"""
API-mode sale detection for GEAR (weapons/armour). Compliant: only the Torn item-market API is
polled here; seller identity comes from the item-matcher capture (the `listings` seller map).

Per item we have captured sellers for, we poll `itemmarket` and read each listing's
`item_details.uid`. The uid is unique per weapon/armour and persists across reprices, so:
  - a uid newly present that matches a captured (item_id, price) seller -> start tracking it,
  - a tracked uid absent for GEAR_VANISH_CONFIRM_POLLS polls -> that item sold -> mug the seller.

Feeds detection.on_sale (the same enrichment + mug + alert path the WS path uses). Stackable
items are intentionally out of scope here (added later via price/quantity matching).
"""

import asyncio
import json
import logging
import time

import httpx

import config
from server import db, deal_finder, detection, torn
from server import keys as keypool

log = logging.getLogger("mug.poller")

# Adaptive per-item scheduling state (see CACHE_REPOLL_OFFSET in config). _next_due[item_id] is the
# wall-clock epoch at which the item is next due to be polled; _last_cts[item_id] is the last
# cache_timestamp we saw (to detect "snapshot hasn't refreshed yet" and avoid busy-looping).
_next_due: dict = {}
_last_cts: dict = {}

# Sale handling (enrich + mug calc + Discord post) runs as independent tasks so it NEVER blocks the
# poll loop — a sale is processed in real time, concurrently with ongoing polling, instead of
# stalling that item's poll slot while we wait on the Torn API / Discord.
_sale_tasks: set = set()


def _spawn_sale(coro) -> None:
    task = asyncio.create_task(coro)
    _sale_tasks.add(task)
    task.add_done_callback(_sale_tasks.discard)


async def drain_sale_tasks() -> None:
    """Await all in-flight sale tasks. The poller never needs this (tasks finish on their own);
    tests use it to observe the result of a reconcile synchronously."""
    if _sale_tasks:
        await asyncio.gather(*list(_sale_tasks), return_exceptions=True)


async def run_gear_poller(bot=None) -> None:
    """Long-lived loop; started from main when DETECTION_MODE is api/both. Adaptive cache-timed: each
    item is polled just after its next cache snapshot is due (cache_timestamp + CACHE_REPOLL_OFFSET),
    not on a blind fixed interval — freshest data, ~1 poll per item per 30s cache cycle."""
    client = httpx.AsyncClient()
    log.info("gear API poller started (adaptive: repoll at cache_ts+%ss, retry %ss, confirm=%s polls)",
             config.CACHE_REPOLL_OFFSET, config.CACHE_RETRY_SECONDS, config.GEAR_VANISH_CONFIRM_POLLS)
    try:
        while True:
            try:
                # Self-throttling: only actually re-fetches every VALUES_REFRESH_SECONDS (~4h).
                await deal_finder.maybe_refresh_values(client)
                await _poll_due(bot, client)
            except Exception:
                log.exception("gear poll tick error")
            await asyncio.sleep(config.POLL_TICK_SECONDS)
    finally:
        await client.aclose()


def _schedule_next(item_id, cts) -> None:
    """Set when this item is next due, from the cache_timestamp we just observed. A cache stamped
    `cts` only becomes visible ~30s later and is replaced ~30s after that, so the NEXT distinct
    snapshot appears at ~cts+60 → poll at cts+CACHE_REPOLL_OFFSET to land just after it. If the
    snapshot hasn't actually refreshed (same cts as last time, or fetch failed), re-check soon
    instead of recomputing from the stale stamp (which would busy-loop)."""
    now = time.time()
    if cts is None or cts == _last_cts.get(item_id):
        _next_due[item_id] = now + config.CACHE_RETRY_SECONDS
        return
    _last_cts[item_id] = cts
    _next_due[item_id] = max(cts + config.CACHE_REPOLL_OFFSET, now + config.CACHE_RETRY_SECONDS)


