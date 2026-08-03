"""
Auction house scraper — pulls completed auctions into the AH ledger (server/ah_db.py).

Runs as a cron process, not inside the bot. Two modes:

  forward   keep up with newly-completed auctions. This is the cron job.
  backfill  walk backwards through history until Torn stops serving rows. Run manually, or on a
            slow cron until it reports complete.
  coverage  report how much of the anonymous-uid backlog the ledger can de-anonymise (#12's
            go/no-go number). Reads both databases; makes no API calls.
  calibrate report how often AH-inferred ownership would have been CORRECT, by evidence age,
            measured against sellers we directly observed. This is what #12's confidence bands
            should be derived from. Reads both databases; makes no API calls.
  match     resolve every un-attributed uid against the ledger and write the result into the MAIN
            database, with a measured confidence. Runs automatically after `forward`.

Both are resumable: forward is capped by page count (catching up should never be large), backfill
by wall clock (its length is exactly what we are trying to measure). An interrupted run loses
nothing — the next one continues from where it reached.

WHY AN API SCRAPER AND NOT A USERSCRIPT: issue #11 was originally written believing Torn had no
auction-house API and specced a page scraper bound by the "actively viewed page" compliance rule.
Torn's OpenAPI spec (www.torn.com/swagger/openapi.json) lists three auctionhouse endpoints, all at
public access level. An API poller has no such constraint, runs with the browser closed, and returns
structured data including the armoury uid — which is what makes #12's join exact rather than a
name-and-stats guess.
"""

import argparse
import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

import httpx

import config
from server import ah_db, torn

log = logging.getLogger("mug.ah")


class ScrapeError(RuntimeError):
    """A page request failed. Distinct from an empty page, which means 'end of history'."""


# --------------------------------- payload normalisation ------------------------------

def normalize(row: Dict) -> Optional[Dict]:
    """One API auction -> one ledger row, or None if it carries no ownership transfer.

    `item` is a oneOf over a stackable item and full gear details; both declare `uid`, and the gear
    branch reuses the SAME schema the itemmarket endpoint returns, so stats/bonuses/rarity land in
    the shapes the rest of the codebase already expects.

    An auction with no buyer ENDED UNSOLD. It is kept, with buyer_id NULL. It transfers no
    ownership so it is useless for #12 — every ownership lookup filters it out — but it is real
    market signal (a reserve nobody met) and, unlike anything derivable, it cannot be recovered once
    it ages out of Torn's window. The spec marks `buyer` required; it is evidently optimistic.
    """
    buyer = row.get("buyer") or {}
    buyer_id = buyer.get("id") or None          # 0 and absent both mean "nobody bought it"
    auction_id = row.get("id")
    ended_at = row.get("timestamp")
    if not auction_id or not ended_at:
        return None
    # Torn serves some rows with an epoch-adjacent timestamp — real auction, real buyer, unusable
    # date. Dropped rather than stored: ownership is "most recent wins" and confidence is a function
    # of evidence age, so a win that cannot be placed in time is not evidence. Storing it would also
    # wreck MIN(ended_at), which is the backfill's own floor.
    if int(ended_at) < config.AH_MIN_TIMESTAMP:
        return None

    item = row.get("item") or {}
    stats = item.get("stats") or {}
    seller = row.get("seller") or {}
    uid = item.get("uid")
    bonuses = item.get("bonuses")

    return {
        "auction_id": int(auction_id),
        # str(), always. Not for SQLite's sake — TEXT affinity would coerce the integer anyway — but
        # because uid is a Python `str` everywhere else in this codebase (api_poller normalises with
        # str(uid), reconcile_gear then classifies with `uid in tracked` / `in anon`). #12 adds an AH
        # lookup to that chain, and '123' != 123 in a Python set fails silently.
        # uid 0 means "this auction has no item uid" — confirmed live: a single uid=0 covering
        # 49,435 auctions across 302 different item ids, i.e. every stackable Torn has ever
        # auctioned. Stored as NULL, which is what it actually is. The AH_MAX_UID_REUSE backstop
        # would also catch it, but only once it had accumulated enough rows to look pathological;
        # this is exact, and it keeps 9% of the ledger from being stored at all.
        "uid": None if uid in (None, "", 0, "0") else str(uid),
        "item_id": item.get("id"),
        "item_name": item.get("name"),
        "item_type": item.get("type"),
        "sub_type": item.get("sub_type"),
        "damage": stats.get("damage"),
        "accuracy": stats.get("accuracy"),
        "armor": stats.get("armor"),
        "quality": stats.get("quality"),
        "rarity": item.get("rarity"),
        "bonuses": json.dumps(bonuses) if bonuses else None,
        "seller_id": seller.get("id"),
        "seller_name": seller.get("name"),
        "buyer_id": int(buyer_id) if buyer_id else None,
        "buyer_name": buyer.get("name") if buyer_id else None,
        "price": row.get("price"),
        "bids": row.get("bids"),
        "ended_at": int(ended_at),
    }


