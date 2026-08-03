"""
Torn API v2 client for SELLER ENRICHMENT only (the market data itself comes from the
WebSocket, never the API — see README/plan).

On first sight of a new seller (and refreshed as needed) we call:
  - get_job(id)     -> clothing-store detection: job.type_id == 5, rating >= 7 => 75% reduction
  - get_basic(id)   -> status (state + until) for the hospital-accumulation feature
  - get_profile(id) -> fuller profile (faction, last_action, property) for the embed

There is no single endpoint with both status and job, so clothing + status are two calls.
Keys rotate across config.TORN_API_KEYS (advancing each second) to spread the ~100 req/min
per-key budget, mirroring the war-room rotation.
"""

import logging
import time
from typing import Dict, List, Optional

import httpx

import config
from server import keys as keypool

log = logging.getLogger("mug.torn")

BASE = "https://api.torn.com/v2"
V1_BASE = "https://api.torn.com"
CLOTHING_STORE_TYPE_ID = 5
CLOTHING_STORE_MIN_RATING = 7   # 7-star+ clothing store => 75% mug reduction


def _next_key() -> Optional[str]:
    return keypool.next_key()


async def _get(client: httpx.AsyncClient, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
    """GET a Torn v2 path with the active key rotation. On a KEY-specific failure (rate-limit, bad
    key, or a network blip) we immediately retry on a DIFFERENT key, up to API_MAX_KEY_RETRIES.
    Torn-SIDE errors (backend/IP/maintenance, and HTTP 5xx) are NOT retried — another key from the
    same IP won't help — so we return None and let the caller back off. Returns the JSON or None."""
    tried: set = set()
    attempts = max(1, min(config.API_MAX_KEY_RETRIES, keypool.active_count() or 1))
    for _ in range(attempts):
        key = _next_key_excluding(tried)
        if not key:
            if not tried:
                log.warning("no active Torn API keys — add one with /keys add")
            break
        tried.add(key)
        # Sending ANY `timestamp` (ms OR sec) makes Torn serve a ~30s-fresher cache than OMITTING it
        # (confirmed by experiment — ms_comment == sec_comment == sec_only, only "none" was stale).
        # The value/unit is irrelevant; we send seconds as the correct unit. Don't drop it.
        q = {"key": key, "comment": config.API_COMMENT, "timestamp": int(time.time())}
        if params:
            q.update(params)
        try:
            resp = await client.get(f"{BASE}/{path}", params=q, timeout=15,
                                     headers={"User-Agent": config.HTTP_USER_AGENT})
        except httpx.HTTPError as e:
            log.warning("torn GET %s network error (key %s): %s -> retrying next key", path, _mask(key), e)
            continue   # transient network/connection issue — a different key/connection may work
        if resp.status_code >= 500:
            log.warning("torn GET %s HTTP %s (Torn-side) -> not retrying", path, resp.status_code)
            return None   # Torn backend (e.g. the midnight 504s) — hammering other keys won't help
        try:
            data = resp.json()
        except ValueError:
            log.warning("torn GET %s returned non-JSON (HTTP %s) -> not retrying", path, resp.status_code)
            return None
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            code = err.get("code")
            if code == 5:                                              # too many requests for THIS key
                keypool.mark_rate_limited(key)
                log.info("torn %s rate-limited (code 5) key %s -> retrying next key", path, _mask(key))
                continue
            if code in keypool.KEY_ERROR_CODES:                        # this key is bad: disable + notify
                await keypool.mark_key_error(key, code, err.get("error"))
                log.warning("torn %s key error (code %s: %s) key %s -> retrying next key",
                            path, code, err.get("error"), _mask(key))
                continue
            if code in keypool.TORN_SIDE_CODES:                        # Torn-side/IP: don't retry-spam
                log.warning("torn %s Torn-side error (code %s: %s) -> not retrying",
                            path, code, err.get("error"))
                return None
            log.warning("torn %s API error (code %s: %s)", path, code, err.get("error"))
            return None
        return data
    log.warning("torn GET %s: exhausted %d key(s) without success", path, len(tried))
    return None


def _next_key_excluding(exclude: set) -> Optional[str]:
    return keypool.next_key(exclude=exclude)


def _mask(key: Optional[str]) -> str:
    return f"…{key[-4:]}" if key else "?"


async def get_itemmarket(
    client: httpx.AsyncClient, item_id: int, *, bonus: Optional[str] = None, offset: int = 0
) -> Optional[Dict]:
    """One page (<=100) of an item's market. Returns {"item": {...meta...}, "listings": [...]}
    or None on error. Gear listings carry item_details.uid + stats; stackable items return only
    {price, amount}. `bonus="Any"` filters gear to bonus-only listings. item.type is one of
    Primary/Secondary/Melee/Defensive (gear) or a consumable category."""
    params = {"limit": 100, "offset": offset}
    if bonus:
        params["bonus"] = bonus
    data = await _get(client, f"market/{item_id}/itemmarket", params)
    if not data:
        return None
    im = data.get("itemmarket") or {}
    # cache_timestamp = when Torn generated this cached snapshot (unix). It increments in exact 30s
    # steps and the snapshot only becomes visible ~30s after that stamp, so it drives adaptive
    # poll scheduling in api_poller (poll just after the NEXT snapshot is due).
    return {"item": im.get("item") or {}, "listings": im.get("listings") or [],
            "cache_timestamp": im.get("cache_timestamp")}


async def get_all_item_values(client: httpx.AsyncClient) -> Optional[Dict]:
    """One call returning every item's market value (v1 `torn?selections=items`). Returns
    {item_id: {"name": str, "market_value": int}} or None on error. Used by the deal-finder;
    refreshed every few hours so one cheap call covers the whole catalogue."""
    key = _next_key()
    if not key:
        log.warning("no active Torn API keys for item-values fetch")
        return None
    try:
        resp = await client.get(
            f"{V1_BASE}/torn/",
            params={"selections": "items", "key": key, "comment": config.API_COMMENT},
            timeout=30,
        )
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("torn items fetch failed: %s", e)
        return None
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        code = err.get("code")
        if code == 5:
            keypool.mark_rate_limited(key)
        elif code in keypool.KEY_ERROR_CODES:
            await keypool.mark_key_error(key, code, err.get("error"))
        else:
            log.warning("torn items API error: %s", err)
        return None
    items = (data or {}).get("items") or {}
    out: Dict[int, Dict] = {}
    for k, v in items.items():
        try:
            iid = int(k)
        except (TypeError, ValueError):
            continue
        mv = v.get("market_value")
        if mv:   # skip items with no/0 market value
            out[iid] = {"name": v.get("name"), "market_value": int(mv), "type": v.get("type")}
    return out


async def get_auctionhouse(
    client: httpx.AsyncClient,
    *,
    item_id: Optional[int] = None,
    limit: int = 100,
    sort: str = "DESC",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> Optional[List[Dict]]:
    """One page of COMPLETED auctions, newest-first by default.

    Torn's `timestamp` on an auction is when it ENDED, and every row carries a buyer — this is a
    settled-sales feed, not live listings. `from`/`to` bound that timestamp, `limit` caps at 100.

    Returns the raw list, or None on error. Empty list and None are NOT the same thing: the scraper
    treats an empty page as "we have reached the end of what Torn will serve" and stops walking, so
    conflating it with a transient API failure would permanently truncate the backfill.
    """
    params: Dict = {"limit": max(1, min(100, limit)), "sort": sort}
    if from_ts is not None:
        params["from"] = int(from_ts)
    if to_ts is not None:
        params["to"] = int(to_ts)
    path = f"market/{item_id}/auctionhouse" if item_id else "market/auctionhouse"
    data = await _get(client, path, params)
    if data is None:
        return None
    rows = data.get("auctionhouse")
    return rows if isinstance(rows, list) else []


async def get_job(client: httpx.AsyncClient, user_id: int) -> Optional[Dict]:
    data = await _get(client, f"user/{user_id}/job")
    return data.get("job") if data else None


async def get_basic(client: httpx.AsyncClient, user_id: int) -> Optional[Dict]:
    data = await _get(client, f"user/{user_id}/basic")
    return data.get("profile") if data else None


async def get_profile(client: httpx.AsyncClient, user_id: int) -> Optional[Dict]:
    data = await _get(client, f"user/{user_id}/profile")
    return data.get("profile") if data else None


def is_clothing_store(job: Optional[Dict]) -> bool:
    """True if the seller's job grants the 7*+ clothing-store mug reduction."""
    if not job:
        return False
    return (
        job.get("type_id") == CLOTHING_STORE_TYPE_ID
        and (job.get("rating") or 0) >= CLOTHING_STORE_MIN_RATING
    )
