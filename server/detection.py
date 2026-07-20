"""
Phase 1 sale detection from the item-market WebSocket events the userscript forwards.

Event shapes (confirmed from live captures), delivered as body["data"][i]["message"]:
  add    : {listingID, anonymous, status, user:{ID,name}, faction:{...}, price, available, relatedItemID}
  update : {itemID, minPrice}                  -> price floor (deal-finder, Phase 2; ignored here)
           {listingID, available}              -> partial buy: available decreased = a sale
  remove : {listingID}                         -> listing gone = sale of the remaining qty
                                                  (also what "list then delist" emulates for testing)
  counterIncrease/Decrease                     -> listing-count noise; the userscript already drops these

Flow: add -> store listing (with seller) + warm enrichment; quantity-drop/remove -> record a sale,
accumulate the seller's window total, (re)enrich, then alert if the mug clears threshold — unless
the seller is in hospital, in which case we hold and the bot's loop posts ~60s before release.
"""

import asyncio
import logging
import time
from typing import Dict, Optional

import httpx

import config
from server import db, mug
from server.enrich import enrich_clothing, enrich_seller

log = logging.getLogger("mug.detect")

_client: Optional[httpx.AsyncClient] = None

# One lock per seller: sale handling is now spawned as independent tasks (so it never blocks the
# poll loop), so two sales for the SAME seller could otherwise race on the window/cooldown/claim
# and double-alert. Serialise per seller; different sellers still run concurrently.
_seller_locks: Dict[int, "asyncio.Lock"] = {}


def _seller_lock(seller_id) -> "asyncio.Lock":
    lock = _seller_locks.get(seller_id)
    if lock is None:
        lock = asyncio.Lock()
        _seller_locks[seller_id] = lock
    return lock


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient()
    return _client


def _item_name(item_id) -> str:
    if item_id is None:
        return "Unknown"
    return config.ITEM_MAP.get(item_id, f"Item {item_id}")


def _is_anon(row: Dict) -> bool:
    if row.get("anonymous"):
        return True
    user = row.get("user") or {}
    return user.get("ID") in (0, "0", None)


async def process_market_payload(body: Dict, bot=None) -> None:
    """Entry point from the /market ingest route."""
    for entry in (body.get("data") or []):
        if not isinstance(entry, dict):
            continue
        msg = entry.get("message", entry)
        action = msg.get("action")
        for row in (msg.get("data") or []):
            try:
                if action == "add":
                    await _handle_add(row)
                elif action == "update":
                    if row.get("listingID") is not None and isinstance(row.get("available"), (int, float)):
                        await _handle_quantity_update(row, bot)
                    # else: {itemID, minPrice} price floor -> deal-finder (Phase 2)
                elif action == "remove":
                    await _handle_remove(row, bot)
            except Exception:
                log.exception("error handling %s row %r", action, row)


async def _handle_add(row: Dict) -> None:
    listing_id = row.get("listingID")
    if listing_id is None:
        return
    item_id = row.get("relatedItemID")
    anon = _is_anon(row)
    user = row.get("user") or {}
    seller_id = None if anon else user.get("ID")
    seller_name = None if anon else user.get("name")
    faction = (row.get("faction") or {}).get("name")

    await db.upsert_listing(
        listing_id=listing_id, item_id=item_id, item_name=_item_name(item_id),
        seller_id=seller_id, price=row.get("price"), available=row.get("available"),
        anonymous=anon,
    )
    if not anon and seller_id:
        await db.upsert_seller_seen(
            user_id=seller_id, name=seller_name, faction=faction, online_status=row.get("status")
        )
        # Warm enrichment in the background so it's ready by the time they sell.
        asyncio.create_task(_safe_enrich(seller_id))


async def _safe_enrich(seller_id) -> None:
    try:
        await enrich_seller(seller_id, _http())
    except Exception:
        log.exception("enrich failed for %s", seller_id)


async def _handle_quantity_update(row: Dict, bot) -> None:
    listing = await db.get_listing(row["listingID"])
    if not listing:
        return
    new_avail = int(row["available"])
    sold = int(listing["available"] or 0) - new_avail
    await db.set_listing_available(row["listingID"], new_avail)
    if sold > 0 and not listing["anonymous"] and listing["seller_id"]:
        await _record_and_maybe_alert(listing, sold, bot)