async def _poll_due(bot, client: httpx.AsyncClient) -> None:
    """Poll every watched item whose scheduled time has arrived, CONCURRENTLY (bounded). New items
    (no schedule yet) are due immediately; items no longer watched are dropped from the schedule."""
    targets = await db.poll_targets()
    if not targets:
        return
    now = time.time()
    watched = {t[0] for t in targets}
    for gone in [i for i in _next_due if i not in watched]:
        _next_due.pop(gone, None)
        _last_cts.pop(gone, None)

    due = [(i, g, c) for (i, g, c) in targets if _next_due.get(i, 0.0) <= now]
    if not due:
        return

    concurrency = config.MAX_CONCURRENT_POLLS or keypool.active_count() or 1
    concurrency = max(1, min(concurrency, len(due)))
    sem = asyncio.Semaphore(concurrency)
    all_bonus = await db.get_gear_all_bonus()   # gear to fetch with NO bonus filter (capture all)

    async def _one(item_id, is_gear, category):
        async with sem:   # each in-flight call grabs the next key in the rotation
            try:
                cts = await poll_item(bot, client, item_id, is_gear, category, all_bonus)
                _schedule_next(item_id, cts)
            except Exception:
                log.exception("poll error item=%s", item_id)
                _next_due[item_id] = time.time() + config.CACHE_RETRY_SECONDS

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await asyncio.gather(*(_one(i, g, c) for i, g, c in due))
    log.debug("poll tick: %d/%d items due, polled in %.1fs, concurrency=%d",
              len(due), len(targets), loop.time() - t0, concurrency)


async def _fetch_pages(client, item_id, bonus, max_pages):
    """Paginate up to max_pages (stops early when a page returns < 100). Returns
    (item_meta, listings, complete, cache_ts); listings is None on a first-page API error.
    `complete` is False if a LATER page errored — the snapshot is then truncated (missing the
    listings on the failed page), so it must NOT drive sale detection or every missing uid looks
    sold (the TCT-midnight 504 storm). The caller still tracks the listings it did get, just no
    vanish pass. `cache_ts` (from page 0) drives adaptive re-poll scheduling."""
    item_meta = None
    cache_ts = None
    out = []
    for page in range(max_pages):
        res = await torn.get_itemmarket(client, item_id, bonus=bonus, offset=page * 100)
        if res is None:
            # page 0 failed -> nothing usable; later page failed -> partial (incomplete) snapshot.
            return (item_meta, (out if page > 0 else None), False, cache_ts)
        if item_meta is None:
            item_meta = res.get("item") or {}
            cache_ts = res.get("cache_timestamp")
        listings = res.get("listings") or []
        out.extend(listings)
        if len(listings) < 100:
            break
    return (item_meta, out, True, cache_ts)


def _detect_is_gear(item_meta, listings) -> bool:
    if (item_meta or {}).get("type") in ("Primary", "Secondary", "Melee", "Defensive"):
        return True
    return any((e.get("item_details") or {}).get("uid") is not None for e in listings)


async def poll_once(bot, client: httpx.AsyncClient) -> None:
    """One back-to-back pass over all watched items (used by tests / non-staggered mode)."""
    all_bonus = await db.get_gear_all_bonus()
    for item_id, is_gear, category in await db.poll_targets():
        await poll_item(bot, client, item_id, is_gear, category, all_bonus)


def _wants_all_bonus(item_id, category, tokens) -> bool:
    """True if this gear should be polled with NO bonus filter (capture every listing), per the
    /gear allbonus list — matched by item id or by Torn category (Primary/Secondary/Melee/Defensive)."""
    if not tokens:
        return False
    low = {str(t).lower() for t in tokens}
    return str(item_id) in low or (category and str(category).lower() in low)


