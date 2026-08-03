"""
FastAPI ingest app — receives the hardened userscripts' POSTs and serves the gear caches.

Phase 0 focus: capture everything. Each ingest payload is appended to a dated JSONL under
scratch/dumps/ AND stored in the raw_events table, so we can inspect the real WebSocket /
gear schema before writing a parser. Status heartbeats update monitored_pages immediately
so status-tracker works from day one. Sale detection + mug alerts are wired in Phase 1
(see the markers in /market).
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
from server import ah_db, api_access, db, detection

log = logging.getLogger("mug.server")

DUMP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scratch", "dumps")


def _page_label(token: Any) -> Optional[str]:
    """Map a /status token (item id or category name) to a human label."""
    if isinstance(token, int):
        return config.ITEM_MAP.get(token, str(token))
    s = str(token)
    if s.isdigit():
        return config.ITEM_MAP.get(int(s), s)
    return s


async def _store_gear(body: Any, *, anonymous: bool) -> None:
    """Store an item-matcher gear capture as the seller map. Tolerates both the main /gear
    shape (item_name/damage_armor/accuracy/lister/seller_name) and the /gear/anon shape
    (itemName/stats/user). seller_id None => anonymous (kept for the cache, never mugged)."""
    if not isinstance(body, dict):
        return
    listing_id = body.get("listingID") or body.get("listing_id")
    if listing_id is None:
        return
    item_id = body.get("item_id") or body.get("itemId")
    item_name = body.get("item_name") or body.get("itemName")
    price = body.get("price")
    # Prefer the API uid (exact join key) if the item-matcher captured it; else the armouryID.
    armoury_uid = body.get("uid") or body.get("armouryID") or body.get("armoury_uid")
    stats = body.get("stats") or {"damage": body.get("damage_armor"), "accuracy": body.get("accuracy")}

    seller_id = None
    seller_name = None
    if not anonymous:
        raw = body.get("lister")
        if raw is None:
            raw = (body.get("user") or {}).get("ID")
        if raw is not None and str(raw).isdigit():
            seller_id = int(raw) or None
        seller_name = body.get("seller_name") or (body.get("user") or {}).get("name")

    if seller_id:
        await db.upsert_seller_seen(user_id=seller_id, name=seller_name)
    await db.store_gear_listing(
        listing_id=listing_id, item_id=item_id, item_name=item_name, seller_id=seller_id,
        price=price, stats_json=json.dumps(stats) if stats else None,
        armoury_uid=str(armoury_uid) if armoury_uid else None,
    )
    if config.DEBUG_GEAR:
        log.info("/gear stored: listing=%s uid=%s item_id=%s seller=%s price=%s anon=%s",
                 listing_id, armoury_uid, item_id, seller_id, price, anonymous)
    # Reconcile against the poller's unmatched/anon lists (matched by uid == armouryID).
    if armoury_uid:
        if anonymous:
            await db.mark_anon_uid(uid=armoury_uid, item_name=item_name,
                                   stat=stats.get("damage"), accuracy=stats.get("accuracy"), price=price)
        else:
            await db.remove_unmatched_gear(armoury_uid)   # captured a seller -> no longer unmatched
    elif config.DEBUG_GEAR:
        log.info("/gear stored WITHOUT armouryID: listing=%s item_id=%s seller=%s "
                 "(can't join to the poller -> will re-appear as unmatched)",
                 listing_id, item_id, seller_id)


async def _store_item(r: Any) -> None:
    """Store one stackable-item capture (seller ↔ item/price/quantity). Anonymous listings are
    stored too (seller_id NULL): they can't be mugged, but they occupy a market slot, so counting
    them keeps an item's "uncaptured" tally honest (anon listings aren't permanently 'missing')."""
    if not isinstance(r, dict):
        return
    listing_id = r.get("listingID") or r.get("listing_id")
    if listing_id is None:
        return
    item_id = r.get("item_id") or r.get("relatedItemID")
    if not await db.is_item_watched(item_id):
        return   # not on the (enabled) watch list -> discard; we don't auto-add stackables anymore
    raw = r.get("seller_id") or r.get("lister") or (r.get("user") or {}).get("ID")
    seller_id = int(raw) if raw is not None and str(raw).isdigit() else None
    if seller_id == 0:
        seller_id = None
    item_name = r.get("item_name") or (config.ITEM_MAP.get(item_id) if item_id else None)
    if seller_id:
        seller_name = r.get("seller_name") or (r.get("user") or {}).get("name")
        await db.upsert_seller_seen(user_id=seller_id, name=seller_name)
    await db.store_item_listing(
        listing_id=listing_id, item_id=item_id, item_name=item_name, seller_id=seller_id,
        price=r.get("price"), available=r.get("available") or r.get("amount") or 1,
    )


def create_app(bot=None) -> FastAPI:
    app = FastAPI(title="Mug Bot Ingest", version="0.1.0")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )
    if config.RAW_CAPTURE:
        os.makedirs(DUMP_DIR, exist_ok=True)

    def _write_dump(path: str, line: str) -> None:
        try:
            with open(path, "a") as f:
                f.write(line)
        except OSError as e:
            log.warning("dump write failed (%s): %s", path, e)

    async def _dump(source: str, payload: Any) -> None:
        """Append one payload to a dated JSONL, off the event loop.

        No-op unless MUG_RAW_CAPTURE is set: this is a Phase 0 discovery aid and the parser exists
        now, so it should not run — or grow — by default. When it IS on, the write goes through a
        thread: a blocking open()/write() inside an async handler stalls the whole loop, poller and
        in-flight alerts included, on every ingest POST.
        """
        if not config.RAW_CAPTURE:
            return
        path = os.path.join(DUMP_DIR, f"{source}-{time.strftime('%Y%m%d')}.jsonl")
        line = json.dumps({"_ts": int(time.time()), "payload": payload}) + "\n"
        await asyncio.to_thread(_write_dump, path, line)

    # ----------------------------- read API -------------------------------
    # Serves the AH ledger to the price-checker userscript. Behind a shared token, rate limited,
    # and every request logged with its source IP. Disabled entirely when no token is configured.

    async def _guard(request: Request, path: str):
        """Returns an error response, or None to proceed. Logs either way."""
        t0 = time.monotonic()
        if not api_access.enabled():
            api_access.record(request, path=path, status=503, detail="api-disabled")
            return JSONResponse({"error": "API disabled"}, status_code=503)
        if not api_access.authorise(request):
            api_access.record(request, path=path, status=401, detail="bad-token")
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if api_access.rate_limited(request):
            api_access.record(request, path=path, status=429, detail="rate-limited",
                              elapsed_ms=(time.monotonic() - t0) * 1000)
            return JSONResponse({"error": "rate limited"}, status_code=429)
        return None

    @app.post("/api/auctions/search")
    async def api_search(request: Request):
        t0 = time.monotonic()
        denied = await _guard(request, "/api/auctions/search")
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        try:
            result = await ah_db.search_auctions(body)
        except Exception:
            log.exception("auction search failed for %r", body)
            api_access.record(request, path="/api/auctions/search", status=500, detail="error",
                              elapsed_ms=(time.monotonic() - t0) * 1000)
            return JSONResponse({"error": "search failed"}, status_code=500)
        api_access.record(
            request, path="/api/auctions/search", status=200,
            detail=f"item={body.get('item_name')!r} total={result['total']} n={len(result['auctions'])}",
            elapsed_ms=(time.monotonic() - t0) * 1000,
        )
        return result

    @app.get("/api/auctions/meta")
    async def api_meta(request: Request):
        """Coverage, so a caller can tell how much history it is querying and how fresh it is."""
        t0 = time.monotonic()
        denied = await _guard(request, "/api/auctions/meta")
        if denied is not None:
            return denied
        meta = await ah_db.table_schema(ah_db.DATA_TABLE)
        api_access.record(request, path="/api/auctions/meta", status=200,
                          elapsed_ms=(time.monotonic() - t0) * 1000)
        return {"rows": meta["row_count"], "coverage": meta["coverage"],
                "max_limit": ah_db.SEARCH_MAX_LIMIT}

    @app.get("/health")
    async def health():
        return {"ok": True, "status": await db.status_snapshot()}

    @app.post("/market")
    async def market(request: Request):
        body = await request.json()
        channel = body.get("channel") if isinstance(body, dict) else None
        await _dump("market", body)
        if config.RAW_CAPTURE:
            await db.insert_raw_event("market", channel, body)
        if config.detection_ws_enabled():
            await detection.process_market_payload(body, bot)
        return {"ok": True}

    @app.post("/status")
    async def status(request: Request):
        body = await request.json()
        await _dump("status", body)
        for token in (body.get("tracked", []) if isinstance(body, dict) else []):
            await db.touch_page(token, _page_label(token))
        return {"ok": True}

    def _touch():
        if bot is not None:
            bot.request_status_refresh()

    @app.post("/gear")
    async def gear(request: Request):
        body = await request.json()
        await _dump("gear", body)
        await _store_gear(body, anonymous=False)
        _touch()
        return {"ok": True}

    @app.post("/gear/anon")
    async def gear_anon(request: Request):
        body = await request.json()
        await _dump("gear_anon", body)
        await _store_gear(body, anonymous=True)
        _touch()
        return {"ok": True}

    @app.post("/items")
    async def items(request: Request):
        body = await request.json()
        await _dump("items", body)
        rows = body.get("listings", []) if isinstance(body, dict) else (body if isinstance(body, list) else [])
        for r in rows:
            await _store_item(r)
        _touch()
        return {"ok": True, "stored": len(rows)}

    @app.get("/cache/uncached")
    async def cache_uncached():
        return {"listings": await db.get_uncached_for_matcher()}

    @app.get("/cache/gear")
    async def cache_gear():
        return {"listings": await db.get_gear_cache(anonymous=False)}

    @app.get("/cache/gear/anon")
    async def cache_gear_anon():
        return {"listings": await db.get_anon_gear_cache()}

    return app