async def _handle_remove(row: Dict, bot) -> None:
    listing = await db.get_listing(row["listingID"])
    if not listing:
        return
    await db.delete_listing(row["listingID"])
    remaining = int(listing["available"] or 0)
    if remaining > 0 and not listing["anonymous"] and listing["seller_id"]:
        await _record_and_maybe_alert(listing, remaining, bot)


async def _record_and_maybe_alert(listing: Dict, qty: int, bot) -> None:
    await on_sale(
        seller_id=listing["seller_id"], item_id=listing["item_id"],
        item_name=listing["item_name"], qty=qty, unit_price=listing["price"],
        listing_id=listing["listing_id"], bot=bot,
    )


async def on_sale(*, seller_id, item_id, item_name, qty, unit_price, listing_id=None, bot=None, stats=None) -> None:
    """Shared sale handler used by BOTH the WS path and the API poller: record the sale,
    (re)enrich the seller, then alert if the accumulated mug clears threshold."""
    log.info("SALE detected: seller=%s item=%s(%s) qty=%s unit=%s total=%s listing=%s",
             seller_id, item_name, item_id, qty, unit_price,
             (qty or 0) * (unit_price or 0), listing_id)
    async with _seller_lock(seller_id):
        sale_id = await db.record_sale(
            listing_id=listing_id, item_id=item_id, item_name=item_name,
            seller_id=seller_id, qty_sold=qty, unit_price=unit_price,
        )
        # Per sale we only ensure the clothing flag is fresh (needed for the mug calc). Status/TBS
        # are fetched only if the threshold is crossed (see _evaluate_alert).
        seller = await enrich_clothing(seller_id, _http())
        await _evaluate_alert(
            seller_id, seller, bot,
            last_item={"item_id": item_id, "item_name": item_name, "stats": stats},
            sale_id=sale_id, listing_id=listing_id,
        )


async def _cfg(key, default):
    return await db.get_config(key, default)


async def _mug_for(seller: Optional[Dict], total: int):
    # mug % + clothing reduction are fixed (match the reference bot); only the threshold tunes.
    return mug.compute_mug(
        total,
        in_clothing_store=bool(seller and seller.get("in_clothing_store")),
        min_value=await _cfg("min_mug_value", config.MIN_MUG_VALUE),
    )