async def poll_item(bot, client: httpx.AsyncClient, item_id, is_gear, category=None, all_bonus=None):
    """Poll one item, reconcile its snapshot, and RETURN the snapshot's cache_timestamp (or None on
    a first-page failure) so the adaptive scheduler can time the next poll."""
    # GEAR: filter to bonus listings (the valuable mug targets) UNLESS this item/category is on the
    # "all bonuses" list (then capture every listing). Non-gear / undetected: no filter, 1 page.
    if is_gear is True and not _wants_all_bonus(item_id, category, all_bonus):
        bonus = config.GEAR_BONUS_FILTER
    else:
        bonus = None
    max_pages = config.MAX_PAGES_GEAR if is_gear else config.MAX_PAGES_ITEM
    item_meta, listings, complete, cache_ts = await _fetch_pages(client, item_id, bonus, max_pages)
    if listings is None:
        return cache_ts
    if is_gear is None:
        is_gear = _detect_is_gear(item_meta, listings)

    # One transaction for this item's whole poll. Reconciling a gear item writes a row per listing
    # (plus the watchlist bookkeeping); committing each of those separately meant an fsync per
    # listing on the single aiosqlite worker thread, stalling every other item's poll and the alert
    # path behind it. See db.batch().
    label = (item_meta or {}).get("name")
    item_current = None
    async with db.batch():
        await db.watchlist_mark_polled(
            item_id, len(listings), is_gear=is_gear, category=(item_meta or {}).get("type")
        )
        if label:
            await db.watchlist_ensure(item_id, label, is_gear=is_gear)

        if is_gear:
            current = {}
            for entry in listings:
                det = entry.get("item_details") or {}
                uid = det.get("uid")
                if uid is None:
                    continue
                current[str(uid)] = {"price": entry.get("price"), "stats": det.get("stats")}
            await reconcile_gear(bot, item_id, current, label, complete=complete)
        else:
            item_current = [{"price": e.get("price"), "amount": e.get("amount")} for e in listings]
            await reconcile_items(bot, item_id, item_current, complete=complete, item_label=label)

    # Deliberately OUTSIDE the batch: this posts to Discord, and an HTTP round trip must never be
    # made with a write transaction held open on the single shared connection.
    if item_current is not None:
        await deal_finder.check_item_deals(
            bot, item_id, item_current, item_name=label, category=(item_meta or {}).get("type")
        )
    return cache_ts


async def reconcile_items(bot, item_id, current: list, complete: bool = True, item_label=None) -> None:
    """Items (fungible — no uid): track each UNIQUE price and watch its quantity. A seller is
    attributed only by the STRICT price+exact-quantity fingerprint (db.resolve_item_seller): the
    captured quantity must still match the live one, else the listing stays unattributed and is
    surfaced to the matcher to (re)capture — we never guess a seller from price alone. A quantity
    drop at a confidently-attributed unique price = a sale by that seller. Same-price collisions are
    dropped (can't tell which sold); a full vanish only alerts when ITEM_ALERT_ON_REMOVE is set (sale
    vs delist/reprice is ambiguous). `complete=False` (a paginated page errored) suppresses the
    full-removal pass so a truncated snapshot can't mass-fire."""
    tracked = await db.get_tracked_items(item_id)
    price_counts: dict = {}
    amount_at: dict = {}
    for e in current:
        p = e["price"]
        price_counts[p] = price_counts.get(p, 0) + 1
        amount_at[p] = e["amount"]

    for p, count in price_counts.items():
        if count != 1:
            if p in tracked:
                await db.delete_tracked_item(item_id, p)   # became ambiguous
            continue
        amt = amount_at[p]
        if p in tracked:
            prev = tracked[p].get("last_amount")
            seller_id = tracked[p].get("seller_id")
            if prev is not None and amt < prev and seller_id:
                log.info("item sale: item=%s price=%s qty=%s seller=%s", item_id, p, prev - amt, seller_id)
                _spawn_sale(detection.on_sale(
                    seller_id=seller_id, item_id=item_id,
                    item_name=tracked[p].get("item_name") or item_label,
                    qty=prev - amt, unit_price=p, listing_id=f"item:{item_id}:{p}", bot=bot,
                ))
            await db.upsert_tracked_item(
                item_id=item_id, price=p, seller_id=seller_id,
                item_name=tracked[p].get("item_name"), last_amount=amt,
            )
        else:
            match = await db.resolve_item_seller(item_id, p, amt)
            if match:
                await db.upsert_tracked_item(
                    item_id=item_id, price=p, seller_id=match["seller_id"],
                    item_name=match.get("item_name"), last_amount=amt,
                )

    if not complete:
        return   # truncated snapshot: a missing price may just be on the failed page, not removed
    for p, row in tracked.items():
        if p in price_counts:
            continue
        misses = await db.bump_tracked_item_miss(item_id, p)
        if misses < config.ITEM_VANISH_CONFIRM_POLLS:
            continue
        await db.delete_tracked_item(item_id, p)
        if config.ITEM_ALERT_ON_REMOVE and row.get("seller_id"):
            _spawn_sale(detection.on_sale(
                seller_id=row["seller_id"], item_id=item_id, item_name=row.get("item_name"),
                qty=row.get("last_amount") or 1, unit_price=p,
                listing_id=f"item:{item_id}:{p}", bot=bot,
            ))


