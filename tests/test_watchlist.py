import asyncio

import config
from server import db, item_names


def test_item_names_resolve():
    assert item_names.resolve("233") == (233, "BT MP9")
    assert item_names.resolve("bt mp9") == (233, "BT MP9")
    assert item_names.resolve("Xanax") == (206, "Xanax")
    assert item_names.resolve("nonsense") == (None, None)
    assert item_names.is_gear_hint(233) is True       # weapon
    assert item_names.is_gear_hint(206) is False       # consumable
    assert item_names.is_gear_hint(999999) is None     # unknown


def test_watchlist_crud_and_poll_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "w.db"))
    monkeypatch.setattr(config, "WATCH_ITEM_IDS", [])

    # poll_targets returns (item_id, is_gear, category) tuples.
    def _gmap(targets):
        return {t[0]: t[1] for t in targets}

    async def scenario():
        await db.init_db()
        await db.watchlist_add(233, "BT MP9", enabled=True, is_gear=True)
        await db.watchlist_add(206, "Xanax", enabled=True, is_gear=False)
        assert _gmap(await db.poll_targets()) == {233: True, 206: False}

        await db.watchlist_set_enabled(206, False)
        assert _gmap(await db.poll_targets()) == {233: True}

        # auto-added when a seller is captured for a new item
        await db.upsert_seller_seen(user_id=1, name="x")
        await db.store_gear_listing(
            listing_id="L", item_id=399, item_name="ArmaLite M-15A4", seller_id=1, price=1000
        )
        assert 399 in _gmap(await db.poll_targets())

        st = {w["item_id"]: w for w in await db.watchlist_status()}
        assert st[233]["label"] == "BT MP9" and st[399]["label"] == "ArmaLite M-15A4"
        assert st[233]["is_gear"] is True and st[206]["enabled"] is False

        await db.watchlist_remove(233)
        assert 233 not in _gmap(await db.poll_targets())
        await db.close_db()

    asyncio.run(scenario())


def test_stackable_capture_does_not_autoadd(tmp_path, monkeypatch):
    """Stackable items no longer auto-add to the watch list, and the ingest gate (is_item_watched)
    only accepts items that are explicitly added AND enabled."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "noauto.db"))

    async def scenario():
        await db.init_db()
        assert await db.is_item_watched(206) is False                 # unknown item -> gate rejects
        # storing a stackable listing must NOT create a watchlist entry
        await db.upsert_seller_seen(user_id=1, name="x")
        await db.store_item_listing(listing_id="L", item_id=206, item_name="Xanax",
                                    seller_id=1, price=100, available=5)
        assert 206 not in {w["item_id"] for w in await db.watchlist_status()}
        # explicitly added + enabled -> gate accepts; disabled -> rejects again
        await db.watchlist_add(206, "Xanax", enabled=True, is_gear=False)
        assert await db.is_item_watched(206) is True
        await db.watchlist_set_enabled(206, False)
        assert await db.is_item_watched(206) is False
        await db.close_db()

    asyncio.run(scenario())


def test_matcher_request_is_gear_only(tmp_path, monkeypatch):
    """Items are no longer published to the item-matcher's capture list (they flooded it and didn't
    clear cleanly). Only unmatched GEAR is requested; items are matched passively server-side."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "matcher.db"))

    async def scenario():
        await db.init_db()
        # a watched item with uncaptured listings still shows up INTERNALLY (Discord status)...
        await db.watchlist_add(206, "Xanax", enabled=True, is_gear=False)
        await db.watchlist_mark_polled(206, 5, is_gear=False)
        assert any(i["item_id"] == 206 for i in await db.unmatched_items())
        # ...but the matcher fetch excludes it (gear-only).
        out = await db.get_uncached_for_matcher()
        assert all(row.get("item_id") != 206 for row in out)
        # unmatched GEAR is still requested.
        await db.upsert_unmatched_gear(uid="u1", item_id=399, item_name="ArmaLite M-15A4",
                                       price=1000, stat=74.5, accuracy=60.0)
        out = await db.get_uncached_for_matcher()
        assert any(g.get("item_name") == "ArmaLite M-15A4" for g in out)
        await db.close_db()

    asyncio.run(scenario())


