"""
Auction house scraper — pulls completed auctions into the AH ledger (server/ah_db.py).

Runs as a cron process, not inside the bot. Two modes:

  forward   keep up with newly-completed auctions. This is the cron job.
  backfill  walk backwards through history until Torn stops serving rows. Run manually, or on a
            slow cron until it reports complete.

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

    Rows without a buyer are dropped rather than stored. The spec marks `buyer` required, but an
    auction that expired unsold has nobody to attribute the item to, and specs are routinely
    optimistic about this — a row with a null buyer would otherwise become a NOT NULL failure that
    aborts a whole page mid-scrape.
    """
    buyer = row.get("buyer") or {}
    buyer_id = buyer.get("id")
    auction_id = row.get("id")
    ended_at = row.get("timestamp")
    if not buyer_id or not auction_id or not ended_at:
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
        "uid": None if uid in (None, "") else str(uid),
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
        "buyer_id": int(buyer_id),
        "buyer_name": buyer.get("name"),
        "price": row.get("price"),
        "bids": row.get("bids"),
        "ended_at": int(ended_at),
    }


def _timestamps(raw: List[Dict]) -> List[int]:
    """End timestamps from the RAW page, not the normalised one.

    The cursor has to advance over rows we chose not to store (an unsold auction, say) as well as
    ones we kept. Deriving it from the normalised rows would stall the walk on any page whose
    boundary row was dropped, and re-request that page forever.
    """
    return [int(r["timestamp"]) for r in raw if r.get("timestamp")]


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
                          max_pages: Optional[int] = None,
                          max_seconds: Optional[float] = None) -> Dict:
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

    if await ah_db.get_state("backfill_complete", False):
        floor = await ah_db.oldest_ended_at()
        log.info("ah backfill: already complete (history reaches back to %s)", _fmt_ts(floor))
        return {"mode": "backfill", "pages": 0, "seen": 0, "stored": 0, "new": 0, "complete": True}

    to_ts = await ah_db.oldest_ended_at()
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

async def probe(client: httpx.AsyncClient, *, limit: int = 3) -> None:
    """Fetch a few auctions and print them raw, storing NOTHING.

    Two things the OpenAPI spec cannot tell us and only a live response can: whether an auction that
    expired unsold really does carry a buyer, and whether these endpoints return a `cache_timestamp`
    the way itemmarket does (there is no such field in the schema, which is why the scraper runs on
    a plain interval instead of cache-timed scheduling).
    """
    raw = await torn.get_auctionhouse(client, limit=limit, sort="DESC")
    if raw is None:
        print("request failed — see the log")
        return
    print(json.dumps(raw, indent=2))
    print(f"\n--- {len(raw)} row(s); normalised ---")
    for row in raw:
        print(json.dumps(normalize(row), indent=2))


# --------------------------------- orchestration ---------------------------------------

async def run(mode: str, *, limit: Optional[int] = None, max_pages: Optional[int] = None,
              max_seconds: Optional[float] = None) -> Dict:
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
                                               max_seconds=max_seconds)
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
    print(f"auctions:    {s['rows']:,}  ({s['with_uid']:,} with a uid, {s['buyers']:,} distinct buyers)")
    print(f"covering:    {_fmt_ts(s['oldest'])}  ->  {_fmt_ts(s['newest'])}   ({span_days:.1f} days)")
    print(f"backfill:    {'complete' if s['backfill_complete'] else 'incomplete'}")
    print("recent runs:")
    for r in s["last_runs"]:
        state = r["error"] or ("ok" if r["ended_at"] else "DIED MID-RUN")
        print(f"  {_fmt_ts(r['started_at'])}  {r['mode']:<8} pages={r['pages']:<3} "
              f"new={r['rows_new']:<5} {state}")


async def cli(argv=None) -> int:
    """Entry point. Assumes .env is already loaded — see ah_scrape.py at the repo root."""
    from server import db
    from server import keys as keypool

    p = argparse.ArgumentParser(prog="ah_scrape", description="Torn auction house scraper")
    p.add_argument("mode", choices=["forward", "backfill", "probe", "status"], nargs="?",
                   default="forward")
    p.add_argument("--limit", type=int, default=None, help="rows per request (max 100)")
    p.add_argument("--max-pages", type=int, default=None, help="cap requests for this run")
    p.add_argument("--max-seconds", type=float, default=None,
                   help="wall-clock budget for a backfill run; 0 = unbounded (default %s)"
                        % config.AH_MAX_SECONDS_PER_RUN)
    args = p.parse_args(argv)

    await ah_db.init_db()
    try:
        if args.mode == "status":
            await show_status()
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
                    await probe(client, limit=args.limit or 3)
                return 0
            result = await run(args.mode, limit=args.limit, max_pages=args.max_pages,
                               max_seconds=args.max_seconds)
            print(json.dumps(result))
        finally:
            await db.close_db()
    finally:
        await ah_db.close_db()
    return 0
