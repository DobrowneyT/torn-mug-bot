"""
Async SQLite access (aiosqlite). One shared connection for the process — the ingest
path is effectively single-writer, so a single connection in WAL mode is plenty.

Phase 0 uses raw_events (catch-all) + monitored_pages (status heartbeats). The listings/
sales/sellers helpers are scaffolded for Phase 1 detection + enrichment.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

import aiosqlite

import config

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


def _resolve_db_path() -> str:
    """Use config.DB_PATH (/var/lib/mug-bot) when its dir is writable; otherwise fall back to a
    local file (local dev without the package installed)."""
    path = config.DB_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.access(os.path.dirname(path), os.W_OK):
            return path
    except OSError:
        pass
    os.makedirs(os.path.dirname(config.DB_PATH_FALLBACK), exist_ok=True)
    return config.DB_PATH_FALLBACK


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
    _db_path = _resolve_db_path()
    _db = await aiosqlite.connect(_db_path)
    _db.row_factory = aiosqlite.Row
    with open(SCHEMA_PATH) as f:
        await _db.executescript(f.read())
    await _ensure_columns()
    for key, val in _CONFIG_DEFAULTS.items():
        await _db.execute(
            "INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)", (key, json.dumps(val))
        )
    await _db.commit()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# --------------------------------- raw event capture ----------------------------------

async def insert_raw_event(source: str, channel: Optional[str], payload_obj: Any) -> None:
    await _db.execute(
        "INSERT INTO raw_events(source, channel, payload, received_at) VALUES (?,?,?,?)",
        (source, channel, json.dumps(payload_obj), _now()),
    )
    await _db.commit()


# --------------------------------- status tracker -------------------------------------

async def touch_page(page_key: Any, label: Optional[str] = None) -> None:
    await _db.execute(
        "INSERT INTO monitored_pages(page_key, label, last_ping_at) VALUES (?,?,?) "
        "ON CONFLICT(page_key) DO UPDATE SET last_ping_at=excluded.last_ping_at, "
        "label=COALESCE(excluded.label, monitored_pages.label)",
        (str(page_key), label, _now()),
    )
    await _db.commit()


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
        "anon": await _scalar1("SELECT COUNT(*) FROM anon_uids"),
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
    """Estimate the Torn API call volume for the ENABLED watch list: each item costs 1 call per
    page per poll (gear paginates by listing count, ~100/page; items are 1 page), and we poll
    every GEAR_POLL_INTERVAL_SECONDS. Returns {enabled, calls_per_poll, calls_per_min}."""
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
    polls_per_min = 60.0 / max(config.GEAR_POLL_INTERVAL_SECONDS, 1)
    return {
        "enabled": len(rows),
        "calls_per_poll": calls_per_poll,
        "calls_per_min": int(round(calls_per_poll * polls_per_min)),
    }


# ------------------------------------ config ------------------------------------------

async def get_config(key: str, default: Any = None) -> Any:
    cur = await _db.execute("SELECT value FROM config WHERE key=?", (key,))
    row = await cur.fetchone()
    return json.loads(row["value"]) if row else default


async def set_config(key: str, value: Any) -> None:
    await _db.execute(
        "INSERT INTO config(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    await _db.commit()


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
    await _db.commit()


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
    await _db.commit()


async def remove_unmatched_gear(uid) -> None:
    await _db.execute("DELETE FROM unmatched_gear WHERE uid=?", (str(uid),))
    await _db.commit()


async def prune_unmatched_gear(item_id, current_uids) -> None:
    """Drop unmatched rows for this item whose uid is no longer on the market."""
    cur = await _db.execute("SELECT uid FROM unmatched_gear WHERE item_id=?", (item_id,))
    gone = [r[0] for r in await cur.fetchall() if r[0] not in current_uids]
    for uid in gone:
        await _db.execute("DELETE FROM unmatched_gear WHERE uid=?", (uid,))
    if gone:
        await _db.commit()


async def clear_unmatched_gear(item_id=None) -> int:
    """Wipe the unmatched-gear list (all, or just one item). Returns how many rows were removed."""
    if item_id is None:
        cur = await _db.execute("DELETE FROM unmatched_gear")
    else:
        cur = await _db.execute("DELETE FROM unmatched_gear WHERE item_id=?", (item_id,))
    await _db.commit()
    return cur.rowcount


async def clear_uncached() -> Dict[str, int]:
    """Clear everything shown in the 'uncached' list: unmatched GEAR rows, plus reset the stackable
    ITEM listing-count baseline (the item entries are derived, so we zero last_count and they vanish
    until the next poll re-discovers any still missing a seller). Both repopulate from the market if
    they're genuinely still uncaptured — so this is also a 'are these stale?' check."""
    g = await _db.execute("DELETE FROM unmatched_gear")
    i = await _db.execute("UPDATE watchlist SET last_count=0 WHERE COALESCE(is_gear, 0)=0")
    await _db.commit()
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
    await _db.execute(
        "INSERT INTO anon_uids(uid, item_name, stat, accuracy, price, seen_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(uid) DO UPDATE SET seen_at=excluded.seen_at",
        (str(uid), item_name, stat, accuracy, price, _now()),
    )
    await _db.execute("DELETE FROM unmatched_gear WHERE uid=?", (str(uid),))
    await _db.commit()