async def _evaluate_alert(seller_id, seller: Optional[Dict], bot, last_item=None,
                          sale_id=None, listing_id=None) -> None:
    window = await _cfg("sale_window_seconds", config.SALE_WINDOW_SECONDS)
    win = await db.seller_window(seller_id, window)
    if win["total"] <= 0:
        return
    res = await _mug_for(seller, win["total"])      # cheap pre-check on the raw window total
    if not res.qualifies:
        log.info("mug-eval seller=%s: raw=%s mug_min=%s < threshold -> skip", seller_id, win["total"], res.mug_min)
        return
    if not await _cooldown_ok(seller):
        log.info("mug-eval seller=%s: raw=%s qualifies but in cooldown -> skip", seller_id, win["total"])
        return
    # Threshold crossed: NOW make a fresh API call for their current status (hospital/online) +
    # last_action + TBS. last_action drives the banking decay; status drives hold + embed colour.
    seller = await enrich_seller(seller_id, _http(), force=True)
    # Delist guard for GEAR: if the seller acted right around when their item vanished, it's almost
    # certainly them pulling/relisting/trading it, not a buy. Drop the phantom sale so it neither
    # fires a $0 mug nor inflates the window.
    if config.DELIST_GUARD_SECONDS and str(listing_id or "").startswith(("uid:", "item:")):
        la = (seller or {}).get("last_action_ts")
        if la and (int(time.time()) - int(la)) < config.DELIST_GUARD_SECONDS:
            log.info("mug-eval seller=%s: gear/item vanish but seller acted %ss ago -> likely DELIST, "
                     "dropping (not a sale)", seller_id, int(time.time()) - int(la))
            if sale_id is not None:
                await db.delete_sale(sale_id)
            return
    raw_total = win["total"]
    win, res = await _bank_adjust(seller, win)
    win, res, protection_pct = await _mug_protect_adjust(seller, win)
    la = (seller or {}).get("last_action_ts")
    state = (seller or {}).get("status_state")
    log.info("mug-eval seller=%s: raw=%s adj=%s mug=%s-%s qualifies=%s protection=%s%% | status=%s last_action=%ss-ago online=%s",
             seller_id, raw_total, win["total"], res.mug_min, res.mug_max, res.qualifies, protection_pct,
             state, (int(time.time()) - int(la)) if la else "?", (seller or {}).get("online_status"))
    if not res.qualifies:
        return  # decayed below threshold — they've likely banked it

    # Likely de-list handling (soft, configurable via /delist): a GEAR uid that vanished while the
    # seller was actively online is almost always them pulling their own listing, not a buy.
    tag_secs = await _cfg("delist_tag_seconds", config.DELIST_TAG_SECONDS)
    delist_mode = (await _cfg("delist_mode", config.DELIST_MODE) or "ping")
    delist_secs = _delist_active_secs(seller, listing_id, tag_secs)
    if delist_secs is not None and delist_mode == "off":
        log.info("mug-eval seller=%s: likely de-list (acted %ss ago) + mode=off -> drop, no alert",
                 seller_id, delist_secs)
        if sale_id is not None:
            await db.delete_sale(sale_id)   # also remove the phantom from the value window
        return

    # Hospital handling (configurable via /hosplock). hold = defer + post once near release (the
    # bot loop does it); live/remind = post now (remind also re-posts near release, so keep the
    # window — don't claim — until that reminder fires).
    hosp_mode = (await _cfg("hosp_mode", config.HOSP_MODE) or "live")
    in_hospital = state == "Hospital" and bool((seller or {}).get("status_until"))
    if in_hospital and hosp_mode == "hold":
        log.info("mug-eval seller=%s: HELD (in hospital until %s, mode=hold) -> loop posts before release",
                 seller_id, (seller or {}).get("status_until"))
        return

    # A hospital post only PINGS the role when the seller is within the reminder lead of release
    # (i.e. about to be muggable). A first alert for someone still locked up longer than the lead
    # posts SILENTLY — the near-release reminder (remind mode) pings when it's actually actionable.
    hosp_far = False
    if in_hospital:
        lead = int(await _cfg("hosp_lead_seconds", config.HOSPITAL_RELEASE_LEAD_SECONDS))
        hosp_far = int((seller or {}).get("status_until")) - int(time.time()) > lead

    if state == "Hospital":
        variant = "hospital"
    elif state in ("Traveling", "Abroad"):
        variant = "overseas"   # flag a target abroad so it's clear you must fly to them to mug
    else:
        variant = "mug"
    hospitalize = await _is_hospitalize_faction(seller)
    silent = bool((delist_secs is not None and delist_mode == "silent") or hosp_far)
    log.info("mug-eval seller=%s: POST alert variant=%s value=%s hospitalize=%s delist=%s mode=%s silent=%s",
             seller_id, variant, win["total"], hospitalize,
             delist_secs if delist_secs is not None else "-", delist_mode, silent)
    if bot is not None:
        event = _build_event(seller_id, seller, win, res, variant=variant, last_item=last_item,
                             hospitalize=hospitalize, protection_pct=protection_pct,
                             delist_active_secs=delist_secs)
        event["silent"] = silent
        await bot.post_mug_alert(event)
    await _mark_alerted(seller_id, win, claim=not (in_hospital and hosp_mode == "remind"))


def _was_mugged(seller: Optional[Dict]) -> bool:
    return "mug" in ((seller or {}).get("status_details") or "").lower()


async def _mug_protect_adjust(seller: Optional[Dict], win: Dict):
    """Mug protection: a seller who was recently MUGGED has their remaining money protected, decaying
    linearly back to fully-muggable over MUG_PROTECTION_HOURS. Applies whether they're still in
    hospital OR already out (Okay) — protection outlasts the hospital stay. A "Was Mugged" seller who
    recovered but stayed inactive (the $300M Swiss Army Knife: mugged ~2.5h ago, still Offline) was
    posting FULL value and mugging for $0; the old `status==Hospital` gate skipped it. Mug time ~=
    their last action (can't act while hospitalised / offline since). Returns (win, res, pct)."""
    hours = config.MUG_PROTECTION_HOURS
    la = (seller or {}).get("last_action_ts")
    if hours <= 0 or not la or not _was_mugged(seller):
        return win, await _mug_for(seller, win["total"]), 0
    elapsed = int(time.time()) - int(la)
    muggable = max(0.0, min(1.0, elapsed / (hours * 3600)))   # protection decays over `hours`
    adj = int(win["total"] * muggable)
    win = {**win, "total": adj}
    return win, await _mug_for(seller, adj), round((1.0 - muggable) * 100)


