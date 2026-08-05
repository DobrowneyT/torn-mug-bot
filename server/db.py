"""
Async SQLite access (aiosqlite). One shared connection for the process — the ingest
path is effectively single-writer, so a single connection in WAL mode is plenty.

Phase 0 uses raw_events (catch-all) + monitored_pages (status heartbeats). The listings/
sales/sellers helpers are scaffolded for Phase 1 detection + enrichment.

WRITES: every helper here ends in ``await _commit()`` rather than a bare ``_db.commit()``.
Inside an ``async with db.batch():`` block those are no-ops and ONE commit happens at the
end — see ``batch()`` for why that matters.
"""

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import aiosqlite

import config

log = logging.getLogger("mug.db")

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

# Runtime-tunable values mirrored into the `config` table on first run. (The mug % + clothing
# reduction are FIXED to match the reference bot, so they live in config.py, not here.)
_CONFIG_DEFAULTS = {
    "min_mug_value": config.MIN_MUG_VALUE,        # tuned live via /mugmin
    "sale_window_seconds": config.SALE_WINDOW_SECONDS,
    "item_deal_pct": config.ITEM_DEAL_PCT,
    "alert_cooldown_seconds": config.ALERT_COOLDOWN_SECONDS,
    "hospitalize_faction_ids": config.HOSPITALIZE_FACTION_IDS,   # /faction add|remove (live)
    "gear_all_bonus": config.GEAR_ALL_BONUS,                     # /gear allbonus add|remove (live)
    "delist_tag_seconds": config.DELIST_TAG_SECONDS,             # /delist seconds (live)
    "delist_mode": config.DELIST_MODE,                          # /delist mode: ping|silent|off (live)
    "hosp_mode": config.HOSP_MODE,                              # /hosplock mode: live|remind|hold (live)
    "hosp_lead_seconds": config.HOSPITAL_RELEASE_LEAD_SECONDS,   # /hosplock lead_minutes (live)
    "anonmatch_mode": config.ANONMATCH_MODE,                     # /anonmatch mode (live)
    "anonmatch_min_confidence": config.ANONMATCH_MIN_CONFIDENCE,  # /anonmatch min_confidence (live)
}

# Columns added after the initial schema; ALTER-ed onto existing DBs on startup so upgrades
# (which preserve the .db) don't need a manual migration.
_SELLER_FORWARD_COLS = {
    "online_status": "TEXT", "last_alerted_at": "INTEGER",
    "job_type_id": "INTEGER", "job_rating": "INTEGER", "job_company_name": "TEXT",
    "status_state": "TEXT", "status_until": "INTEGER", "status_description": "TEXT",
    "status_details": "TEXT",          # hospital reason (e.g. "Mugged by …" vs a medical reason)
    "status_checked_at": "INTEGER",
    "mug_claimed_until": "INTEGER",   # sales at/before this ts were already alerted (assumed mugged)
    "faction_id": "INTEGER",          # for the hospitalize-faction filter
}

_db: Optional[aiosqlite.Connection] = None
_db_path: Optional[str] = None

# ---- hot-path caches -------------------------------------------------------------------------
# These back lookups that ran once per item per poll, or once per scheduler tick, and re-read a
# whole table every time. Measured at production sizes (1,639 anon uids, 196 watched items), the
# anon set alone cost ~346ms per 60s cache cycle — all of it blocking the single aiosqlite worker
# thread that the poller, the ingest routes and the alert path also queue on.
#
# Each cache has exactly ONE invalidation point, noted at its definition. Both are rebuilt lazily,
# so a manual `DELETE FROM ...` against a STOPPED service (as the README documents for resets) is
# picked up on the next start.
_anon_uids: Optional[frozenset] = None          # invalidated by: mark_anon_uid
_watchlist_targets: Optional[Dict[int, tuple]] = None   # invalidated by: _invalidate_watchlist()
_gear_all_bonus: Optional[List[str]] = None     # invalidated by: set_config("gear_all_bonus")


def _reset_caches() -> None:
    global _anon_uids, _watchlist_targets, _gear_all_bonus
    _anon_uids = None
    _watchlist_targets = None
    _gear_all_bonus = None


def _invalidate_watchlist() -> None:
    global _watchlist_targets
    _watchlist_targets = None

# Nesting depth of open batch() blocks. >0 => the per-helper _commit() calls are suppressed and a
# single commit is issued when the outermost block exits.
_batch_depth: int = 0


async def _commit() -> None:
    """Commit unless we're inside a batch() block."""
    if _batch_depth == 0:
        await _db.commit()


@asynccontextmanager
async def batch():
    """Group a burst of writes into ONE transaction, i.e. one fsync instead of one per row.

    Every commit is an fsync, and aiosqlite runs the whole process's SQL on a SINGLE worker
    thread — so a reconcile pass that commits once per listing doesn't just slow itself down, it
    stalls the poller, the ingest routes and the alert path behind it. Polling one 60-listing gear
    item cost 62 commits before this; it now costs 1.

    Nests safely: only the outermost block commits. Concurrent tasks share the single connection
    (and therefore the transaction) either way — that was already true between commits.

    On an exception we still COMMIT rather than roll back. Before batching, each helper committed
    as it went, so a mid-reconcile failure left the earlier writes durable; rolling back here would
    both change that and discard writes made by *other* tasks that happened to land in the same
    transaction.
    """
    global _batch_depth
    _batch_depth += 1
    try:
        yield
    finally:
        _batch_depth -= 1
        if _batch_depth == 0:
            await _db.commit()


def _within_git_checkout(path: str) -> bool:
    """True if `path` sits at or below a directory containing .git — i.e. inside a working tree."""
    d = os.path.abspath(os.path.dirname(path))
    while True:
        if os.path.isdir(os.path.join(d, ".git")):
            return True
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent


def _resolve_db_path() -> str:
    """Use config.DB_PATH (/var/lib/mug-bot) when its dir is writable; otherwise fall back to a file
    beside the code — which is fine for local dev and dangerous on a server.

    The fallback used to be SILENT, which is how production ended up running with its database inside
    the git checkout: a re-clone, a bad reset or `git clean -xfd` would have destroyed every capture,
    seller mapping and API key. It stays a warning rather than a hard failure so that a box already in
    that state keeps running (and can be migrated deliberately) instead of failing to boot.
    """
    path = config.DB_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.access(os.path.dirname(path), os.W_OK):
            return path
    except OSError:
        pass

    fallback = config.DB_PATH_FALLBACK
    os.makedirs(os.path.dirname(fallback), exist_ok=True)
    detail = ""
    if _within_git_checkout(fallback):
        detail = (" That path is inside a GIT CHECKOUT — `git clean`, a re-clone or a bad reset will"
                  " DESTROY it. Expected for local dev; on a server, fix this.")
    log.warning(
        "database directory %s is not writable — falling back to %s.%s Set MUG_DB_PATH in .env to "
        "choose the location explicitly.",
        os.path.dirname(path), fallback, detail,
    )
    return fallback


def db_path() -> str:
    return _db_path or config.DB_PATH


def _now() -> int:
    return int(time.time())