def _timestamps(raw: List[Dict]) -> List[int]:
    """PLAUSIBLE end timestamps from the RAW page, not the normalised one.

    Raw, because the cursor has to advance over rows we chose not to store (an unsold auction, say)
    as well as ones we kept — deriving it from normalised rows would stall the walk on any page
    whose boundary row was dropped, and re-request that page forever.

    Plausible, because Torn serves some auctions with an epoch-adjacent timestamp and the backfill
    bounds its next request on the OLDEST value it saw — so one such row drags the cursor to 1970.

    Measured honestly: on today's API that costs wasted requests, NOT data. Torn sorts DESC by
    timestamp, so epoch rows come after every datable auction and there is nothing left below them
    to skip (simulated against the real walk: two requests at `to=72` and `to=23`, zero rows lost).
    The guard stays because that safety rests entirely on an ordering guarantee Torn never made, and
    the failure if it ever changed would be silent.

    An empty list here means the page held no datable auction at all — genuinely the end of usable
    history.
    """
    return [int(r["timestamp"]) for r in raw
            if r.get("timestamp") and int(r["timestamp"]) >= config.AH_MIN_TIMESTAMP]


async def _fetch(client: httpx.AsyncClient, **kwargs) -> List[Dict]:
    raw = await torn.get_auctionhouse(client, **kwargs)
    if raw is None:
        raise ScrapeError(f"auctionhouse request failed ({kwargs})")
    return raw


async def _store(raw: List[Dict]) -> Dict[str, int]:
    rows = [n for n in (normalize(r) for r in raw) if n is not None]
    new = await ah_db.record_auctions(rows)
    return {"seen": len(raw), "stored": len(rows), "new": new}


# --------------------------------- forward ---------------------------------------------

async def scrape_forward(client: httpx.AsyncClient, *, limit: Optional[int] = None,
                         max_pages: Optional[int] = None, overlap: Optional[int] = None) -> Dict:
    """Pull everything that has completed since our newest stored auction, oldest-first.

    ASC + `from` is the resumable direction: each page we commit permanently advances the
    watermark, so a crash mid-run costs at most the pages not yet fetched. DESC + `to` would leave
    a gap in the middle if interrupted.
    """
    limit = limit or config.AH_PAGE_LIMIT
    max_pages = max_pages or config.AH_MAX_PAGES_PER_RUN
    overlap = config.AH_OVERLAP_SECONDS if overlap is None else overlap

    watermark = await ah_db.newest_ended_at()
    if watermark is None:
        # Nothing stored yet. Take the newest page and stop, rather than silently walking the entire
        # history on a routine cron tick — that is what `backfill` is for, and it is opt-in.
        raw = await _fetch(client, limit=limit, sort="DESC")
        counts = await _store(raw)
        log.info("ah forward: seeded from empty ledger, %d row(s), %d new", counts["seen"], counts["new"])
        return {"mode": "forward", "pages": 1, "seeded": True, **counts}

    cursor = max(0, watermark - overlap)
    pages = seen = stored = new = 0
    while pages < max_pages:
        raw = await _fetch(client, limit=limit, sort="ASC", from_ts=cursor)
        if not raw:
            break
        counts = await _store(raw)
        seen += counts["seen"]
        stored += counts["stored"]
        new += counts["new"]
        pages += 1

        newest = max(_timestamps(raw), default=cursor)
        if newest <= cursor:
            # Every row on this page ends at or before where we started, so the next request would
            # return the same page. Only reachable if a full page shares one timestamp; bail rather
            # than spin, and say so, because it means `limit` is too small for that second.
            log.warning("ah forward: page did not advance past %d (%d rows) — stopping", cursor, len(raw))
            break
        cursor = newest
        if len(raw) < limit:
            break
        await asyncio.sleep(config.AH_PAGE_DELAY_SECONDS)

    log.info("ah forward: %d page(s), %d row(s), %d new", pages, seen, new)
    return {"mode": "forward", "pages": pages, "seen": seen, "stored": stored, "new": new,
            "seeded": False}