async def get_anon_uid_set() -> set:
    cur = await _db.execute("SELECT uid FROM anon_uids")
    return {r[0] for r in await cur.fetchall()}


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
    "tracked_gear": {"seen": "INTEGER"},   # polls present since attribution (stale-match guard)
}


async def _ensure_columns() -> None:
    for table, cols in _FORWARD_COLS.items():
        cur = await _db.execute(f"PRAGMA table_info({table})")
        have = {r[1] for r in await cur.fetchall()}
        for col, typ in cols.items():
            if col not in have:
                await _db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    await _db.commit()


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
    await _db.commit()


async def get_listing(listing_id) -> Optional[Dict]:
    cur = await _db.execute("SELECT * FROM listings WHERE listing_id=?", (str(listing_id),))
    row = await cur.fetchone()
    return dict(row) if row else None


async def set_listing_available(listing_id, available) -> None:
    await _db.execute(
        "UPDATE listings SET available=?, updated_at=? WHERE listing_id=?",
        (available, _now(), str(listing_id)),
    )
    await _db.commit()


async def delete_listing(listing_id) -> None:
    await _db.execute("DELETE FROM listings WHERE listing_id=?", (str(listing_id),))
    await _db.commit()


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
    await _db.commit()
    return cur.lastrowid


async def delete_sale(sale_id) -> None:
    """Remove a recorded sale (used when a gear vanish turns out to be a delist, not a buy)."""
    await _db.execute("DELETE FROM sales WHERE id=?", (sale_id,))
    await _db.commit()


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
    await _db.commit()


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
    await _db.commit()


