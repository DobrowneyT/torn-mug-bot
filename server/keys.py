"""
Dynamic Torn API key pool. The poller + enrichment rotate over the ENABLED keys in the api_keys
table (seeded from TORN_API_KEYS, managed via /keys). A key that returns a key-level error is
auto-disabled and queued as a status-channel alert.

Held in an in-memory cache (`_active`) refreshed from the DB, because torn._get's key pick is
synchronous and per-call.
"""

import logging
import time
from typing import Dict, List, Optional

import httpx

import config
from server import db

log = logging.getLogger("mug.keys")

# Torn error codes that mean the KEY is the problem (not a transient rate limit):
#   1 empty, 2 incorrect, 10 temp-disabled, 13 owner-inactive, 16 access-too-low, 18 paused.
KEY_ERROR_CODES = {1, 2, 10, 13, 16, 18}
# Torn error codes that are TORN-SIDE or IP-wide — retrying with a DIFFERENT key won't help (same
# IP / Torn's backend), so we don't retry-spam: 8 IP block, 9 API disabled (maintenance), 17 backend.
TORN_SIDE_CODES = {8, 9, 17}

_active: List[str] = []
_pending_alerts: List[Dict] = []   # drained by the bot loop -> posted to the status channel
_cooldowns: Dict[str, float] = {}  # key -> unix ts until which it is rate-limited (skipped)
_global_pause_until: float = 0.0   # all keys paused until this ts (optional)
_cursor: int = 0                   # round-robin position in _active (see next_key)

# Per-key budget. Torn allows ~100 requests/minute PER KEY; the pool previously discovered that
# limit only by being told (error 5), and each such error then parks the key for
# RATE_LIMIT_BACKOFF_SECONDS — shrinking the pool exactly when we are busiest. A token bucket lets
# us skip a key BEFORE overrunning it. Refill is continuous rather than in 60s steps: step refills
# produce a burst at each boundary, which is the behaviour we are trying to avoid.
_tokens: Dict[str, float] = {}     # key -> tokens currently available
_refilled_at: Dict[str, float] = {}


def _now() -> float:
    """Indirection so tests can drive the clock instead of sleeping."""
    return time.time()


def _mask(key: str) -> str:
    return f"…{key[-4:]}" if key else "?"


def _budget(key: str, now: float) -> float:
    """Tokens available for `key`, refilled for elapsed time. New keys start full."""
    cap = float(config.KEY_BUDGET_PER_MIN)
    if cap <= 0:
        return 1.0                                  # budget disabled
    last = _refilled_at.get(key)
    if last is None:
        _tokens[key] = cap
        _refilled_at[key] = now
        return cap
    tokens = min(cap, _tokens.get(key, cap) + (now - last) * cap / 60.0)
    _tokens[key] = tokens
    _refilled_at[key] = now
    return tokens


def _spend(key: str) -> None:
    if config.KEY_BUDGET_PER_MIN > 0:
        _tokens[key] = max(0.0, _tokens.get(key, 0.0) - 1.0)


def budget_remaining(key: str) -> int:
    """Whole tokens left for a key — surfaced by /keys list so pool sizing is visible."""
    return int(_budget(key, _now()))


def next_key(exclude=frozenset()) -> Optional[str]:
    """Hand out the next active key, round-robin, skipping any cooling down from a rate-limit (and
    any in `exclude`, used to pick a DIFFERENT key on an immediate retry). Returns None when paused
    globally or when every active key is cooling down / excluded.

    The position is a CALL COUNTER, not the clock. It used to be `available[int(now) % len(...)]`,
    which handed every call made within the same wall-clock second the same key — so a burst of
    concurrent polls (MAX_CONCURRENT_POLLS auto-sizes to the key count precisely so each in-flight
    call lands on a different key) all piled onto one key and burned its ~100 req/min budget while
    the others idled. Each rate-limit then costs that key RATE_LIMIT_BACKOFF_SECONDS of availability,
    which shows up as delayed sale detection.
    """
    global _cursor
    now = _now()
    if now < _global_pause_until:
        return None
    n = len(_active)
    if n == 0:
        return None
    starved = 0
    for offset in range(n):
        idx = (_cursor + offset) % n
        key = _active[idx]
        if _cooldowns.get(key, 0) > now or key in exclude:
            continue
        if _budget(key, now) < 1.0:
            starved += 1
            continue
        _spend(key)
        _cursor = (idx + 1) % n    # resume AFTER this key, so the next call moves on
        return key
    if starved:
        # Distinct from a rate-limit: we stopped ourselves rather than being told to. It means the
        # pool is too small for the watch list — add keys or trim it (see the status panel estimate).
        log.warning("all %d key(s) out of request budget (%s/min each) — pausing calls briefly",
                    starved, config.KEY_BUDGET_PER_MIN)
    return None