async def reconcile_gear(bot, item_id, current: dict, item_label=None, complete: bool = True) -> None:
    """Diff this item's current uids. A uid we can attribute to a seller (exact uid match, or a
    unique price) is tracked for sale detection; an anonymous uid is ignored; everything else is
    recorded as UNMATCHED so the item-matcher knows to capture its seller. Vanished tracked uids
    with a seller = a sale.

    `complete=False` means a paginated page errored and `current` is a TRUNCATED snapshot — we
    still track the uids we did see, but SKIP the vanish->sale pass, since uids on the failed page
    would otherwise all look sold (the TCT-midnight 504 storm that fired ~90 false mugs at once).
    """
    tracked = await db.get_tracked_uids_for_item(item_id)
    uid_map = await db.get_gear_uid_map(item_id)
    anon = await db.get_anon_uid_set()

    price_counts: dict = {}
    for info in current.values():
        price_counts[info["price"]] = price_counts.get(info["price"], 0) + 1

    # Per-poll disposition tally (logged below; helps diagnose "weapons won't leave the list").
    stats_n = {"tracked": 0, "uid": 0, "auction": 0, "price": 0, "anon": 0, "unmatched": 0}

    for uid, info in current.items():
        if uid in tracked:
            seller_id = tracked[uid].get("seller_id")
            attribution = tracked[uid].get("attribution")
            # A direct capture that arrives AFTER an inference has to replace it. This branch
            # short-circuits the whole resolution chain below, so without an explicit upgrade here
            # the FIRST answer wins for the life of the listing — and the first answer is the
            # weaker one. That is the invalidation rule #12 turns on, and it is invisible when it
            # fails: the alert still fires, just naming the wrong player.
            if attribution == "auction" and uid in uid_map:
                log.info("uid=%s: item-matcher captured seller=%s — replacing the AH inference (%s)",
                         uid, uid_map[uid]["seller_id"], seller_id)
                seller_id = uid_map[uid]["seller_id"]
                attribution = "matcher"
            await db.upsert_tracked_gear(  # resets misses, refreshes last_seen
                uid=uid, item_id=item_id, item_name=tracked[uid].get("item_name"),
                price=info["price"], seller_id=seller_id,
                stats_json=tracked[uid].get("stats_json"),
                attribution=attribution,
            )
            stats_n["tracked"] += 1
            continue

        seller_id = None
        item_name = None
        how = None
        # GEAR is attributed to a seller ONLY by exact uid (armouryID == API item_details.uid). There
        # is deliberately NO price fallback: matching by price alone pins one player's weapon onto
        # another who listed at the same round price (the $300M Swiss Army Knife mis-match). An
        # uncaptured uid stays unmatched until the item-matcher captures its seller — that's fine.
        if uid in uid_map:
            seller_id = uid_map[uid]["seller_id"]
            item_name = uid_map[uid].get("item_name")
            how = "uid"

        # No direct capture. Before writing this off as an anonymous dead end, ask the auction
        # house: if this uid was won at auction and nothing since says otherwise, the person
        # listing it anonymously is the winner. Deliberately AFTER uid_map — a direct capture is
        # stronger evidence and must never be overridden by an inference.
        attribution = "matcher" if seller_id else None
        if not seller_id:
            match = await db.get_anon_match(uid)
            decision = await detection.anon_match_decision(match)
            if decision["use"]:
                seller_id = match["buyer_id"]
                item_name = item_label
                attribution = "auction"
                how = "auction"
                if config.DEBUG_GEAR:
                    log.info("  uid=%s ANON-MATCH -> seller=%s (%s, %s)",
                             uid, seller_id, match.get("buyer_name"), decision["reason"])

        if seller_id:
            await db.upsert_tracked_gear(
                uid=uid, item_id=item_id, item_name=item_name, price=info["price"],
                seller_id=seller_id, attribution=attribution,
                stats_json=json.dumps(info.get("stats")) if info.get("stats") else None,
            )
            await db.remove_unmatched_gear(uid)
            stats_n[how] += 1
        elif uid in anon:
            await db.remove_unmatched_gear(uid)              # anonymous -> stash + ignore
            stats_n["anon"] += 1
        else:                                                # missing a seller -> unmatched
            stats = info.get("stats") or {}
            stat = stats.get("damage") if stats.get("damage") is not None else stats.get("armor")
            await db.upsert_unmatched_gear(
                uid=uid, item_id=item_id, item_name=item_label, price=info["price"],
                stat=stat, accuracy=stats.get("accuracy") or 0,
            )
            stats_n["unmatched"] += 1
            if config.DEBUG_GEAR:
                log.info("  uid=%s price=%s UNMATCHED (in_uid_map=%s in_anon=%s price_count=%s)",
                         uid, info["price"], uid in uid_map, uid in anon,
                         price_counts.get(info["price"]))

    if config.DEBUG_GEAR or stats_n["unmatched"]:
        log.info("reconcile item=%s: %d listings -> tracked=%d uid=%d price=%d anon=%d unmatched=%d "
                 "(uid_map=%d, anon=%d)", item_id, len(current), stats_n["tracked"], stats_n["uid"],
                 stats_n["price"], stats_n["anon"], stats_n["unmatched"], len(uid_map), len(anon))

    # A truncated snapshot (a paginated page errored) is missing real listings, not sold ones — do
    # NOT run the vanish pass against it, or every uid on the failed page fires as a phantom sale.
    if not complete:
        if config.DEBUG_GEAR:
            log.info("reconcile item=%s: incomplete fetch -> skipping vanish pass (no sale detection)", item_id)
        return

    # Tracked uids that are gone this poll: debounce, then treat as a sale.
    for uid, row in tracked.items():
        if uid in current:
            continue
        misses = await db.bump_tracked_miss(uid)
        if misses < config.GEAR_VANISH_CONFIRM_POLLS:
            continue
        await db.delete_tracked_gear(uid)
        # Lifecycle ends when the item leaves the market: drop the seller↔uid pairing (a relist must
        # be re-marked). The sale stays in `sales` for the mug-value window.
        await db.unlink_gear_uid(uid)
        if row.get("seller_id"):
            first_seen, last_seen = row.get("first_seen"), row.get("last_seen")
            listed_for = (int(last_seen) - int(first_seen)) if (first_seen and last_seen) else None
            # Opt-in stale-match guard (off by default): only count if re-confirmed present >1 poll.
            if config.GEAR_REQUIRE_RESEEN and (row.get("seen") or 0) < 2:
                log.info("gear uid=%s item=%s: attributed but only seen %s poll(s) -> stale match, "
                         "dropping (no sale)", uid, item_id, row.get("seen") or 0)
                continue
            log.info("gear vanished -> SALE: uid=%s item=%s price=%s seller=%s listed_for=%ss "
                     "(note: a delist looks identical to a buy — you judge)",
                     uid, item_id, row.get("price"), row.get("seller_id"), listed_for)
            stats = None
            if row.get("stats_json"):
                try:
                    stats = json.loads(row["stats_json"])
                except (ValueError, TypeError):
                    stats = None
            _spawn_sale(detection.on_sale(
                seller_id=row["seller_id"], item_id=item_id,
                item_name=row.get("item_name") or item_label, qty=1, unit_price=row.get("price"),
                listing_id=f"uid:{uid}", bot=bot, stats=stats,
                attribution=row.get("attribution"), uid=uid,
            ))

    await db.prune_unmatched_gear(item_id, set(current.keys()))