# --------------------------------- backfill --------------------------------------------

async def scrape_backfill(client: httpx.AsyncClient, *, limit: Optional[int] = None,
                          max_pages: Optional[int] = None, max_seconds: Optional[float] = None,
                          restart: bool = False) -> Dict:
    """Walk backwards from the oldest auction we hold until Torn stops returning rows.

    How far back the API will actually serve is not documented, and Torn has capped these windows
    before. Rather than block the design on a manual probe, this discovers it: the walk simply stops
    when a page comes back empty and records that history is complete. Depth becomes a measured
    property of the data instead of an assumption in the schema.

    Bounded by WALL CLOCK rather than page count, because the whole point of this mode is a download
    of unknown length — a page cap would mean guessing the very number we are trying to measure.
    Pass max_pages to cap it anyway (tests do), or max_seconds=0 for a genuinely unbounded run.
    """
    limit = limit or config.AH_PAGE_LIMIT
    budget = config.AH_MAX_SECONDS_PER_RUN if max_seconds is None else max_seconds
    started = time.monotonic()

    if restart:
        # Re-walk from the newest end. Inserts are idempotent, so this costs time and nothing else —
        # which is what makes it the right answer whenever there is doubt about whether a previous
        # walk was complete rather than merely finished.
        log.info("ah backfill: --restart, re-walking from the newest end")
        await ah_db.set_state("backfill_complete", False)

    if not restart and await ah_db.get_state("backfill_complete", False):
        floor = await ah_db.oldest_ended_at()
        log.info("ah backfill: already complete (history reaches back to %s)", _fmt_ts(floor))
        return {"mode": "backfill", "pages": 0, "seen": 0, "stored": 0, "new": 0, "complete": True}

    # A restart starts from the newest end rather than resuming at the floor — resuming is what a
    # normal run does, and the point of a restart is to re-cover ground a previous walk may have
    # jumped over.
    to_ts = None if restart else await ah_db.oldest_ended_at()
    pages = seen = stored = new = 0
    complete = False
    stopped = "page cap"

    while True:
        if max_pages is not None and pages >= max_pages:
            break
        if budget and (time.monotonic() - started) >= budget:
            stopped = "time budget"
            break

        raw = await _fetch(client, limit=limit, sort="DESC", to_ts=to_ts)
        if not raw:
            complete = True
            break
        counts = await _store(raw)
        seen += counts["seen"]
        stored += counts["stored"]
        new += counts["new"]
        pages += 1

        oldest = min(_timestamps(raw), default=None)
        if oldest is None:
            # A full page and not one datable auction on it — we are past the end of usable history
            # and into Torn's epoch-timestamped rows. That is the real floor.
            log.info("ah backfill: page held no datable auction — end of usable history")
            complete = True
            break
        if len(raw) < limit:
            complete = True
            break

        # INCLUSIVE, matching Torn's own `prev` link, which sets `to` to the oldest timestamp ON the
        # page rather than one below it. Several auctions settle in the same second and a page
        # boundary can fall in the middle of them, so stepping to oldest-1 would skip the rest of
        # that second — permanently, and with nothing to show for it. Re-requesting the boundary row
        # costs one no-op insert per page, which is the trade Torn's own pagination makes.
        next_to = oldest
        if to_ts is not None and next_to >= to_ts:
            # A FULL page entirely within one second: an inclusive bound cannot advance off it, so
            # the walk would sit here. Step below the second instead. That may lose auctions inside
            # it, but losing one second beats abandoning the rest of history — and at limit=100 it
            # needs 100 auctions in the same second, which is why this is a fallback, not the rule.
            log.warning("ah backfill: a full page sits entirely at %s — stepping below it; "
                        "auctions within that second may be missed", _fmt_ts(oldest))
            next_to = oldest - 1
        to_ts = next_to

        if pages % 25 == 0:
            log.info("ah backfill: %d pages, %d new, now at %s", pages, new, _fmt_ts(to_ts))
        await asyncio.sleep(config.AH_PAGE_DELAY_SECONDS)

    if complete:
        await ah_db.set_state("backfill_complete", True)
        log.info("ah backfill: COMPLETE — %d page(s), %d new; history reaches back to %s",
                 pages, new, _fmt_ts(await ah_db.oldest_ended_at()))
    else:
        log.info("ah backfill: %d page(s), %d new, stopped on %s — resuming at %s next run",
                 pages, new, stopped, _fmt_ts(to_ts))
    return {"mode": "backfill", "pages": pages, "seen": seen, "stored": stored, "new": new,
            "complete": complete, "reached": to_ts}