def test_api_call_estimate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "est.db"))
    # The real cadence: each item is repolled at cache_timestamp + CACHE_REPOLL_OFFSET, i.e. about
    # once a minute — NOT the nominal GEAR_POLL_INTERVAL_SECONDS the estimate used to divide by.
    monkeypatch.setattr(config, "CACHE_REPOLL_OFFSET", 60)
    monkeypatch.setattr(config, "MAX_PAGES_GEAR", 50)
    monkeypatch.setattr(config, "MAX_PAGES_ITEM", 1)

    async def scenario():
        await db.init_db()
        await db.watchlist_add(399, "ArmaLite M-15A4", enabled=True, is_gear=True)
        await db.watchlist_mark_polled(399, 223, is_gear=True)   # 223 listings -> 3 pages
        await db.watchlist_add(206, "Xanax", enabled=True, is_gear=False)
        await db.watchlist_mark_polled(206, 100, is_gear=False)  # item -> 1 page
        await db.watchlist_add(330, "Boxing Gloves", enabled=False, is_gear=False)  # disabled -> ignored
        est = await db.api_call_estimate()
        assert est["enabled"] == 2
        assert est["calls_per_poll"] == 4          # 3 (gear) + 1 (item)
        assert est["calls_per_min"] == 4           # 4 per item-cycle, one cycle a minute
        await db.close_db()

    asyncio.run(scenario())


def test_watch_add_all_and_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "all.db"))

    async def scenario():
        await db.init_db()
        catalog = [(iid, name, item_names.is_gear_hint(iid)) for iid, name in item_names.CATALOG]
        n = await db.watchlist_add_all(catalog)
        assert n == len(item_names.CATALOG)
        rows = await db.watchlist_status()
        assert len(rows) == n and all(r["enabled"] for r in rows)   # all added + enabled
        removed = await db.watchlist_clear()
        assert removed == n and await db.watchlist_status() == []
        await db.close_db()

    asyncio.run(scenario())


def test_category_enable_and_type_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "cat.db"))

    async def scenario():
        await db.init_db()
        await db.upsert_item_values({399: {"name": "ArmaLite M-15A4", "type": "Primary", "market_value": 1},
                                     233: {"name": "BT MP9", "type": "Secondary", "market_value": 1}})
        assert await db.item_ids_by_type(["Primary"]) == {399}
        # add both, then enable just the Primary category
        await db.watchlist_add(399, "ArmaLite M-15A4", enabled=False, is_gear=True)
        await db.watchlist_mark_polled(399, 5, is_gear=True, category="Primary")
        await db.watchlist_add(233, "BT MP9", enabled=False, is_gear=True)
        await db.watchlist_mark_polled(233, 5, is_gear=True, category="Secondary")
        n = await db.watchlist_set_enabled_by_category("Primary", True)
        assert n == 1
        rows = {w["item_id"]: w for w in await db.watchlist_status()}
        assert rows[399]["enabled"] is True and rows[233]["enabled"] is False
        await db.close_db()

    asyncio.run(scenario())


def test_gear_all_bonus_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "ab.db"))
    monkeypatch.setattr(config, "GEAR_BONUS_FILTER", "Any")
    from server import api_poller

    async def scenario():
        await db.init_db()
        await db.add_gear_all_bonus("Melee")        # capture ALL melee, by category
        await db.add_gear_all_bonus("399")          # ...and one specific item id
        tokens = await db.get_gear_all_bonus()
        assert set(tokens) == {"Melee", "399"}
        # Melee gear -> no bonus filter; a Primary gear (not listed) -> "Any".
        assert api_poller._wants_all_bonus(11, "Melee", tokens) is True       # by category
        assert api_poller._wants_all_bonus(399, "Primary", tokens) is True    # by item id
        assert api_poller._wants_all_bonus(26, "Primary", tokens) is False    # neither
        await db.remove_gear_all_bonus("Melee")
        assert api_poller._wants_all_bonus(11, "Melee", await db.get_gear_all_bonus()) is False
        await db.close_db()

    asyncio.run(scenario())