def mark_rate_limited(key: str) -> None:
    """Torn error 5: back the key off for RATE_LIMIT_BACKOFF_SECONDS (and optionally all keys).

    With the token bucket in place this should be rare — reaching it means our budget estimate is
    wrong, or another client is using the same key."""
    global _global_pause_until
    now = _now()
    _tokens[key] = 0.0            # Torn says it is spent; believe that over our own accounting
    _cooldowns[key] = now + config.RATE_LIMIT_BACKOFF_SECONDS
    if config.RATE_LIMIT_GLOBAL_PAUSE:
        _global_pause_until = now + config.RATE_LIMIT_BACKOFF_SECONDS
    log.warning(
        "rate-limited key %s — backing off %ss%s", _mask(key),
        config.RATE_LIMIT_BACKOFF_SECONDS, " (global pause)" if config.RATE_LIMIT_GLOBAL_PAUSE else "",
    )


def active_count() -> int:
    return len(_active)


async def refresh() -> None:
    """Reload the active pool from the DB and forget per-key state for keys that are gone.

    Without the cleanup, a removed or disabled key keeps its cooldown and token entries forever —
    a slow leak, and worse, a key re-enabled later would resume with whatever budget it had when it
    left rather than a clean slate."""
    global _active
    _active = await db.get_enabled_api_keys()
    live = set(_active)
    for store in (_cooldowns, _tokens, _refilled_at):
        for gone in [k for k in store if k not in live]:
            store.pop(gone, None)


async def validate_key(client: httpx.AsyncClient, key: str) -> Dict:
    """Confirm a key works for our use and identify its owner.
    Returns {ok, user_id, name, level, reason}."""
    # `code` is included on failure so the caller can tell a DEAD key (KEY_ERROR_CODES) from a
    # TRANSIENT one (rate-limit 5 / Torn-side 8,9,17 / network=None) and not disable a good key.
    try:
        r = await client.get(
            "https://api.torn.com/user/",
            params={"selections": "basic", "key": key, "comment": config.API_COMMENT}, timeout=15,
            headers={"User-Agent": config.HTTP_USER_AGENT},
        )
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "code": None, "reason": f"network error: {e}"}
    if data.get("error"):
        code = data["error"].get("code")
        return {"ok": False, "code": code, "reason": f"{data['error'].get('error')} (code {code})"}

    owner = {"user_id": data.get("player_id"), "name": data.get("name"), "level": data.get("level")}

    # Confirm it can actually read the item market (the call we depend on most).
    try:
        r2 = await client.get(
            "https://api.torn.com/v2/market/206/itemmarket",
            params={"key": key, "limit": 1, "comment": config.API_COMMENT}, timeout=15,
            headers={"User-Agent": config.HTTP_USER_AGENT},
        )
        d2 = r2.json()
        if isinstance(d2, dict) and d2.get("error"):
            code = d2["error"].get("code")
            return {"ok": False, "code": code,
                    "reason": f"insufficient access for item market: {d2['error'].get('error')}", **owner}
    except (httpx.HTTPError, ValueError):
        pass  # treat market-call hiccups as non-fatal; basic already validated the key
    return {"ok": True, "reason": "ok", **owner}


async def add_key(client: httpx.AsyncClient, key: str) -> Dict:
    res = await validate_key(client, key)
    if not res.get("ok"):
        return res
    await db.api_key_add(key=key, user_id=res.get("user_id"), name=res.get("name"), level=res.get("level"))
    await refresh()
    return res


async def remove_key(identifier) -> int:
    n = await db.api_key_remove(identifier)
    await refresh()
    return n


async def mark_key_error(key: str, code, message: str) -> None:
    """Disable a key that failed with a key-level error and queue a status-channel alert."""
    info = await db.api_key_info(key)
    if info and not info.get("enabled"):
        return  # already disabled — don't spam
    await db.api_key_mark_error(key, f"code {code}: {message}")
    await refresh()
    _pending_alerts.append({
        "name": (info or {}).get("name"), "user_id": (info or {}).get("user_id"),
        "code": code, "error": message, "remaining": len(_active),
    })
    log.warning("disabled API key (%s code %s) — %s active keys remain",
                (info or {}).get("name") or "?", code, len(_active))


async def validate_all(client: httpx.AsyncClient) -> None:
    """Background: fill owner info, and RE-ENABLE keys that now validate. Only DISABLE a key on a
    definitive key-level error (KEY_ERROR_CODES). A transient failure (rate-limit code 5, Torn-side
    8/9/17, or a network blip → code None) must NOT disable a good key — that mass-nuke is exactly
    what emptied the pool during a Torn backend wobble (every key returned code 5/17 at once)."""
    for row in await db.all_api_keys():
        res = await validate_key(client, row["key"])
        if res.get("ok"):
            # api_key_set_owner also re-enables, so a key wrongly disabled earlier recovers here.
            await db.api_key_set_owner(row["key"], res.get("user_id"), res.get("name"), res.get("level"))
        elif res.get("code") in KEY_ERROR_CODES:
            await db.api_key_mark_error(row["key"], res.get("reason", "validation failed"))
        else:
            log.warning("key %s validation deferred (transient: %s) — not disabling",
                        _mask(row["key"]), res.get("reason"))
    await refresh()


async def enable_keys(identifier=None) -> int:
    """Recovery: re-enable a disabled key (by full key / owner id / name) or ALL disabled keys when
    identifier is None, then refresh the in-memory pool. Returns how many rows were re-enabled."""
    n = await db.api_key_enable(identifier)
    await refresh()
    return n


def drain_alerts() -> List[Dict]:
    out = _pending_alerts[:]
    _pending_alerts.clear()
    return out
