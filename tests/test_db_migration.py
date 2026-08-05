"""Opening an EXISTING database, which is the only case that matters.

Every deploy runs `init_db` against a database created by an older version of `schema.sql`. On such
a database `CREATE TABLE IF NOT EXISTS` is a no-op — so anything in that script which depends on a
column added later is evaluated against the OLD table and fails, and because `init_db` is on the
startup path the whole process dies rather than degrading.

A fresh-database test cannot see any of this: there, `CREATE TABLE` really does create the table
with every column, and the script is self-consistent. So these tests build the old shape first.
"""

import asyncio
import sqlite3

import config
from server import db


def _open_then_reopen(tmp_path, monkeypatch, old_sql: str, name: str):
    """Create a database in a PRE-migration shape, then run the real init_db over it."""
    path = tmp_path / f"{name}.db"
    con = sqlite3.connect(path)
    con.executescript(old_sql)
    con.commit()
    con.close()

    monkeypatch.setattr(config, "DB_PATH", str(path))

    async def wrapper():
        await db.init_db()
        try:
            cur = await db._db.execute("PRAGMA table_info(sales)")
            cols = {r[1] for r in await cur.fetchall()}
            cur = await db._db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='sales'")
            indexes = {r[0] for r in await cur.fetchall()}
            return cols, indexes
        finally:
            await db.close_db()

    return asyncio.run(wrapper())


OLD_SALES = """
CREATE TABLE sales (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  TEXT,
    item_id     INTEGER,
    item_name   TEXT,
    seller_id   INTEGER,
    qty_sold    INTEGER,
    unit_price  INTEGER,
    total_value INTEGER,
    sold_at     INTEGER NOT NULL
);
INSERT INTO sales(listing_id, seller_id, qty_sold, unit_price, total_value, sold_at)
VALUES ('uid:1', 42, 1, 100, 100, 1700000000);
"""


def test_a_database_predating_the_attribution_columns_still_opens(tmp_path, monkeypatch):
    """The failure this guards against was real and fatal: `idx_sales_attribution` was written into
    schema.sql, which runs BEFORE the forward-migration adds the column. On a fresh database that
    is fine. On every existing one it raised `no such column: attribution` out of init_db — so the
    next restart of the live bot would not have come back up."""
    cols, indexes = _open_then_reopen(tmp_path, monkeypatch, OLD_SALES, "old")
    assert {"attribution", "confidence"} <= cols, "forward-migration did not add the columns"
    assert "idx_sales_attribution" in indexes, "the index over a migrated column was never created"


def test_the_existing_rows_survive_and_read_as_unknown(tmp_path, monkeypatch):
    """Backfill is impossible — the attribution was destroyed with the tracked_gear row long ago —
    so old sales must read as NULL rather than being silently counted as direct captures, which
    would flatter the inference's measured hit rate with 8,000 rows it never produced."""
    path = tmp_path / "rows.db"
    con = sqlite3.connect(path)
    con.executescript(OLD_SALES)
    con.commit()
    con.close()
    monkeypatch.setattr(config, "DB_PATH", str(path))

    async def scenario():
        await db.init_db()
        try:
            cur = await db._db.execute(
                "SELECT COUNT(*), COUNT(attribution) FROM sales")
            total, attributed = await cur.fetchone()
            assert total == 1, "an existing sale was lost by the migration"
            assert attributed == 0, "an unattributable old sale was given an attribution"
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_init_is_idempotent_over_an_already_migrated_database(tmp_path, monkeypatch):
    """Deploys are frequent and each one re-runs this. Running twice must be indistinguishable from
    running once — including the post-migration index, which is why it is `IF NOT EXISTS`."""
    cols, indexes = _open_then_reopen(tmp_path, monkeypatch, OLD_SALES, "twice")

    async def again():
        await db.init_db()
        try:
            cur = await db._db.execute("PRAGMA table_info(sales)")
            return {r[1] for r in await cur.fetchall()}
        finally:
            await db.close_db()

    assert asyncio.run(again()) == cols