async def _is_hospitalize_faction(seller: Optional[Dict]) -> bool:
    """True if the seller is in a faction flagged for hospitalization (don't mug — hosp them)."""
    fid = (seller or {}).get("faction_id")
    return bool(fid) and int(fid) in set(await db.get_hospitalize_factions())


async def _bank_adjust(seller: Optional[Dict], win: Dict):
    """Apply the banking-decay heuristic to the window total (seller active since a sale ⇒ likely
    banked) and recompute the mug. Returns (adjusted_win, recomputed_res)."""
    adj_total = mug.bank_adjusted_total(win.get("sales") or [], (seller or {}).get("last_action_ts"))
    win = {**win, "total": adj_total}
    return win, await _mug_for(seller, adj_total)


async def _mark_alerted(seller_id, win: Dict, claim: bool = True) -> None:
    """Record the cooldown and (assuming the alert => a mug) claim the alerted sales so they're
    never re-counted in a later window. claim=False keeps the window intact (used for the live
    'remind' hospital post, where the near-release reminder still needs the accumulated total)."""
    await db.set_last_alerted(seller_id)
    sales = win.get("sales") or []
    if claim and config.MUG_CLAIM_ON_ALERT and sales:
        await db.set_mug_claimed(seller_id, max(s["sold_at"] for s in sales))


async def _cooldown_ok(seller: Optional[Dict]) -> bool:
    last = (seller or {}).get("last_alerted_at")
    if not last:
        return True
    cooldown = await _cfg("alert_cooldown_seconds", config.ALERT_COOLDOWN_SECONDS)
    return (int(time.time()) - int(last)) >= cooldown


# How long after a sale the seller can act before we assume the cash is banked (used to suppress a
# stale hospital-release alert for someone who's been online since the sale). Small on purpose.
_BANKED_AFTER_SALE_GRACE = 120


def _banked_since_sales(seller: Optional[Dict], win: Dict) -> Optional[int]:
    """If the seller's most recent action is well AFTER their most recent sale in the window, they've
    been online since selling and have almost certainly stashed the cash in their wallet (can't be
    mugged). Returns the seconds-active-since-the-sale if so, else None. Offline/old sellers (last
    action before the sale) return None — they haven't had the chance, so they stay muggable."""
    la = (seller or {}).get("last_action_ts")
    latest_sale = max((int(s.get("sold_at") or 0) for s in (win.get("sales") or [])), default=0)
    if la and latest_sale and int(la) > latest_sale + _BANKED_AFTER_SALE_GRACE:
        return int(la) - latest_sale
    return None


async def post_hospital_due(bot) -> None:
    """Called periodically by the bot: post the near-release alert for in-hospital sellers.
      hold   -> the ONLY post for that seller (deferred from detection); gated by the cooldown.
      remind -> a re-post (detection already posted live); gated so it fires exactly ONCE in the
                lead window (we skip if the seller was last alerted inside it).
      live   -> nothing to do here (the single post happened at detection).
    Either way we re-verify live status: only post if they're genuinely leaving within the lead
    window (or already out); a re-hospitalised seller is re-held for the real near-release."""
    hosp_mode = (await _cfg("hosp_mode", config.HOSP_MODE) or "live")
    if hosp_mode == "live":
        return
    lead = int(await _cfg("hosp_lead_seconds", config.HOSPITAL_RELEASE_LEAD_SECONDS))
    for row in await db.sellers_in_hospital_due(lead):
        seller_id = row["user_id"]
        window = await _cfg("sale_window_seconds", config.SALE_WINDOW_SECONDS)
        win = await db.seller_window(seller_id, window)
        if win["total"] <= 0:
            continue
        # hold: the cooldown is the de-dup. remind: the cooldown was just set by the live detection
        # post, so it would (wrongly) block the reminder — instead gate on "already alerted inside
        # this lead window" further below, after we know the live release time.
        if hosp_mode == "hold" and not await _cooldown_ok(row):
            continue
        res = await _mug_for(row, win["total"])
        if not res.qualifies:
            continue
        seller = await enrich_seller(seller_id, _http(), force=True)
        state = (seller or {}).get("status_state")
        until = (seller or {}).get("status_until")
        now = int(time.time())
        if state == "Hospital" and until and int(until) > now + lead:
            log.info("hospital-due seller=%s: re-hospitalised (until %s, >%ss out) -> re-hold",
                     seller_id, until, lead)
            continue
        # Out of hospital and active since the sale? They've banked it — don't post a stale alert.
        banked_secs = _banked_since_sales(seller, win)
        if banked_secs is not None:
            log.info("hospital-due seller=%s: online %ss after the sale -> assume banked, skip",
                     seller_id, banked_secs)
            await db.set_mug_claimed(seller_id, max(s["sold_at"] for s in win["sales"]))
            continue
        if hosp_mode == "remind":
            last = row.get("last_alerted_at")
            if until and last and int(last) >= int(until) - lead:
                continue   # already reminded inside this lead window -> fire only once
        win, res = await _bank_adjust(seller, win)
        win, res, protection_pct = await _mug_protect_adjust(seller, win)
        if not res.qualifies:
            continue
        variant = "hospital" if state == "Hospital" else "mug"
        if bot is not None:
            await bot.post_mug_alert(_build_event(
                seller_id, seller, win, res, variant=variant,
                hospitalize=await _is_hospitalize_faction(seller), protection_pct=protection_pct))
        await _mark_alerted(seller_id, win)   # claim the window now (final post for this stay)