# --------------------------------- probe -----------------------------------------------

async def probe(client: httpx.AsyncClient, *, limit: int = 3, oldest: bool = False) -> None:
    """Fetch a few auctions and print them raw, storing NOTHING.

    `oldest=True` walks from the OTHER end (sort=ASC), which is where the rows without a buyer live.

    ANSWERED 2026-08-03, and worth recording because the guess was wrong. The question was whether
    no-buyer rows are auctions that ENDED UNSOLD or ones still RUNNING. Neither hypothesis needed
    to be right: the epoch-timestamped rows turned out to be auction ids 1-63 on Torn staff accounts
    (Chedburn is the game's creator), i.e. SEED DATA from when the auction house was built in
    January 2013. Not live auctions, and not meaningful history.

    The feed contains NO active auctions at all — nothing carries a future timestamp, and the ASC
    end is 2013 rather than today. So it really is a completed-events feed.

    Genuinely ended-unsold auctions DO exist (ids 8/9/10: real 2013 dates, 0 bids, null price, no
    buyer) but are vanishingly rare — at most 293 across 553,907 rows, 0.05%. Modern Torn evidently
    does not publish expired auctions. That is why seller-side inference stays unmeasured: not
    because the idea is wrong, but because there is no data to calibrate it with.

    Kept as a tool because the same question will come up for any new field, and the answer should
    come from a request rather than from the spec.
    """
    raw = await torn.get_auctionhouse(client, limit=limit, sort="ASC" if oldest else "DESC")
    if raw is None:
        print("request failed — see the log")
        return
    print(json.dumps(raw, indent=2))

    now = int(time.time())
    no_buyer = [r for r in raw if not (r.get("buyer") or {}).get("id")]
    print(f"\n--- {len(raw)} row(s), {len(no_buyer)} without a buyer ---")
    for row in no_buyer:
        ts = row.get("timestamp")
        if ts is None:
            verdict = "no timestamp at all"
        elif ts > now:
            verdict = f"FUTURE ({(ts - now) // 3600}h away) -> this auction is STILL RUNNING"
        elif ts < config.AH_MIN_TIMESTAMP:
            verdict = ("epoch-adjacent -> 2013 SEED DATA, not a real auction. Confirmed live: "
                       "these are auction ids 1-63 on Torn staff accounts, from when the AH was "
                       "built. Correctly dropped")
        else:
            verdict = "a real past date -> this auction genuinely ENDED UNSOLD"
        print(f"  auction {row.get('id')}: timestamp={ts} bids={row.get('bids')} "
              f"price={row.get('price')}  {verdict}")
    if not no_buyer:
        print("  (none on this page — try --oldest, which is where they sort)")
    print("\n--- normalised ---")
    for row in raw:
        print(json.dumps(normalize(row), indent=2))


# --------------------------------- orchestration ---------------------------------------

