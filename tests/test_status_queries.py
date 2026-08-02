"""watchlist_status: same numbers as the per-row implementation, in a constant number of queries.

This powers the Discord status panel, so a subtle regression (a wrong `captured` count, an item
reading as fully covered when it isn't) would be easy to miss and annoying to debug — hence the
equality check against a reference implementation rather than only a query count.
"""

import asyncio

import config
from server import db


async def _reference_watchlist_status():
    """The original per-row implementation, kept here purely as the oracle."""
    cur = await db._db.execute("SELECT * FROM watchlist ORDER BY enabled DESC, label")
    out = []
    for r in [dict(x) for x in await cur.fetchall()]:
        iid = r["item_id"]
        captured = await db._scalar1(
            "SELECT COUNT(*) FROM listings WHERE item_id=? AND seller_id IS NOT NULL", iid)
        if r["is_gear"]:
            tracked = await db._scalar1("SELECT COUNT(*) FROM tracked_gear WHERE item_id=?", iid)
        else:
            tracked = await db._scalar1("SELECT COUNT(*) FROM tracked_items WHERE item_id=?", iid)
        out.append({
            "item_id": iid, "label": r["label"] or f"Item {iid}", "enabled": bool(r["enabled"]),
            "is_gear": (None if r["is_gear"] is None else bool(r["is_gear"])),
            "category": r["category"],
            "captured": captured, "tracked": tracked, "last_polled_ts": r["last_polled_at"],
        })
    return out


async def _build_fixture():
    """Every shape the panel has to render, including the awkward ones."""
    # gear: captured sellers + tracked uids
    await db.watchlist_add(399, "AAA gear", is_gear=True)
    await db.watchlist_mark_polled(399, 3, is_gear=True, category="Primary")
    for j in range(3):
        await db.upsert_listing(listing_id=f"g{j}", item_id=399, item_name="AAA gear",
                                seller_id=100 + j, price=10, available=1, anonymous=False,
                                is_gear=True)
        await db.upsert_tracked_gear(uid=f"gu{j}", item_id=399, item_name="AAA gear",
                                     price=10, seller_id=100 + j)
    # stackable: tracked by price, one anonymous listing (no seller)
    await db.watchlist_add(206, "BBB stackable", is_gear=False)
    await db.watchlist_mark_polled(206, 2, is_gear=False, category="Drug")
    await db.upsert_listing(listing_id="i0", item_id=206, item_name="BBB stackable",
                            seller_id=200, price=5, available=9, anonymous=False, is_gear=False)
    await db.upsert_listing(listing_id="i1", item_id=206, item_name="BBB stackable",
                            seller_id=None, price=6, available=9, anonymous=True, is_gear=False)
    await db.upsert_tracked_item(item_id=206, price=5, seller_id=200,
                                 item_name="BBB stackable", last_amount=9)
    # watched but nothing captured or tracked
    await db.watchlist_add(1, "CCC empty", is_gear=True)
    # never polled, is_gear still unknown (NULL) -> must fall to the stackable side
    await db.watchlist_add(2, "DDD unknown")
    # disabled items still appear in the panel
    await db.watchlist_add(3, "EEE disabled", is_gear=False)
    await db.watchlist_set_enabled(3, False)


def test_matches_the_per_row_implementation_exactly(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "s.db"))

    async def scenario():
        await db.init_db()
        try:
            await _build_fixture()
            assert await db.watchlist_status() == await _reference_watchlist_status()
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_counts_are_actually_right(tmp_path, monkeypatch):
    """Guards against both implementations being wrong in the same way."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "s2.db"))

    async def scenario():
        await db.init_db()
        try:
            await _build_fixture()
            by_id = {w["item_id"]: w for w in await db.watchlist_status()}

            assert by_id[399]["captured"] == 3 and by_id[399]["tracked"] == 3
            # the anonymous listing has no seller, so it is not "captured"
            assert by_id[206]["captured"] == 1 and by_id[206]["tracked"] == 1
            assert by_id[1]["captured"] == 0 and by_id[1]["tracked"] == 0
            assert by_id[2]["is_gear"] is None and by_id[2]["tracked"] == 0
            assert by_id[3]["enabled"] is False
            assert by_id[399]["category"] == "Primary"
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_query_count_does_not_grow_with_the_watch_list(tmp_path, monkeypatch):
    """The point of the change: 196 watched items used to mean 393 round trips per refresh."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "s3.db"))

    async def scenario():
        await db.init_db()
        try:
            await db.watchlist_add_all([(400 + i, f"Item{i}", i % 2 == 0) for i in range(50)])

            counted = []
            real = db._db.execute

            async def spy(sql, *a, **k):
                counted.append(sql)
                return await real(sql, *a, **k)

            try:
                db._db.execute = spy
                await db.watchlist_status()
            finally:
                db._db.execute = real

            assert len(counted) == 4, f"50 items cost {len(counted)} queries"
        finally:
            await db.close_db()

    asyncio.run(scenario())