async def _scalar1(q: str, *args) -> int:
    cur = await _db.execute(q, args)
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def init_db() -> aiosqlite.Connection:
    global _db, _db_path
    _reset_caches()      # a different DB file must never inherit the previous one's cached rows
    _db_path = _resolve_db_path()
    _db = await aiosqlite.connect(_db_path)
    _db.row_factory = aiosqlite.Row
    # Under WAL, synchronous=NORMAL skips the fsync on each commit (the WAL is still fsynced at
    # checkpoints). A process crash / kill -9 is still safe; only a host power-loss or kernel panic
    # can lose the last few transactions — an acceptable trade for market snapshots we re-poll
    # every ~30s anyway, and it removes the dominant cost of the write path.
    await _db.execute("PRAGMA synchronous = NORMAL")
    with open(SCHEMA_PATH) as f:
        await _db.executescript(f.read())
    await _ensure_columns()
    for key, val in _CONFIG_DEFAULTS.items():
        await _db.execute(
            "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)", (key, json.dumps(val))
        )
    await _commit()
    return _db


async def close_db() -> None:
    global _db
    _reset_caches()
    if _db is not None:
        await _db.close()
        _db = None


# --------------------------------- raw event capture ----------------------------------

async def prune_raw_events(max_age_days: Optional[int] = None) -> int:
    """Delete raw_events older than the retention window; returns how many went.

    raw_events lives in the same database the poller and the alert path use, so unbounded growth is
    not free — it was a Phase 0 catch-all that nothing ever cleaned up. VACUUM is deliberately NOT
    run here: it rewrites the entire file under an exclusive lock, which on a live bot is a far worse
    stall than the bloat. Reclaim space with a manual VACUUM during a restart window if you need it.
    """
    days = config.RAW_RETENTION_DAYS if max_age_days is None else max_age_days
    if days <= 0:
        return 0
    cur = await _db.execute("DELETE FROM raw_events WHERE received_at < ?", (_now() - days * 86400,))
    await _commit()
    return cur.rowcount


async def insert_raw_event(source: str, channel: Optional[str], payload_obj: Any) -> None:
    await _db.execute(
        "INSERT INTO raw_events(source, channel, payload, received_at) VALUES (?,?,?,?)",
        (source, channel, json.dumps(payload_obj), _now()),
    )
    await _commit()


# --------------------------------- status tracker -------------------------------------

async def touch_page(page_key: Any, label: Optional[str] = None) -> None:
    await _db.execute(
        "INSERT INTO monitored_pages(page_key, label, last_ping_at) VALUES (?,?,?) "
        "ON CONFLICT(page_key) DO UPDATE SET last_ping_at=excluded.last_ping_at, "
        "label=COALESCE(excluded.label, monitored_pages.label)",
        (str(page_key), label, _now()),
    )
    await _commit()


async def get_pages() -> List[Dict]:
    cur = await _db.execute(
        "SELECT page_key, label, last_ping_at FROM monitored_pages ORDER BY label, page_key"
    )
    rows = await cur.fetchall()
    now = _now()
    return [
        {
            "key": r["page_key"],
            "name": r["label"] or r["page_key"],
            "last_ping_ts": r["last_ping_at"],
            "online": bool(
                r["last_ping_at"] and (now - r["last_ping_at"]) <= config.STATUS_STALE_SECONDS
            ),
        }
        for r in rows
    ]


async def counts() -> Dict[str, int]:
    return {
        "regular": await _scalar1("SELECT COUNT(*) FROM listings WHERE is_gear=0 AND seller_id IS NOT NULL"),
        "gear": await _scalar1("SELECT COUNT(*) FROM listings WHERE is_gear=1 AND seller_id IS NOT NULL"),
        "unmatched": await _scalar1("SELECT COUNT(*) FROM unmatched_gear"),
        "unmatched_items": len(await unmatched_items()),
        "anon": len(await get_anon_uid_set()),
    }


async def status_snapshot() -> Dict:
    return {
        "generated_ts": _now(),
        "watchlist": await watchlist_status(),
        "counts": await counts(),
        "unmatched": await unmatched_list(25),
        "unmatched_items": await unmatched_items(),
        "api_estimate": await api_call_estimate(),
    }