async def run(mode: str, *, limit: Optional[int] = None, max_pages: Optional[int] = None,
              max_seconds: Optional[float] = None, restart: bool = False) -> Dict:
    """One scrape run, wrapped in an ah_runs record.

    The run log exists because nobody watches a scraper. Alerts stopping is noticed within minutes;
    a cron job that has been failing for three weeks is not, unless "when did this last succeed"
    is answerable after the fact.
    """
    run_id = await ah_db.start_run(mode)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient() as client:
            if mode == "forward":
                result = await scrape_forward(client, limit=limit, max_pages=max_pages)
            elif mode == "backfill":
                result = await scrape_backfill(client, limit=limit, max_pages=max_pages,
                                               max_seconds=max_seconds, restart=restart)
            else:
                raise ValueError(f"unknown scrape mode {mode!r}")
    except Exception as exc:
        await ah_db.finish_run(run_id, pages=0, rows_seen=0, rows_new=0, error=repr(exc))
        raise
    await ah_db.finish_run(
        run_id, pages=result.get("pages", 0), rows_seen=result.get("seen", 0),
        rows_new=result.get("new", 0),
    )
    await ah_db.prune_runs()
    result["elapsed"] = round(time.monotonic() - started, 2)
    return result


def _fmt_ts(ts: Optional[int]) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) + "Z" if ts else "-"


async def show_status() -> None:
    s = await ah_db.stats()
    span_days = ((s["newest"] - s["oldest"]) / 86400) if s["oldest"] and s["newest"] else 0
    print(f"ledger:      {ah_db.db_path()}")
    print(f"auctions:    {s['rows']:,}  ({s['with_uid']:,} with a uid, {s['buyers']:,} distinct buyers, "
          f"{s['unsold'] or 0:,} ended unsold)")
    print(f"covering:    {_fmt_ts(s['oldest'])}  ->  {_fmt_ts(s['newest'])}   ({span_days:.1f} days)")
    print(f"backfill:    {'complete' if s['backfill_complete'] else 'incomplete'}")
    print("recent runs:")
    for r in s["last_runs"]:
        state = r["error"] or ("ok" if r["ended_at"] else "DIED MID-RUN")
        print(f"  {_fmt_ts(r['started_at'])}  {r['mode']:<8} pages={r['pages']:<3} "
              f"new={r['rows_new']:<5} {state}")

    bad = await ah_db.count_undatable(config.AH_MIN_TIMESTAMP)
    if bad:
        print()
        print(f"WARNING: {bad:,} row(s) have a timestamp before {_fmt_ts(config.AH_MIN_TIMESTAMP)}.")
        print("  Torn serves some auctions with an epoch-adjacent date. They are dropped at ingest")
        print("  now, but rows stored before that guard existed still break MIN(ended_at) — which is")
        print("  the backfill's own floor, and the 'covering' span above. Remove them with:")
        print(f"    sqlite3 {ah_db.db_path()} "
              f"'DELETE FROM auction_wins WHERE ended_at < {config.AH_MIN_TIMESTAMP};'")

    years = await ah_db.auctions_by_year(config.AH_MIN_TIMESTAMP)
    if len(years) > 1:
        print()
        print("auctions per year — a year with none between two with plenty means the backfill")
        print("skipped ground. 'complete' does not rule that out; only this does.")
        peak = max(c for _, c in years) or 1
        seen_years = {y for y, _ in years}
        for year in range(min(seen_years), max(seen_years) + 1):
            count = dict(years).get(year, 0)
            bar = "#" * int(40 * count / peak)
            flag = "   <-- GAP" if count == 0 else ""
            print(f"  {year}  {count:>8,}  {bar}{flag}")


def confidence_for_age(age_days: float, source: str = "buyer") -> Optional[float]:
    """Measured P(the inferred owner is still the seller) for evidence this old.

    A lookup into the band table for that SOURCE, which comes from `calibrate` against
    directly-observed sellers — not from judgement. A step function rather than interpolated: the
    underlying numbers are bucket accuracies with real sample sizes, and smoothing between them
    would invent precision the measurement does not have.

    Returns None when the source has no band table, i.e. it has not been measured. Callers must
    treat that as "do not use", never as "assume the buyer numbers apply".
    """
    bands = config.AH_CONFIDENCE_BANDS if source == "buyer" else config.AH_SELLER_CONFIDENCE_BANDS
    if not bands:
        return None
    for max_age, accuracy in bands:
        if max_age is None or age_days <= max_age:
            return accuracy
    return bands[-1][1]