async def get_seller(user_id) -> Optional[Dict]:
    cur = await _db.execute("SELECT * FROM sellers WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def update_seller_enrichment(user_id, fields: Dict) -> None:
    fields = {k: v for k, v in (fields or {}).items() if v is not None}
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    await _db.execute(f"UPDATE sellers SET {cols} WHERE user_id=?", (*fields.values(), user_id))
    await _db.commit()


async def set_last_alerted(user_id, ts=None) -> None:
    await _db.execute(
        "UPDATE sellers SET last_alerted_at=? WHERE user_id=?", (ts or _now(), user_id)
    )
    await _db.commit()


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
    await _db.commit()


# ------------------------------ tracked_gear (poller) ---------------------------------

async def get_tracked_uids_for_item(item_id) -> Dict[str, Dict]:
    cur = await _db.execute("SELECT * FROM tracked_gear WHERE item_id=?", (item_id,))
    return {r["uid"]: dict(r) for r in await cur.fetchall()}


async def upsert_tracked_gear(*, uid, item_id, item_name, price, seller_id, stats_json=None) -> None:
    now = _now()
    await _db.execute(
        """INSERT INTO tracked_gear(uid, item_id, item_name, price, seller_id, stats_json,
                                    misses, seen, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,0,1,?,?)
           ON CONFLICT(uid) DO UPDATE SET misses=0, last_seen=excluded.last_seen,
               price=excluded.price, seen=COALESCE(tracked_gear.seen, 1) + 1""",
        (str(uid), item_id, item_name, price, seller_id, stats_json, now, now),
    )
    await _db.commit()


async def bump_tracked_miss(uid) -> int:
    await _db.execute("UPDATE tracked_gear SET misses=misses+1 WHERE uid=?", (str(uid),))
    await _db.commit()
    cur = await _db.execute("SELECT misses FROM tracked_gear WHERE uid=?", (str(uid),))
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def delete_tracked_gear(uid) -> None:
    await _db.execute("DELETE FROM tracked_gear WHERE uid=?", (str(uid),))
    await _db.commit()


# ----------------------------- poll targets (gear + items) ----------------------------

async def poll_targets() -> List[tuple]:
    """(item_id, is_gear-or-None, category) for every ENABLED watchlist item, optionally restricted
    to config.WATCH_ITEM_IDS. is_gear None => the poller detects it on first poll; category is the
    Torn type once known (used for the per-category 'all bonuses' override)."""
    cur = await _db.execute("SELECT item_id, is_gear, category FROM watchlist WHERE enabled=1")
    rows = [(r[0], (None if r[1] is None else bool(r[1])), r[2]) for r in await cur.fetchall()]
    if config.WATCH_ITEM_IDS:
        rows = [t for t in rows if t[0] in config.WATCH_ITEM_IDS]
    return rows


async def get_gear_all_bonus() -> List[str]:
    """Tokens (gear category names like 'Melee', or item-id strings) that should be polled with NO
    bonus filter — i.e. capture EVERY listing, not just bonus ones. Editable via /gear allbonus."""
    return [str(x) for x in (await get_config("gear_all_bonus", []) or [])]


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
    await _db.commit()


async def watchlist_add(item_id, label=None, enabled=True, is_gear=None) -> None:
    g = None if is_gear is None else (1 if is_gear else 0)
    await _db.execute(
        "INSERT INTO watchlist(item_id, label, is_gear, enabled, added_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET enabled=excluded.enabled, "
        "label=COALESCE(watchlist.label, excluded.label), "
        "is_gear=COALESCE(watchlist.is_gear, excluded.is_gear)",
        (item_id, label, g, 1 if enabled else 0, _now()),
    )
    await _db.commit()


async def watchlist_remove(item_id) -> int:
    cur = await _db.execute("DELETE FROM watchlist WHERE item_id=?", (item_id,))
    await _db.commit()
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
    await _db.commit()
    return len(rows)


async def watchlist_clear() -> int:
    """Remove every watched item. Returns how many were removed."""
    cur = await _db.execute("DELETE FROM watchlist")
    await _db.commit()
    return cur.rowcount


async def watchlist_set_enabled(item_id, enabled) -> int:
    cur = await _db.execute(
        "UPDATE watchlist SET enabled=? WHERE item_id=?", (1 if enabled else 0, item_id)
    )
    if not enabled:
        # Disabled items shouldn't keep cluttering the uncached list.
        await _db.execute("DELETE FROM unmatched_gear WHERE item_id=?", (item_id,))
    await _db.commit()
    return cur.rowcount


async def watchlist_mark_polled(item_id, count, is_gear=None, category=None) -> None:
    await _db.execute(
        "UPDATE watchlist SET last_polled_at=?, last_count=? WHERE item_id=?", (_now(), count, item_id)
    )
    if is_gear is not None:
        await _db.execute(
            "UPDATE watchlist SET is_gear=? WHERE item_id=?", (1 if is_gear else 0, item_id)
        )
    if category:
        await _db.execute(
            "UPDATE watchlist SET category=? WHERE item_id=?", (category, item_id)
        )
    await _db.commit()


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
    await _db.commit()
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
    await _db.commit()
    return cur.rowcount


async def watchlist_set_all_enabled(enabled) -> int:
    cur = await _db.execute("UPDATE watchlist SET enabled=?", (1 if enabled else 0,))
    if not enabled:
        await _db.execute("DELETE FROM unmatched_gear")
    await _db.commit()
    return cur.rowcount


async def watchlist_status() -> List[Dict]:
    cur = await _db.execute("SELECT * FROM watchlist ORDER BY enabled DESC, label")
    out = []
    for r in [dict(x) for x in await cur.fetchall()]:
        iid = r["item_id"]
        captured = await _scalar1(
            "SELECT COUNT(*) FROM listings WHERE item_id=? AND seller_id IS NOT NULL", iid
        )
        if r["is_gear"]:
            tracked = await _scalar1("SELECT COUNT(*) FROM tracked_gear WHERE item_id=?", iid)
        else:
            tracked = await _scalar1("SELECT COUNT(*) FROM tracked_items WHERE item_id=?", iid)
        out.append({
            "item_id": iid, "label": r["label"] or f"Item {iid}", "enabled": bool(r["enabled"]),
            "is_gear": (None if r["is_gear"] is None else bool(r["is_gear"])),
            "category": r["category"],
            "captured": captured, "tracked": tracked, "last_polled_ts": r["last_polled_at"],
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
    await _db.commit()


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
    await _db.commit()


async def api_key_set_owner(key, user_id, name, level) -> None:
    # Re-enable too: this is only called after a SUCCESSFUL validation, so a key that was wrongly
    # disabled earlier (e.g. a transient Torn wobble) recovers on the next validation pass.
    await _db.execute(
        "UPDATE api_keys SET user_id=?, name=?, level=?, enabled=1, last_ok_at=?, last_error=NULL "
        "WHERE key=?",
        (user_id, name, level, _now(), key),
    )
    await _db.commit()


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
    await _db.commit()
    return cur.rowcount


async def api_key_mark_error(key, message) -> None:
    await _db.execute(
        "UPDATE api_keys SET enabled=0, last_error=?, last_error_at=? WHERE key=?",
        (message, _now(), key),
    )
    await _db.commit()


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
    await _db.commit()
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
    await _db.commit()


async def bump_tracked_item_miss(item_id, price) -> int:
    await _db.execute(
        "UPDATE tracked_items SET misses=misses+1 WHERE item_id=? AND price=?", (item_id, price)
    )
    await _db.commit()
    cur = await _db.execute(
        "SELECT misses FROM tracked_items WHERE item_id=? AND price=?", (item_id, price)
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def delete_tracked_item(item_id, price) -> None:
    await _db.execute("DELETE FROM tracked_items WHERE item_id=? AND price=?", (item_id, price))
    await _db.commit()
