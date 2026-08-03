"""
Auction house ledger storage — a SEPARATE SQLite file from the mug database.

Why separate: everything in the bot process serialises through one aiosqlite connection, and the
alert path queues on it. A backfill writing tens of thousands of rows into that same file would
contend directly with alerts. The scraper is a short-lived cron process with its own connection, so
neither can stall the other. See ah_schema.sql for the full reasoning and the cost of the split.

This module mirrors server/db.py's shape (module-level connection, batch(), _commit()) so the two
read the same way, but it is deliberately NOT the same connection.
"""

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import aiosqlite

import config

log = logging.getLogger("mug.ah_db")

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "ah_schema.sql")

_db: Optional[aiosqlite.Connection] = None
_db_path: Optional[str] = None
_batch_depth: int = 0


async def _commit() -> None:
    if _batch_depth == 0:
        await _db.commit()


@asynccontextmanager
async def batch():
    """One transaction — and so one fsync — for a whole page of auctions instead of one per row."""
    global _batch_depth
    _batch_depth += 1
    try:
        yield
    finally:
        _batch_depth -= 1
        if _batch_depth == 0:
            await _db.commit()


def _now() -> int:
    return int(time.time())


def _resolve_db_path() -> str:
    """Same policy as the main database: use the configured path when its directory is writable,
    otherwise fall back beside the code and say so loudly. Silence here is how production ended up
    with its data inside the git checkout once already."""
    path = config.AH_DB_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.access(os.path.dirname(path), os.W_OK):
            return path
    except OSError:
        pass

    fallback = config.AH_DB_PATH_FALLBACK
    os.makedirs(os.path.dirname(fallback), exist_ok=True)
    detail = ""
    # Reuse the main module's checkout detection rather than duplicating the walk — one definition
    # of "is this inside a working tree", so the two warnings can never disagree.
    from server.db import _within_git_checkout
    if _within_git_checkout(fallback):
        detail = (" That path is inside a GIT CHECKOUT — `git clean`, a re-clone or a bad reset will"
                  " DESTROY it. Expected for local dev; on a server, fix this.")
    log.warning(
        "auction-house database directory %s is not writable — falling back to %s.%s "
        "Set MUG_AH_DB_PATH in .env to choose the location explicitly.",
        os.path.dirname(path), fallback, detail,
    )
    return fallback


def db_path() -> str:
    return _db_path or config.AH_DB_PATH


async def init_db(path: Optional[str] = None) -> aiosqlite.Connection:
    global _db, _db_path
    _db_path = path or _resolve_db_path()
    _db = await aiosqlite.connect(_db_path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA synchronous = NORMAL")
    with open(SCHEMA_PATH) as f:
        await _db.executescript(f.read())
    await _commit()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def _scalar1(q: str, *args):
    cur = await _db.execute(q, args)
    row = await cur.fetchone()
    return row[0] if row else None


# --------------------------------- the ledger -----------------------------------------

_INSERT = """
INSERT INTO auction_wins(
    auction_id, uid, item_id, item_name, item_type, sub_type,
    damage, accuracy, armor, quality, rarity, bonuses,
    seller_id, seller_name, buyer_id, buyer_name, price, bids, ended_at, seen_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(auction_id) DO NOTHING
"""

_COLUMNS = ("auction_id", "uid", "item_id", "item_name", "item_type", "sub_type",
            "damage", "accuracy", "armor", "quality", "rarity", "bonuses",
            "seller_id", "seller_name", "buyer_id", "buyer_name", "price", "bids", "ended_at")


async def record_auctions(rows: List[Dict[str, Any]]) -> int:
    """Insert normalised auction rows, ignoring ones already stored. Returns how many were NEW.

    A completed auction is immutable, so a re-fetch of a row we already have carries no new
    information — DO NOTHING is both correct and what makes the scrape's overlap window free. The
    new-row count comes from total_changes rather than cursor.rowcount, which executemany does not
    report usefully.
    """
    if not rows:
        return 0
    before = _db.total_changes
    seen = _now()
    async with batch():
        await _db.executemany(
            _INSERT, [tuple(r.get(c) for c in _COLUMNS) + (seen,) for r in rows]
        )
    return _db.total_changes - before


async def newest_ended_at() -> Optional[int]:
    """Forward-scrape watermark. Derived from the data, so it cannot drift away from it."""
    return await _scalar1("SELECT MAX(ended_at) FROM auction_wins")


async def oldest_ended_at() -> Optional[int]:
    """Backfill floor — how far back we have actually walked."""
    return await _scalar1("SELECT MIN(ended_at) FROM auction_wins")


async def count_auctions() -> int:
    return int(await _scalar1("SELECT COUNT(*) FROM auction_wins") or 0)


async def owner_of_uid(uid: Any) -> Optional[Dict]:
    """Most recent auction win for a uid — the lookup #12 attributes anonymous listings with.

    Takes the uid in whatever form the caller has it (the API hands out integers, the main database
    stores TEXT) and normalises, because a type mismatch here fails as "no matches, ever" rather
    than as an error.
    """
    if uid is None:
        return None
    cur = await _db.execute(
        "SELECT * FROM auction_wins WHERE uid = ? ORDER BY ended_at DESC LIMIT 1", (str(uid),)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


# --------------------------------- scraper bookkeeping --------------------------------

async def get_state(key: str, default=None):
    val = await _scalar1("SELECT value FROM ah_state WHERE key = ?", key)
    if val is None:
        return default
    try:
        return json.loads(val)
    except ValueError:
        return default


async def set_state(key: str, value) -> None:
    await _db.execute(
        "INSERT INTO ah_state(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    await _commit()


async def start_run(mode: str) -> int:
    cur = await _db.execute(
        "INSERT INTO ah_runs(mode, started_at) VALUES (?,?)", (mode, _now())
    )
    await _commit()
    return int(cur.lastrowid)


async def finish_run(run_id: int, *, pages: int, rows_seen: int, rows_new: int,
                     error: Optional[str] = None) -> None:
    await _db.execute(
        "UPDATE ah_runs SET ended_at=?, pages=?, rows_seen=?, rows_new=?, error=? WHERE id=?",
        (_now(), pages, rows_seen, rows_new, error, run_id),
    )
    await _commit()


async def prune_runs(keep: int = 500) -> int:
    """The run log is an operational aid, not history — cap it so it can't outgrow the ledger."""
    cur = await _db.execute(
        "DELETE FROM ah_runs WHERE id NOT IN (SELECT id FROM ah_runs ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    await _commit()
    return cur.rowcount


async def recent_runs(limit: int = 10) -> List[Dict]:
    cur = await _db.execute("SELECT * FROM ah_runs ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in await cur.fetchall()]


async def stats() -> Dict[str, Any]:
    """Everything needed to answer "is the scraper healthy and how much have we got" in one call."""
    cur = await _db.execute(
        "SELECT COUNT(*) AS rows, COUNT(uid) AS with_uid, COUNT(DISTINCT buyer_id) AS buyers, "
        "MIN(ended_at) AS oldest, MAX(ended_at) AS newest FROM auction_wins"
    )
    row = dict(await cur.fetchone())
    row["backfill_complete"] = bool(await get_state("backfill_complete", False))
    row["last_runs"] = await recent_runs(5)
    return row