async def run_match() -> Dict:
    """Resolve every un-attributed uid we can against the ledger, into the main database.

    Runs after each `forward` scrape. Precomputed rather than looked up live because reconcile_gear
    runs per item per poll on the connection the alert path queues on — a cross-database read there
    would put the ledger on the hot path for no benefit, since AH data only changes when this runs.

    Recomputed wholesale each time rather than incrementally. The candidate set is small (thousands),
    the ledger only moves when the scrape does, and a full recompute means the invalidation rule can
    never drift: a uid that has since been attributed directly simply is not in the input, so its
    inferred row is dropped.
    """
    from server import db

    anon = await db.get_anon_uid_set()
    cur = await db._db.execute("SELECT uid FROM unmatched_gear")
    unmatched = {r[0] for r in await cur.fetchall()}
    candidates = set(anon) | unmatched

    if not candidates:
        result = await db.replace_anon_matches([], candidates)
        log.info("ah match: no un-attributed uids")
        return {"candidates": 0, **result}

    # Unsold auctions are only considered once seller-side inference has been calibrated. While
    # AH_SELLER_CONFIDENCE_BANDS is None they are excluded from the lookup entirely, so an unsold
    # row cannot become the "newest" observation and mask a real sale beneath it.
    use_seller = bool(config.AH_SELLER_CONFIDENCE_BANDS)
    wins = await ah_db.wins_for_uids(candidates, include_unsold=use_seller)
    now = int(time.time())
    rows, skipped = [], 0
    for uid, win in wins.items():
        owner = ah_db.inferred_owner(win)
        if owner is None:
            skipped += 1
            continue
        owner_id, source = owner
        age_days = max(0, (now - win["ended_at"]) // 86400)
        confidence = confidence_for_age(age_days, source)
        if confidence is None:                  # unmeasured source — not usable, by design
            skipped += 1
            continue
        rows.append({
            "uid": uid, "buyer_id": owner_id,
            "buyer_name": win["buyer_name"] if source == "buyer" else win["seller_name"],
            "auction_id": win["auction_id"], "price": win["price"], "won_at": win["ended_at"],
            "age_days": age_days, "confidence": confidence, "source": source,
        })

    result = await db.replace_anon_matches(rows, candidates)
    high = sum(1 for r in rows if r["confidence"] >= config.AH_CONFIDENCE_BANDS[1][1])
    by_source = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    log.info("ah match: %d of %d un-attributed uid(s) resolved (%d high-confidence, by source %s), "
             "%d dropped", len(rows), len(candidates), high, by_source or "-", result["dropped"])
    return {"candidates": len(candidates), "resolved": len(rows), "high_confidence": high,
            "by_source": by_source, **result}


async def show_coverage() -> None:
    """How much of the anonymous backlog the ledger can actually de-anonymise.

    This is the go/no-go number for #12. That feature is worth building in proportion to how many of
    the ~1,600 stashed anon uids the AH ledger can put a name to — and that is a measurement, not a
    guess. Reads both databases and joins nothing permanently; it is a report, not a pipeline.
    """
    from server import db

    anon = await db.get_anon_uid_set()
    cur = await db._db.execute("SELECT uid FROM unmatched_gear")
    unmatched = {r[0] for r in await cur.fetchall()}
    cur = await db._db.execute("SELECT uid FROM tracked_gear WHERE seller_id IS NOT NULL")
    tracked = {r[0] for r in await cur.fetchall()}

    total = await ah_db.count_auctions()
    print(f"ledger:           {total:,} auctions, back to {_fmt_ts(await ah_db.oldest_ended_at())}")
    print()

    for label, uids, note in (
        ("anon uids", anon, "the dead end #12 exists to open up"),
        ("unmatched gear", unmatched, "on the market, no seller captured yet"),
        ("tracked gear", tracked, "already attributed — a cross-check, not a target"),
    ):
        if not uids:
            print(f"{label:<16} 0 — nothing to match")
            continue
        wins = await ah_db.wins_for_uids(uids)
        pct = 100.0 * len(wins) / len(uids)
        print(f"{label:<16} {len(wins):,} of {len(uids):,} resolve ({pct:.1f}%)   {note}")
        if wins:
            now = int(time.time())
            ages = sorted((now - w["ended_at"]) / 86400 for w in wins.values())
            fresh = sum(1 for a in ages if a <= 7)
            print(f"{'':<16}   evidence age: median {ages[len(ages) // 2]:.0f}d, "
                  f"{fresh:,} within a week, oldest {ages[-1]:.0f}d")

    print()
    turnover = await ah_db.uid_turnover()
    repeats = sum(v for k, v in turnover.items() if k != "seen_1x")
    print(f"uid turnover:     {turnover}")
    print(f"                  {repeats:,} uid(s) auctioned more than once", end="")
    median = await ah_db.median_days_between_wins()
    print(f", median {median}d apart" if median is not None else "")
    print()
    print("Read the anon line as the ceiling on #12's backlog: an anon uid with no AH win behaves")
    print("exactly as it does today. The turnover figures set how stale a win can be and still be")
    print("trusted — a uid that changes hands every few days cannot carry weeks-old evidence.")


# Evidence-age buckets, in days. Chosen to straddle the ranges where the answer plausibly changes:
# within a week, within a month, within a quarter, older. Not tuned to any result.
_AGE_BUCKETS = ((0, 1), (1, 7), (7, 30), (30, 90), (90, 365), (365, None))


def _bucket_label(lo, hi) -> str:
    return f"{lo}-{hi}d" if hi is not None else f"{lo}d+"


async def show_calibration() -> None:
    """How often AH-inferred ownership would have been RIGHT, by evidence age AND by source.

    The auction house is a lossy observer: a uid also changes hands by trade, bazaar and item
    market, none of which it sees. So an AH row is a decaying estimate of current ownership, and the
    decay rate is what any confidence model has to be built on.

    TWO SOURCES, MEASURED SEPARATELY:
      buyer   a completed sale — the buyer held it
      seller  an auction that ENDED UNSOLD — the seller kept it

    Equally strong at the moment they happen, but differently selected afterwards. Someone whose
    auction failed is much more likely than a random owner to relist, including anonymously, which
    is exactly the population being resolved — so the seller curve could plausibly be BETTER. It
    could also be worse, if a failed auction mostly means the item went somewhere we cannot see.
    Sharing one set of bands between them would hide whichever it is.

    Measured retroactively against sellers we directly observed. No feedback loop, no waiting.
    """
    from server import db

    observations = await db.observed_gear_owners()
    if not observations:
        print("no directly-observed gear owners yet — nothing to calibrate against.")
        print("this fills in as the poller detects gear sales (sales rows with a 'uid:' listing_id).")
        return

    wins = await ah_db.all_wins_for_uids({o["uid"] for o in observations})
    buckets = {src: {b: [0, 0] for b in _AGE_BUCKETS} for src in ("buyer", "seller")}
    unknown = 0

    for obs in observations:
        history = wins.get(obs["uid"])
        win = ah_db.win_as_of(history, obs["observed_at"]) if history else None
        owner = ah_db.inferred_owner(win)
        if owner is None:
            unknown += 1                                  # no prior row, or nothing inferable
            continue
        owner_id, source = owner
        age_days = (obs["observed_at"] - win["ended_at"]) / 86400
        for bucket in _AGE_BUCKETS:
            lo, hi = bucket
            if age_days >= lo and (hi is None or age_days < hi):
                buckets[source][bucket][0] += 1
                buckets[source][bucket][1] += int(owner_id == obs["seller_id"])
                break

    print("Calibration: would the AH-inferred owner have been the actual seller?")
    print(f"({len(observations):,} direct observations; {unknown:,} had no prior AH row to test)")

    grand_checked = 0
    for source, label in (("buyer", "BUYER of a completed sale"),
                          ("seller", "SELLER of an auction that ended UNSOLD")):
        checked_total = sum(c for c, _ in buckets[source].values())
        print()
        print(f"  {label}")
        if not checked_total:
            if source == "seller":
                print("    no data — the ledger holds no unsold auctions. They were dropped at")
                print("    ingest before buyer_id became nullable; `backfill --restart` recovers")
                print("    them. Until then seller-side inference stays unmeasured, and unused.")
            else:
                print("    no data")
            continue
        grand_checked += checked_total
        print(f"    {'evidence age':<14}{'checked':>9}{'agreed':>9}{'accuracy':>11}")
        agreed_total = 0
        for bucket in _AGE_BUCKETS:
            checked, agreed = buckets[source][bucket]
            agreed_total += agreed
            if not checked:
                continue
            print(f"    {_bucket_label(*bucket):<14}{checked:>9,}{agreed:>9,}"
                  f"{100.0 * agreed / checked:>10.1f}%")
        print(f"    {'overall':<14}{checked_total:>9,}{agreed_total:>9,}"
              f"{100.0 * agreed_total / checked_total:>10.1f}%")
        if checked_total < 50:
            print(f"    NOT ENOUGH DATA — {checked_total} comparisons. A smoke test, not a")
            print("    calibration; setting a threshold from this would be setting it from noise.")

    print()
    if grand_checked >= 50:
        print("Set AH_CONFIDENCE_BANDS from the buyer table. Only fill AH_SELLER_CONFIDENCE_BANDS")
        print("once the seller table has real numbers — leaving it None keeps seller-side")
        print("inference switched off, which is the correct state while it is unmeasured.")
    print("One caveat on the sample: it only covers uids that reached the ITEM MARKET, because that")
    print("is the only place we observe a seller. A uid won at auction and kept forever never")
    print("appears here — which is the right bias, since we only ever infer for uids we can see")
    print("listed, but it does mean these numbers describe items that move.")


async def cli(argv=None) -> int:
    """Entry point. Assumes .env is already loaded — see ah_scrape.py at the repo root."""
    from server import db
    from server import keys as keypool

    p = argparse.ArgumentParser(prog="ah_scrape", description="Torn auction house scraper")
    p.add_argument("mode",
                   choices=["forward", "backfill", "probe", "status", "coverage", "calibrate",
                            "match"],
                   nargs="?", default="forward")
    p.add_argument("--no-match", action="store_true",
                   help="forward: skip the match step that normally follows the scrape")
    p.add_argument("--limit", type=int, default=None, help="rows per request (max 100)")
    p.add_argument("--max-pages", type=int, default=None, help="cap requests for this run")
    p.add_argument("--oldest", action="store_true",
                   help="probe: fetch from the OLDEST end (sort=ASC), where rows without a "
                        "buyer live — the ones that are either unsold or still running")
    p.add_argument("--restart", action="store_true",
                   help="backfill: clear the completed flag and re-walk from the newest end. "
                        "Inserts are idempotent, so this costs time and nothing else.")
    p.add_argument("--max-seconds", type=float, default=None,
                   help="wall-clock budget for a backfill run; 0 = unbounded (default %s)"
                        % config.AH_MAX_SECONDS_PER_RUN)
    args = p.parse_args(argv)

    await ah_db.init_db()
    try:
        if args.mode == "status":
            await show_status()
            return 0

        if args.mode in ("coverage", "calibrate", "match"):
            # Need the main database too. coverage/calibrate only read it; match writes the
            # resolved rows. No keys, no API calls either way.
            await db.init_db()
            try:
                if args.mode == "coverage":
                    await show_coverage()
                elif args.mode == "calibrate":
                    await show_calibration()
                else:
                    print(json.dumps(await run_match()))
            finally:
                await db.close_db()
            return 0

        # API keys are managed through /keys and live in the MAIN database, so the scraper opens it
        # too — read-mostly, briefly, and under WAL, so it does not block the running bot.
        await db.init_db()
        try:
            await db.seed_api_keys_from_env(config.TORN_API_KEYS)
            await keypool.refresh()
            if keypool.active_count() == 0:
                print("no active Torn API keys — add one with /keys add, or set TORN_API_KEYS")
                return 2
            if args.mode == "probe":
                async with httpx.AsyncClient() as client:
                    await probe(client, limit=args.limit or 3, oldest=args.oldest)
                return 0
            result = await run(args.mode, limit=args.limit, max_pages=args.max_pages,
                               max_seconds=args.max_seconds, restart=args.restart)
            # New auctions are only useful once they have been resolved against the un-attributed
            # uids, so the scrape and the match are one operation by default rather than two cron
            # lines that can drift apart.
            if args.mode == "forward" and not args.no_match:
                await db.init_db()
                try:
                    result["match"] = await run_match()
                finally:
                    await db.close_db()
            print(json.dumps(result))
        finally:
            await db.close_db()
    finally:
        await ah_db.close_db()
    return 0