def _delist_active_secs(seller: Optional[Dict], listing_id, tag_secs) -> Optional[int]:
    """Likely-de-list signal (no extra API call): a GEAR uid vanished, OR a stackable-item listing's
    quantity dropped, and the SELLER had acted within `tag_secs` of it -> they almost certainly
    pulled/relisted/repriced/edited it themselves (a real buy is the BUYER's action, which leaves the
    seller's last_action old). Applies to BOTH gear (`uid:` listing) and items (`item:` listing) —
    the item path used to be skipped, so item de-lists (e.g. the Armor Cache, seller active 36s ago)
    wrongly pinged. We key purely on last_action recency, NOT the online badge: Torn's coarse status
    can read 'Offline' within a minute or two of a real action (the $450M Guandao acted 88s before
    the vanish yet showed Offline). Returns seconds-since-last-action if within the buffer, else None."""
    if not tag_secs or not str(listing_id or "").startswith(("uid:", "item:")):
        return None
    la = (seller or {}).get("last_action_ts")
    if la is not None:
        age = int(time.time()) - int(la)
        if 0 <= age <= int(tag_secs):
            return age
    return None


def _build_event(seller_id, seller: Optional[Dict], win: Dict, res, *, variant="mug",
                 last_item=None, hospitalize=False, protection_pct=0, delist_active_secs=None) -> Dict:
    seller = seller or {}
    # Headline item for the embed: the sale that triggered this, else the most recent in the window.
    headline = last_item or {}
    if not headline.get("item_name") and win["sales"]:
        s = win["sales"][-1]
        headline = {"item_id": s.get("item_id"), "item_name": s.get("item_name"), "stats": None}
    return {
        "variant": variant,
        "seller_id": seller_id,
        "seller_name": seller.get("name") or str(seller_id),
        "item_id": headline.get("item_id"),
        "item_name": headline.get("item_name"),
        "item_stats": headline.get("stats"),
        "value_change": win["total"],
        "num_sales": win["count"],
        "mug_min": res.mug_min,
        "mug_max": res.mug_max,
        "tbs": seller.get("bsp_total"),
        "last_action_ts": seller.get("last_action_ts"),
        "status_state": seller.get("status_state"),     # Hospital/Okay/Traveling/Abroad/...
        "status_until": seller.get("status_until"),      # hospital release time (countdown)
        "status_description": seller.get("status_description"),  # e.g. "In Switzerland"
        "online_status": seller.get("online_status"),   # Online/Idle/Offline -> embed colour + dot
        "detected_ts": int(time.time()),
        "faction": seller.get("faction"),
        "faction_id": seller.get("faction_id"),
        "hospitalize": bool(hospitalize),
        "protection_pct": protection_pct,   # mug-protection reduction applied (%)
        "delist_active_secs": delist_active_secs,  # set => "⚠️ Likely de-list" (seller active Ns ago)
        "in_clothing_store": bool(seller.get("in_clothing_store")),
        # Only claim "Was Mugged" if the hospital REASON actually says so — being in hospital for
        # any other reason (self-hosp, attacked, medical) is NOT a mug.
        "was_mugged": "mug" in (seller.get("status_details") or "").lower(),
        "sales": [
            {"item_name": s["item_name"], "qty": s["qty_sold"], "price": s["unit_price"], "ts": s["sold_at"]}
            for s in win["sales"][-10:]
        ],
    }