async def api_call_estimate() -> Dict:
    """Estimate Torn API call volume for the ENABLED watch list, against the pool's capacity.

    Each item costs one call per page per poll (gear paginates by listing count, ~100/page; items
    are one page). The cadence is CACHE_REPOLL_OFFSET, not GEAR_POLL_INTERVAL_SECONDS: scheduling
    moved to cache_timestamp-driven ~60s cycles, but this still divided by the old nominal 30s, so
    the figure it reported was roughly DOUBLE the truth — an estimate nobody could trust.

    Returns {enabled, calls_per_poll, calls_per_min, capacity_per_min}."""
    cur = await _db.execute("SELECT is_gear, last_count FROM watchlist WHERE enabled=1")
    rows = await cur.fetchall()
    calls_per_poll = 0
    for r in rows:
        is_gear, last_count = r[0], int(r[1] or 0)
        if is_gear:
            pages = max(1, min((last_count + 99) // 100, config.MAX_PAGES_GEAR))
        else:
            pages = config.MAX_PAGES_ITEM
        calls_per_poll += pages
    polls_per_min = 60.0 / max(config.CACHE_REPOLL_OFFSET, 1)
    from server import keys as keypool  # local import: keys imports db
    return {
        "enabled": len(rows),
        "calls_per_poll": calls_per_poll,
        "calls_per_min": int(round(calls_per_poll * polls_per_min)),
        "capacity_per_min": keypool.active_count() * config.KEY_BUDGET_PER_MIN,
    }


# ------------------------------------ config ------------------------------------------

async def get_config(key: str, default: Any = None) -> Any:
    cur = await _db.execute("SELECT value FROM config WHERE key=?", (key,))
    row = await cur.fetchone()
    return json.loads(row["value"]) if row else default


async def set_config(key: str, value: Any) -> None:
    global _gear_all_bonus
    await _db.execute(
        "INSERT INTO config(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    await _commit()
    if key == "gear_all_bonus":     # single invalidation point for the cached copy
        _gear_all_bonus = None


# ----------------------------- hospitalize factions -----------------------------------

async def get_hospitalize_factions() -> List[int]:
    return [int(x) for x in (await get_config("hospitalize_faction_ids", []) or [])]


async def add_hospitalize_faction(fid: int) -> List[int]:
    ids = await get_hospitalize_factions()
    if int(fid) not in ids:
        ids.append(int(fid))
        await set_config("hospitalize_faction_ids", ids)
    return ids


async def remove_hospitalize_faction(fid: int) -> List[int]:
    ids = [x for x in await get_hospitalize_factions() if x != int(fid)]
    await set_config("hospitalize_faction_ids", ids)
    return ids


# ----------------------------- alerted deals (deal-finder) ----------------------------

async def get_alerted_deals() -> Dict[int, set]:
    """{item_id: {price, ...}} already announced. Loaded once at startup so a restart doesn't
    re-alert every standing under-market listing."""
    cur = await _db.execute("SELECT item_id, price FROM alerted_deals")
    out: Dict[int, set] = {}
    for r in await cur.fetchall():
        out.setdefault(r[0], set()).add(r[1])
    return out


async def add_alerted_deal(item_id, price) -> None:
    await _db.execute(
        "INSERT OR IGNORE INTO alerted_deals(item_id, price, alerted_at) VALUES (?,?,?)",
        (item_id, price, _now()),
    )
    await _commit()


async def drop_alerted_deals(item_id, keep_prices) -> None:
    """Forget alerted prices for this item that are no longer on the market, so a price that
    vanishes and later returns cheap can alert again."""
    if keep_prices:
        placeholders = ",".join("?" for _ in keep_prices)
        await _db.execute(
            f"DELETE FROM alerted_deals WHERE item_id=? AND price NOT IN ({placeholders})",
            (item_id, *keep_prices),
        )
    else:
        await _db.execute("DELETE FROM alerted_deals WHERE item_id=?", (item_id,))
    await _commit()


# ----------------------------- item market values (deal-finder) -----------------------

async def upsert_item_values(values: Dict[int, Dict]) -> None:
    """Bulk-store {item_id: {name, market_value, type}} from the one-shot Torn items call."""
    now = _now()
    await _db.executemany(
        "INSERT INTO item_values(item_id, name, type, market_value, fetched_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET name=excluded.name, type=excluded.type, "
        "market_value=excluded.market_value, fetched_at=excluded.fetched_at",
        [(iid, v.get("name"), v.get("type"), v.get("market_value"), now) for iid, v in values.items()],
    )
    await _commit()


async def item_ids_by_type(types) -> set:
    """item_ids whose Torn `type` is in `types` (e.g. ['Primary']). Empty until the items fetch has
    run at least once. Used to add a whole weapon category by name."""
    types = list(types)
    if not types:
        return set()
    q = ",".join("?" for _ in types)
    cur = await _db.execute(f"SELECT item_id FROM item_values WHERE type IN ({q})", types)
    return {r[0] for r in await cur.fetchall()}


async def get_item_value(item_id) -> Optional[int]:
    cur = await _db.execute("SELECT market_value FROM item_values WHERE item_id=?", (item_id,))
    row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


async def item_values_age() -> Optional[int]:
    """Seconds since the most recent item-values refresh, or None if never fetched."""
    cur = await _db.execute("SELECT MAX(fetched_at) FROM item_values")
    row = await cur.fetchone()
    return (_now() - int(row[0])) if row and row[0] is not None else None


# ----------------------------- gear caches (item-matcher) -----------------------------

async def get_uncached_gear() -> List[Dict]:
    """Unmatched gear in the item-matcher's expected shape (it matches tiles by name + stats and
    builds a filtered search link from item_id + stats)."""
    cur = await _db.execute(
        "SELECT item_id, item_name, stat, accuracy, price FROM unmatched_gear ORDER BY price"
    )
    return [
        {"item_id": r["item_id"], "item_name": r["item_name"], "damage_armor": r["stat"],
         "accuracy": r["accuracy"], "price": r["price"]}
        for r in await cur.fetchall()
    ]


# ----------------------- unmatched gear (poller discovery) ----------------------------

async def upsert_unmatched_gear(*, uid, item_id, item_name, price, stat, accuracy) -> None:
    await _db.execute(
        "INSERT INTO unmatched_gear(uid, item_id, item_name, price, stat, accuracy, seen_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET price=excluded.price, item_name=COALESCE(excluded.item_name, item_name), "
        "stat=excluded.stat, accuracy=excluded.accuracy, seen_at=excluded.seen_at",
        (str(uid), item_id, item_name, price, stat, accuracy, _now()),
    )
    await _commit()


async def remove_unmatched_gear(uid) -> None:
    await _db.execute("DELETE FROM unmatched_gear WHERE uid=?", (str(uid),))
    await _commit()


async def prune_unmatched_gear(item_id, current_uids) -> None:
    """Drop unmatched rows for this item whose uid is no longer on the market. One DELETE, not one
    per row — this runs at the end of every gear reconcile."""
    cur = await _db.execute("SELECT uid FROM unmatched_gear WHERE item_id=?", (item_id,))
    gone = [r[0] for r in await cur.fetchall() if r[0] not in current_uids]
    if not gone:
        return
    # Bind the (small) gone list rather than the live uid set: a fully-paginated gear item can carry
    # thousands of live uids, and SQLite caps host parameters per statement.
    placeholders = ",".join("?" for _ in gone)
    await _db.execute(f"DELETE FROM unmatched_gear WHERE uid IN ({placeholders})", gone)
    await _commit()


async def clear_unmatched_gear(item_id=None) -> int:
    """Wipe the unmatched-gear list (all, or just one item). Returns how many rows were removed."""
    if item_id is None:
        cur = await _db.execute("DELETE FROM unmatched_gear")
    else:
        cur = await _db.execute("DELETE FROM unmatched_gear WHERE item_id=?", (item_id,))
    await _commit()
    return cur.rowcount


async def clear_uncached() -> Dict[str, int]:
    """Clear everything shown in the 'uncached' list: unmatched GEAR rows, plus reset the stackable
    ITEM listing-count baseline (the item entries are derived, so we zero last_count and they vanish
    until the next poll re-discovers any still missing a seller). Both repopulate from the market if
    they're genuinely still uncaptured — so this is also a 'are these stale?' check."""
    g = await _db.execute("DELETE FROM unmatched_gear")
    i = await _db.execute("UPDATE watchlist SET last_count=0 WHERE COALESCE(is_gear, 0)=0")
    await _commit()
    return {"gear": g.rowcount, "items": i.rowcount}


async def unmatched_count() -> int:
    return await _scalar1("SELECT COUNT(*) FROM unmatched_gear")


async def unmatched_items() -> List[Dict]:
    """Watched stackable items that still have live listings we can't mug. `missing` = the last poll's
    listing count minus what's COVERED, where covered = listings the poller has CONFIDENTLY attributed
    to a seller (tracked_items, the strict price+qty matches) PLUS captured anonymous listings (can't
    be mugged, but they fill a slot so they shouldn't read as 'missing' forever). Every item with
    missing>0 is surfaced to the matcher to view + (re)capture. None last_count = not polled yet."""
    cur = await _db.execute(
        "SELECT w.item_id, w.label, w.category, w.last_count, "
        "  (SELECT COUNT(*) FROM tracked_items t "
        "     WHERE t.item_id=w.item_id AND t.seller_id IS NOT NULL) AS attributed, "
        "  (SELECT COUNT(*) FROM listings l "
        "     WHERE l.item_id=w.item_id AND l.is_gear=0 AND l.anonymous=1) AS anon "
        "FROM watchlist w WHERE w.enabled=1 AND w.is_gear=0 ORDER BY w.label"
    )
    out = []
    for r in await cur.fetchall():
        last_count = r["last_count"]
        if last_count is None:
            continue   # not polled yet — nothing to ask for
        covered = int(r["attributed"] or 0) + int(r["anon"] or 0)
        missing = max(int(last_count) - covered, 0)
        if missing == 0:
            continue   # every live listing is confidently attributed (or anon) -> nothing to capture
        out.append({
            "item_id": r["item_id"], "item_name": r["label"] or f"Item {r['item_id']}",
            "category": r["category"], "missing": missing,
        })
    return out


async def get_uncached_for_matcher() -> List[Dict]:
    """What the item-matcher fetches to highlight + request captures: unmatched GEAR only (matched by
    name + stats). Stackable items are deliberately NOT published here — they flooded the list and
    didn't clear cleanly (the user's local page lags the API snapshot, so a "click" rarely removed
    one). Items are still accepted and matched silently server-side; the user views/captures them at
    their own discretion. (unmatched_items still feeds the Discord status panel, just not the matcher.)"""
    return await get_uncached_gear()


async def unmatched_list(limit=25) -> List[Dict]:
    cur = await _db.execute(
        "SELECT item_name, stat, accuracy, price FROM unmatched_gear ORDER BY price DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


# ----------------------------------- anon uids ----------------------------------------

async def mark_anon_uid(*, uid, item_name=None, stat=None, accuracy=None, price=None) -> None:
    global _anon_uids
    await _db.execute(
        "INSERT INTO anon_uids(uid, item_name, stat, accuracy, price, seen_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET seen_at=excluded.seen_at",
        (str(uid), item_name, stat, accuracy, price, _now()),
    )
    await _db.execute("DELETE FROM unmatched_gear WHERE uid=?", (str(uid),))
    await _commit()
    if _anon_uids is not None:                 # the only writer — keep the cache in step
        _anon_uids = _anon_uids | {str(uid)}


async def get_anon_uid_set() -> frozenset:
    """Every anonymous uid, cached in memory.

    reconcile_gear asks this once per item per poll purely to answer "is this uid anonymous?", so at
    production size it was re-reading 1,639 rows ~196 times a cycle (~346ms of the single DB
    thread). The table only grows via mark_anon_uid, so it caches cleanly.

    Returns a frozenset: immutable, so it can be handed out directly without copying per call and
    without a caller being able to corrupt the cache.
    """
    global _anon_uids
    if _anon_uids is None:
        cur = await _db.execute("SELECT uid FROM anon_uids")
        _anon_uids = frozenset(r[0] for r in await cur.fetchall())
    return _anon_uids


async def get_anon_gear_cache() -> List[Dict]:
    cur = await _db.execute("SELECT item_name, stat, accuracy FROM anon_uids")
    return [
        {"item_name": r["item_name"], "damage_armor": r["stat"], "accuracy": r["accuracy"]}
        for r in await cur.fetchall()
    ]


async def get_gear_cache(anonymous: Optional[bool] = None) -> List[Dict]:
    q = "SELECT * FROM listings WHERE is_gear=1"
    args: List[Any] = []
    if anonymous is not None:
        q += " AND anonymous=?"
        args.append(1 if anonymous else 0)
    cur = await _db.execute(q, args)
    return [dict(r) for r in await cur.fetchall()]


# ----------------------------- schema forward-migration -------------------------------

_FORWARD_COLS = {
    "sellers": _SELLER_FORWARD_COLS,
    "watchlist": {"category": "TEXT"},
    "item_values": {"type": "TEXT"},   # Torn item `type` (Primary/Secondary/Melee/Defensive/Drug/…)
    "tracked_gear": {"seen": "INTEGER",    # polls present since attribution (stale-match guard)
                     # How we know who the seller is: NULL/'matcher' = the item-matcher captured
                     # them; 'auction' = INFERRED from an AH win. The alert has to be able to tell
                     # them apart, because the confidence is materially different.
                     "attribution": "TEXT",
                     # The AH evidence AS IT STOOD when the uid was attributed, as JSON. Kept here
                     # rather than looked up at alert time because `anon_matches` is rebuilt whole
                     # on every scrape: by the time the listing vanishes the row can be gone, and
                     # the alert then has no price, date or confidence to show.
                     "attribution_evidence": "TEXT"},
}


async def _ensure_columns() -> None:
    for table, cols in _FORWARD_COLS.items():
        cur = await _db.execute(f"PRAGMA table_info({table})")
        have = {r[1] for r in await cur.fetchall()}
        for col, typ in cols.items():
            if col not in have:
                await _db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    await _commit()


# --------------------------------- listings + sales -----------------------------------

async def upsert_listing(*, listing_id, item_id, item_name, seller_id, price, available,
                         anonymous, is_gear=False, armoury_uid=None, stats_json=None) -> None:
    now = _now()
    await _db.execute(
        """INSERT INTO listings(listing_id, item_id, item_name, seller_id, price, available,
                                is_gear, armoury_uid, anonymous, stats_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(listing_id) DO UPDATE SET
               item_id=excluded.item_id, item_name=excluded.item_name,
               seller_id=excluded.seller_id, price=excluded.price,
               available=excluded.available, anonymous=excluded.anonymous,
               is_gear=excluded.is_gear,
               armoury_uid=COALESCE(excluded.armoury_uid, armoury_uid),
               stats_json=COALESCE(excluded.stats_json, stats_json),
               updated_at=excluded.updated_at""",
        (str(listing_id), item_id, item_name, seller_id, price, available,
         1 if is_gear else 0, armoury_uid, 1 if anonymous else 0, stats_json, now, now),
    )
    await _commit()


async def get_listing(listing_id) -> Optional[Dict]:
    cur = await _db.execute("SELECT * FROM listings WHERE listing_id=?", (str(listing_id),))
    row = await cur.fetchone()
    return dict(row) if row else None


async def set_listing_available(listing_id, available) -> None:
    await _db.execute(
        "UPDATE listings SET available=?, updated_at=? WHERE listing_id=?",
        (available, _now(), str(listing_id)),
    )
    await _commit()


async def delete_listing(listing_id) -> None:
    await _db.execute("DELETE FROM listings WHERE listing_id=?", (str(listing_id),))
    await _commit()


async def record_sale(*, listing_id, item_id, item_name, seller_id, qty_sold,
                      unit_price, sold_at=None) -> int:
    sold_at = sold_at or _now()
    total = int(qty_sold) * int(unit_price or 0)
    cur = await _db.execute(
        """INSERT INTO sales(listing_id, item_id, item_name, seller_id, qty_sold, unit_price,
                             total_value, sold_at) VALUES (?,?,?,?,?,?,?,?)""",
        (str(listing_id) if listing_id is not None else None, item_id, item_name, seller_id,
         int(qty_sold), int(unit_price or 0), total, sold_at),
    )
    await _commit()
    return cur.lastrowid


async def replace_anon_matches(rows: List[Dict], keep_uids) -> Dict[str, int]:
    """Write the AH-inferred owners, and drop any that no longer apply. One transaction.

    `keep_uids` is every uid still eligible for inference — anonymous, or on the market with no
    captured seller. A uid absent from it has since been attributed directly, and DIRECT EVIDENCE
    BEATS INFERRED: leaving the row would keep blaming an old auction winner for a listing whose
    real seller we now know. That deletion is the invalidation rule #12 depends on, so it lives here
    rather than being left to the caller to remember.
    """
    now = _now()
    keep = {str(u) for u in keep_uids}
    async with batch():
        cur = await _db.execute("SELECT uid FROM anon_matches")
        stale = [(u,) for (u,) in await cur.fetchall() if u not in keep]
        if stale:
            await _db.executemany("DELETE FROM anon_matches WHERE uid=?", stale)
        if rows:
            await _db.executemany(
                """INSERT INTO anon_matches(uid, buyer_id, buyer_name, auction_id, price,
                                            won_at, age_days, confidence, matched_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(uid) DO UPDATE SET
                       buyer_id=excluded.buyer_id, buyer_name=excluded.buyer_name,
                       auction_id=excluded.auction_id, price=excluded.price,
                       won_at=excluded.won_at, age_days=excluded.age_days,
                       confidence=excluded.confidence, matched_at=excluded.matched_at""",
                [(str(r["uid"]), r["buyer_id"], r.get("buyer_name"), r.get("auction_id"),
                  r.get("price"), r["won_at"], r.get("age_days"), r.get("confidence"), now)
                 for r in rows],
            )
    return {"written": len(rows), "dropped": len(stale)}


async def get_anon_match(uid) -> Optional[Dict]:
    """The AH-inferred owner for a uid, if we have one. The poller's lookup in #12."""
    if uid is None:
        return None
    cur = await _db.execute("SELECT * FROM anon_matches WHERE uid=?", (str(uid),))
    row = await cur.fetchone()
    return dict(row) if row else None


async def anon_match_summary() -> Dict:
    cur = await _db.execute(
        "SELECT COUNT(*) AS n, AVG(confidence) AS avg_conf, MAX(matched_at) AS last "
        "FROM anon_matches"
    )
    out = dict(await cur.fetchone())
    cur = await _db.execute(
        "SELECT COUNT(*) FROM anon_matches WHERE confidence >= ?",
        (config.AH_CONFIDENCE_BANDS[1][1],),
    )
    out["high_confidence"] = (await cur.fetchone())[0]
    return out


async def observed_gear_owners() -> List[Dict]:
    """Every DIRECT observation of "uid X was listed by seller Y at time T" that we still hold.

    This is the ground truth for calibrating AH-inferred ownership: for each observation we can ask
    what the auction house would have said at that moment, and whether it was right. It needs no
    feedback loop and no new data collection — the record is already here.

    Three sources, deliberately:
      sales        historical, and the only one that survives a listing vanishing. Gear sales are
                   recorded with listing_id "uid:<uid>", which makes the sales table an accidental
                   but complete uid->seller log going back as far as the bot has run.
      listings     the item-matcher's live captures, with the timestamp we first saw them.
      tracked_gear the poller's live view, for uids whose listing row has since been replaced.

    Duplicates across sources are fine — the caller buckets by evidence age, and the same
    observation appearing twice does not bias the ratio it computes.
    """
    out: List[Dict] = []
    cur = await _db.execute(
        "SELECT listing_id, seller_id, sold_at FROM sales "
        "WHERE listing_id LIKE 'uid:%' AND seller_id IS NOT NULL"
    )
    for row in await cur.fetchall():
        out.append({"uid": row[0][4:], "seller_id": row[1], "observed_at": row[2],
                    "source": "sale"})

    cur = await _db.execute(
        "SELECT armoury_uid, seller_id, COALESCE(created_at, updated_at) FROM listings "
        "WHERE armoury_uid IS NOT NULL AND seller_id IS NOT NULL"
    )
    for row in await cur.fetchall():
        if row[2]:
            out.append({"uid": str(row[0]), "seller_id": row[1], "observed_at": row[2],
                        "source": "listing"})

    cur = await _db.execute(
        "SELECT uid, seller_id, first_seen FROM tracked_gear WHERE seller_id IS NOT NULL"
    )
    for row in await cur.fetchall():
        if row[2]:
            out.append({"uid": str(row[0]), "seller_id": row[1], "observed_at": row[2],
                        "source": "tracked"})
    return out


async def recent_sales_for(seller_id, item_id, since) -> List[Dict]:
    """This seller's sales of one item since `since` — the candidate set for cross-path dedup.

    Narrow on purpose: indexed by seller and filtered to a single item and a window of a few
    minutes, so it runs on the alert path without competing with it.
    """
    cur = await _db.execute(
        """SELECT id, listing_id, qty_sold, unit_price, total_value, sold_at
           FROM sales WHERE seller_id=? AND item_id=? AND sold_at>=? ORDER BY sold_at DESC""",
        (seller_id, item_id, int(since)),
    )
    return [dict(r) for r in await cur.fetchall()]


async def delete_sale(sale_id) -> None:
    """Remove a recorded sale (used when a gear vanish turns out to be a delist, not a buy)."""
    await _db.execute("DELETE FROM sales WHERE id=?", (sale_id,))
    await _commit()


async def seller_window(seller_id, window_seconds) -> Dict:
    since = _now() - int(window_seconds)
    # Exclude sales already claimed by a prior alert (assumed mugged) so we never re-count them.
    claimed = await _scalar1(
        "SELECT COALESCE(mug_claimed_until, 0) FROM sellers WHERE user_id=?", seller_id
    )
    floor = max(since, claimed)
    cur = await _db.execute(
        """SELECT item_id, item_name, qty_sold, unit_price, total_value, sold_at
           FROM sales WHERE seller_id=? AND sold_at>? ORDER BY sold_at""",
        (seller_id, floor),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    return {"total": sum(r["total_value"] for r in rows), "count": len(rows), "sales": rows}


async def set_mug_claimed(seller_id, claimed_until) -> None:
    """Mark all this seller's sales at/before `claimed_until` as claimed by an alert (assumed
    mugged) so future windows don't re-count them."""
    await _db.execute(
        "UPDATE sellers SET mug_claimed_until=? WHERE user_id=?",
        (int(claimed_until), seller_id),
    )
    await _commit()


# --------------------------------- sellers --------------------------------------------

async def upsert_seller_seen(*, user_id, name=None, faction=None, online_status=None) -> None:
    now = _now()
    await _db.execute(
        """INSERT INTO sellers(user_id, name, faction, online_status, first_seen, last_seen)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
               name=COALESCE(excluded.name, sellers.name),
               faction=COALESCE(excluded.faction, sellers.faction),
               online_status=COALESCE(excluded.online_status, sellers.online_status),
               last_seen=excluded.last_seen""",
        (user_id, name, faction, online_status, now, now),
    )
    await _commit()


async def get_seller(user_id) -> Optional[Dict]:
    cur = await _db.execute("SELECT * FROM sellers WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def update_seller_enrichment(user_id, fields: Dict) -> None:
    """Store what enrichment learned, CREATING the row if this player is new to us.

    It was a bare UPDATE, which silently matched zero rows for anyone we had never captured
    listing something — and every writer of `sellers` is a capture path. So for a seller INFERRED
    from an auction win, the profile, job and battle stats were fetched over the network and then
    thrown away, every time, leaving the alert with no name and no last action. On this database
    that was 42% of inferred uids (488 of 1,172).
    """
    fields = {k: v for k, v in (fields or {}).items() if v is not None}
    if not fields:
        return
    cols = ", ".join(fields)
    placeholders = ", ".join("?" * len(fields))
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
    await _db.execute(
        f"""INSERT INTO sellers(user_id, first_seen, last_seen, {cols})
            VALUES (?,?,?,{placeholders})
            ON CONFLICT(user_id) DO UPDATE SET {updates}""",
        (user_id, _now(), _now(), *fields.values()),
    )
    await _commit()


async def set_last_alerted(user_id, ts=None) -> None:
    await _db.execute(
        "UPDATE sellers SET last_alerted_at=? WHERE user_id=?", (ts or _now(), user_id)
    )
    await _commit()


async def sellers_in_hospital_due(lead_seconds) -> List[Dict]:
    """Sellers in hospital whose release falls within the next `lead_seconds`."""
    now = _now()
    cur = await _db.execute(
        """SELECT * FROM sellers
           WHERE status_state='Hospital' AND status_until IS NOT NULL
             AND status_until > ? AND status_until <= ?""",
        (now, now + int(lead_seconds)),
    )
    return [dict(r) for r in await cur.fetchall()]


# ------------------------ gear seller map (from item-matcher) --------------------------

async def store_gear_listing(*, listing_id, item_id, item_name, seller_id, price,
                             stats_json=None, armoury_uid=None) -> None:
    await upsert_listing(
        listing_id=listing_id, item_id=item_id, item_name=item_name, seller_id=seller_id,
        price=price, available=1, anonymous=(seller_id is None), is_gear=True,
        armoury_uid=armoury_uid, stats_json=stats_json,
    )
    if item_id is not None and seller_id is not None:
        await watchlist_ensure(item_id, item_name, is_gear=True)


async def gear_poll_item_ids() -> List[int]:
    """Item ids we have captured (seller-mapped) gear for — the poller only watches these."""
    cur = await _db.execute(
        "SELECT DISTINCT item_id FROM listings "
        "WHERE is_gear=1 AND seller_id IS NOT NULL AND item_id IS NOT NULL"
    )
    return [r[0] for r in await cur.fetchall()]


async def get_gear_uid_map(item_id) -> Dict[str, Dict]:
    """Captured gear keyed by the stored uid (armoury_uid) — the exact, collision-proof join
    used when the item-matcher captured the API uid."""
    # ORDER BY updated_at ASC (rowid as tiebreaker for same-second captures) so the MOST RECENT
    # capture for a uid wins (later rows overwrite earlier ones in the dict) — re-marking an item
    # updates its seller even if an older/wrong capture lingers.
    cur = await _db.execute(
        "SELECT armoury_uid, seller_id, item_name FROM listings "
        "WHERE is_gear=1 AND seller_id IS NOT NULL AND armoury_uid IS NOT NULL AND item_id=? "
        "ORDER BY updated_at ASC, rowid ASC",
        (item_id,),
    )
    return {
        str(r["armoury_uid"]): {"seller_id": r["seller_id"], "item_name": r["item_name"]}
        for r in await cur.fetchall()
    }


async def unlink_gear_uid(uid) -> None:
    """Drop the captured seller↔uid pairing (delete the listing rows for this uid) when the item
    leaves the market. The sale itself is already recorded in `sales` for the mug-value window, so
    losing the listing is fine — and a relisted uid then shows as unmatched until it's re-marked."""
    await _db.execute("DELETE FROM listings WHERE armoury_uid=?", (str(uid),))
    await _commit()


# ------------------------------ tracked_gear (poller) ---------------------------------

async def get_tracked_uids_for_item(item_id) -> Dict[str, Dict]:
    cur = await _db.execute("SELECT * FROM tracked_gear WHERE item_id=?", (item_id,))
    return {r["uid"]: dict(r) for r in await cur.fetchall()}


async def upsert_tracked_gear(*, uid, item_id, item_name, price, seller_id, stats_json=None,
                              attribution=None, attribution_evidence=None) -> None:
    now = _now()
    await _db.execute(
        """INSERT INTO tracked_gear(uid, item_id, item_name, price, seller_id, stats_json,
                                    attribution, attribution_evidence, misses, seen,
                                    first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,0,1,?,?)
           ON CONFLICT(uid) DO UPDATE SET misses=0, last_seen=excluded.last_seen,
               price=excluded.price, seen=COALESCE(tracked_gear.seen, 1) + 1,
               -- A later DIRECT capture must overwrite an earlier inference, never the reverse:
               -- COALESCE would pin the first answer, and the first answer is the weaker one.
               -- The evidence moves WITH the attribution: a capture supersedes the inference, so
               -- leaving the old AH evidence behind would have the embed cite reasoning that is no
               -- longer why we believe it.
               seller_id=excluded.seller_id, attribution=excluded.attribution,
               attribution_evidence=excluded.attribution_evidence""",
        (str(uid), item_id, item_name, price, seller_id, stats_json, attribution,
         attribution_evidence, now, now),
    )
    await _commit()


async def bump_tracked_miss(uid) -> int:
    """Increment and return the miss count in one statement (was UPDATE + commit + SELECT, run once
    per vanished uid)."""
    cur = await _db.execute(
        "UPDATE tracked_gear SET misses=misses+1 WHERE uid=? RETURNING misses", (str(uid),)
    )
    row = await cur.fetchone()
    await _commit()
    return int(row[0]) if row else 0


async def delete_tracked_gear(uid) -> None:
    await _db.execute("DELETE FROM tracked_gear WHERE uid=?", (str(uid),))
    await _commit()


# ----------------------------- poll targets (gear + items) ----------------------------

async def _watchlist_target_map() -> Dict[int, tuple]:
    """{item_id: (is_gear, category)} for enabled items, cached. The poll loop asks for this every
    POLL_TICK_SECONDS (1s), while it only changes on a /watch or /gearbonus command."""
    global _watchlist_targets
    if _watchlist_targets is None:
        cur = await _db.execute("SELECT item_id, is_gear, category FROM watchlist WHERE enabled=1")
        _watchlist_targets = {
            r[0]: ((None if r[1] is None else bool(r[1])), r[2]) for r in await cur.fetchall()
        }
    return _watchlist_targets


async def poll_targets() -> List[tuple]:
    """(item_id, is_gear-or-None, category) for every ENABLED watchlist item, optionally restricted
    to config.WATCH_ITEM_IDS. is_gear None => the poller detects it on first poll; category is the
    Torn type once known (used for the per-category 'all bonuses' override)."""
    rows = [(iid, g, c) for iid, (g, c) in (await _watchlist_target_map()).items()]
    if config.WATCH_ITEM_IDS:
        rows = [t for t in rows if t[0] in config.WATCH_ITEM_IDS]
    return rows


async def get_gear_all_bonus() -> List[str]:
    """Tokens (gear category names like 'Melee', or item-id strings) that should be polled with NO
    bonus filter — i.e. capture EVERY listing, not just bonus ones. Editable via /gear allbonus.

    Cached: read once per due-batch by the poller, changed only by a slash command."""
    global _gear_all_bonus
    if _gear_all_bonus is None:
        _gear_all_bonus = [str(x) for x in (await get_config("gear_all_bonus", []) or [])]
    return list(_gear_all_bonus)


async def add_gear_all_bonus(token: str) -> List[str]:
    cur = await get_gear_all_bonus()
    if token not in cur:
        cur.append(token)
        await set_config("gear_all_bonus", cur)
    return cur


async def remove_gear_all_bonus(token: str) -> List[str]:
    cur = [t for t in await get_gear_all_bonus() if t != token]
    await set_config("gear_all_bonus", cur)
    return cur


# --------------------------------- watchlist (poller) ---------------------------------

async def watchlist_ensure(item_id, label=None, is_gear=None) -> None:
    """Auto-add an item (enabled) on first capture; backfill label/is_gear when newly known."""
    g = None if is_gear is None else (1 if is_gear else 0)
    await _db.execute(
        "INSERT OR IGNORE INTO watchlist(item_id, label, is_gear, enabled, added_at) "
        "VALUES (?,?,?,1,?)", (item_id, label, g, _now()),
    )
    await _db.execute(
        "UPDATE watchlist SET label=COALESCE(label, ?), is_gear=COALESCE(is_gear, ?) WHERE item_id=?",
        (label, g, item_id),
    )
    await _commit()
    _invalidate_watchlist()


async def watchlist_add(item_id, label=None, enabled=True, is_gear=None) -> None:
    g = None if is_gear is None else (1 if is_gear else 0)
    await _db.execute(
        "INSERT INTO watchlist(item_id, label, is_gear, enabled, added_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET enabled=excluded.enabled, "
        "label=COALESCE(watchlist.label, excluded.label), "
        "is_gear=COALESCE(watchlist.is_gear, excluded.is_gear)",
        (item_id, label, g, 1 if enabled else 0, _now()),
    )
    await _commit()
    _invalidate_watchlist()


async def watchlist_remove(item_id) -> int:
    cur = await _db.execute("DELETE FROM watchlist WHERE item_id=?", (item_id,))
    await _commit()
    _invalidate_watchlist()
    return cur.rowcount


async def watchlist_add_all(items) -> int:
    """Bulk-add every (item_id, label, is_gear) from the mapped catalog as enabled. Existing rows
    are re-enabled (label/is_gear preserved). Returns the number processed."""
    rows = [(iid, label, (None if g is None else (1 if g else 0)), 1, _now())
            for iid, label, g in items]
    await _db.executemany(
        "INSERT INTO watchlist(item_id, label, is_gear, enabled, added_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET enabled=1, "
        "label=COALESCE(watchlist.label, excluded.label), "
        "is_gear=COALESCE(watchlist.is_gear, excluded.is_gear)",
        rows,
    )
    await _commit()
    _invalidate_watchlist()
    return len(rows)


async def watchlist_clear() -> int:
    """Remove every watched item. Returns how many were removed."""
    cur = await _db.execute("DELETE FROM watchlist")
    await _commit()
    _invalidate_watchlist()
    return cur.rowcount


async def watchlist_set_enabled(item_id, enabled) -> int:
    cur = await _db.execute(
        "UPDATE watchlist SET enabled=? WHERE item_id=?", (1 if enabled else 0, item_id)
    )
    if not enabled:
        # Disabled items shouldn't keep cluttering the uncached list.
        await _db.execute("DELETE FROM unmatched_gear WHERE item_id=?", (item_id,))
    await _commit()
    _invalidate_watchlist()
    return cur.rowcount


async def watchlist_mark_polled(item_id, count, is_gear=None, category=None) -> None:
    """Record a poll. is_gear/category are re-sent on every poll but are settled after the first
    one, so they are written ONLY when they actually differ from what we already hold — which saves
    two UPDATEs per item per poll and, just as importantly, avoids invalidating the poll-target
    cache on every poll (which would make it useless)."""
    await _db.execute(
        "UPDATE watchlist SET last_polled_at=?, last_count=? WHERE item_id=?", (_now(), count, item_id)
    )
    known = (await _watchlist_target_map()).get(item_id)
    changed = False
    if is_gear is not None and (known is None or known[0] != bool(is_gear)):
        await _db.execute(
            "UPDATE watchlist SET is_gear=? WHERE item_id=?", (1 if is_gear else 0, item_id)
        )
        changed = True
    if category and (known is None or known[1] != category):
        await _db.execute(
            "UPDATE watchlist SET category=? WHERE item_id=?", (category, item_id)
        )
        changed = True
    if changed:
        _invalidate_watchlist()
    await _commit()


async def watchlist_remove_by_category(category) -> int:
    """Remove WATCHED items by category keyword (see watchlist_set_enabled_by_category)."""
    if category == "ALL":
        cur = await _db.execute("DELETE FROM watchlist")
    elif category == "GEAR":
        cur = await _db.execute("DELETE FROM watchlist WHERE is_gear=1")
    elif category == "Items":
        cur = await _db.execute("DELETE FROM watchlist WHERE COALESCE(is_gear,0)=0")
    else:
        cur = await _db.execute("DELETE FROM watchlist WHERE category=?", (category,))
    await _commit()
    _invalidate_watchlist()
    return cur.rowcount


async def watchlist_set_enabled_by_category(category, enabled) -> int:
    """Enable/disable WATCHED items by category keyword. 'ALL' = everything, 'GEAR' = all weapons +
    armour, 'Items' = all stackables, else a gear type (Primary/Secondary/Melee/Defensive) matched
    against the stored watchlist.category. Returns rows affected."""
    val = 1 if enabled else 0
    if category == "ALL":
        cur = await _db.execute("UPDATE watchlist SET enabled=?", (val,))
    elif category == "GEAR":
        cur = await _db.execute("UPDATE watchlist SET enabled=? WHERE is_gear=1", (val,))
    elif category == "Items":
        cur = await _db.execute("UPDATE watchlist SET enabled=? WHERE COALESCE(is_gear,0)=0", (val,))
    else:
        cur = await _db.execute("UPDATE watchlist SET enabled=? WHERE category=?", (val, category))
    if not enabled:
        await _db.execute("DELETE FROM unmatched_gear")   # disabled items shouldn't linger uncached
    await _commit()
    _invalidate_watchlist()
    return cur.rowcount


async def watchlist_set_all_enabled(enabled) -> int:
    cur = await _db.execute("UPDATE watchlist SET enabled=?", (1 if enabled else 0,))
    if not enabled:
        await _db.execute("DELETE FROM unmatched_gear")
    await _commit()
    _invalidate_watchlist()
    return cur.rowcount


async def _counts_by_item(sql: str) -> Dict[int, int]:
    cur = await _db.execute(sql)
    return {r[0]: r[1] for r in await cur.fetchall()}


async def watchlist_status() -> List[Dict]:
    """Per-item capture/tracking counts for the Discord status panel.

    Four queries regardless of watch-list size. It used to run two extra scalar queries PER ROW —
    393 round trips at 196 watched items, ~56ms — every time the panel refreshed, which is every 30s
    plus (debounced) on every gear capture, all on the one aiosqlite worker thread that sale
    detection also queues on. Three GROUP BY aggregates joined in Python give identical numbers.

    Aggregates rather than one query with three LEFT JOINs: joining three unrelated tables onto the
    watchlist multiplies rows, and the tracked count has to come from a different table depending on
    is_gear, which a single join can't express cleanly.
    """
    cur = await _db.execute("SELECT * FROM watchlist ORDER BY enabled DESC, label")
    rows = [dict(x) for x in await cur.fetchall()]
    captured = await _counts_by_item(
        "SELECT item_id, COUNT(*) FROM listings WHERE seller_id IS NOT NULL GROUP BY item_id")
    gear_tracked = await _counts_by_item(
        "SELECT item_id, COUNT(*) FROM tracked_gear GROUP BY item_id")
    item_tracked = await _counts_by_item(
        "SELECT item_id, COUNT(*) FROM tracked_items GROUP BY item_id")

    out = []
    for r in rows:
        iid = r["item_id"]
        # is_gear may be NULL (not yet detected); NULL falls to the stackable side, as before.
        tracked = (gear_tracked if r["is_gear"] else item_tracked).get(iid, 0)
        out.append({
            "item_id": iid, "label": r["label"] or f"Item {iid}", "enabled": bool(r["enabled"]),
            "is_gear": (None if r["is_gear"] is None else bool(r["is_gear"])),
            "category": r["category"],
            "captured": captured.get(iid, 0), "tracked": tracked,
            "last_polled_ts": r["last_polled_at"],
        })
    return out


# --------------------------------- api keys -------------------------------------------

async def seed_api_keys_from_env(env_keys: List[str]) -> None:
    """Insert any env keys not already present (enabled, owner filled in later by validation)."""
    now = _now()
    for k in env_keys:
        await _db.execute(
            "INSERT OR IGNORE INTO api_keys(key, enabled, added_at) VALUES (?,1,?)", (k, now)
        )
    await _commit()


async def get_enabled_api_keys() -> List[str]:
    cur = await _db.execute("SELECT key FROM api_keys WHERE enabled=1 ORDER BY added_at")
    return [r[0] for r in await cur.fetchall()]


async def all_api_keys() -> List[Dict]:
    cur = await _db.execute("SELECT * FROM api_keys ORDER BY added_at")
    return [dict(r) for r in await cur.fetchall()]


async def api_key_add(*, key, user_id=None, name=None, level=None) -> None:
    await _db.execute(
        "INSERT INTO api_keys(key, user_id, name, level, enabled, added_at, last_ok_at) "
        "VALUES (?,?,?,?,1,?,?) "
        "ON CONFLICT(key) DO UPDATE SET user_id=excluded.user_id, name=excluded.name, "
        "level=excluded.level, enabled=1, last_ok_at=excluded.last_ok_at, last_error=NULL",
        (key, user_id, name, level, _now(), _now()),
    )
    await _commit()


async def api_key_set_owner(key, user_id, name, level) -> None:
    # Re-enable too: this is only called after a SUCCESSFUL validation, so a key that was wrongly
    # disabled earlier (e.g. a transient Torn wobble) recovers on the next validation pass.
    await _db.execute(
        "UPDATE api_keys SET user_id=?, name=?, level=?, enabled=1, last_ok_at=?, last_error=NULL "
        "WHERE key=?",
        (user_id, name, level, _now(), key),
    )
    await _commit()


async def api_key_enable(identifier=None) -> int:
    """Re-enable a disabled key by full key / owner id / name, or ALL disabled keys when identifier
    is None. Clears the stored last_error. Returns the number of rows re-enabled."""
    if identifier is None:
        cur = await _db.execute("UPDATE api_keys SET enabled=1, last_error=NULL WHERE enabled=0")
    else:
        cur = await _db.execute(
            "UPDATE api_keys SET enabled=1, last_error=NULL "
            "WHERE (key=? OR user_id=? OR name=? COLLATE NOCASE) AND enabled=0",
            (identifier, identifier if str(identifier).isdigit() else -1, str(identifier)),
        )
    await _commit()
    return cur.rowcount


async def api_key_mark_error(key, message) -> None:
    await _db.execute(
        "UPDATE api_keys SET enabled=0, last_error=?, last_error_at=? WHERE key=?",
        (message, _now(), key),
    )
    await _commit()


async def api_key_info(key) -> Optional[Dict]:
    cur = await _db.execute("SELECT * FROM api_keys WHERE key=?", (key,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def api_key_remove(identifier) -> int:
    """Remove by full key, owner user_id, or owner name."""
    cur = await _db.execute(
        "DELETE FROM api_keys WHERE key=? OR user_id=? OR name=? COLLATE NOCASE",
        (identifier, identifier if str(identifier).isdigit() else -1, str(identifier)),
    )
    await _commit()
    return cur.rowcount


# --------------------------- item seller map + tracked_items --------------------------

async def store_item_listing(*, listing_id, item_id, item_name, seller_id, price, available) -> None:
    # No watchlist auto-add for stackables: viewing any item page would otherwise grow the list with
    # things you never asked to monitor. The ingest gate (is_item_watched) discards unwatched items,
    # so this is only ever reached for items you've added explicitly via /watch add.
    await upsert_listing(
        listing_id=listing_id, item_id=item_id, item_name=item_name, seller_id=seller_id,
        price=price, available=available, anonymous=(seller_id is None), is_gear=False,
    )


async def is_item_watched(item_id) -> bool:
    """True only if this item is on the watch list AND enabled — the gate for accepting stackable
    item-matcher captures. Data for items not on the list (or disabled) is thrown away; add an item
    with /watch add to start monitoring it. (Gear is unaffected — it still auto-adds on capture.)"""
    if item_id is None:
        return False
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return False
    cur = await _db.execute("SELECT 1 FROM watchlist WHERE item_id=? AND enabled=1", (iid,))
    return (await cur.fetchone()) is not None


async def resolve_item_seller(item_id, price, amount) -> Optional[Dict]:
    """Captured stackable seller for (item_id, price) — STRICT price+quantity fingerprint. Fungible
    items aren't identified by price alone, so we attribute a seller ONLY when their captured quantity
    still EXACTLY matches the live amount. If the quantity drifted since capture (or we never captured
    this price+qty), return None and stay unattributed rather than guess wrong — the item then surfaces
    to the matcher for (re)capture. Most-recent capture wins on an exact tie."""
    cur = await _db.execute(
        "SELECT seller_id, item_name FROM listings "
        "WHERE is_gear=0 AND seller_id IS NOT NULL AND item_id=? AND price=? AND available=? "
        "ORDER BY updated_at DESC LIMIT 1",
        (item_id, price, amount),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_tracked_items(item_id) -> Dict[int, Dict]:
    cur = await _db.execute("SELECT * FROM tracked_items WHERE item_id=?", (item_id,))
    return {r["price"]: dict(r) for r in await cur.fetchall()}


async def upsert_tracked_item(*, item_id, price, seller_id, item_name, last_amount) -> None:
    now = _now()
    await _db.execute(
        """INSERT INTO tracked_items(item_id, price, item_name, seller_id, last_amount,
                                     misses, first_seen, last_seen)
           VALUES (?,?,?,?,?,0,?,?)
           ON CONFLICT(item_id, price) DO UPDATE SET misses=0, last_amount=excluded.last_amount,
               last_seen=excluded.last_seen""",
        (item_id, price, item_name, seller_id, last_amount, now, now),
    )
    await _commit()


async def bump_tracked_item_miss(item_id, price) -> int:
    cur = await _db.execute(
        "UPDATE tracked_items SET misses=misses+1 WHERE item_id=? AND price=? RETURNING misses",
        (item_id, price),
    )
    row = await cur.fetchone()
    await _commit()
    return int(row[0]) if row else 0


async def delete_tracked_item(item_id, price) -> None:
    await _db.execute("DELETE FROM tracked_items WHERE item_id=? AND price=?", (item_id, price))
    await _commit()
