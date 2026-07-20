"""
Items API poller: captured stackable seller + a unique-price listing whose quantity drops across
polls = a confident partial sale. Collisions are skipped; full-vanish is gated by config.
"""

import asyncio

import config
from server import api_poller, db, detection


class FakeBot:
    def __init__(self):
        self.alerts = []

    async def post_mug_alert(self, event):
        self.alerts.append(event)


def _stub_enrich(monkeypatch):
    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)


def test_item_quantity_drop_is_a_sale(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "i.db"))
    _stub_enrich(monkeypatch)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=2250591, name="MonChoon")
        # captured: MonChoon selling Xanax(206) at unique price 881,801, qty 50.
        await db.store_item_listing(
            listing_id="L1", item_id=206, item_name="Xanax",
            seller_id=2250591, price=881_801, available=50,
        )
        bot = FakeBot()

        await api_poller.reconcile_items(bot, 206, [{"price": 881_801, "amount": 50}, {"price": 880_000, "amount": 3}])
        t = await db.get_tracked_items(206)
        assert 881_801 in t and t[881_801]["seller_id"] == 2250591 and t[881_801]["last_amount"] == 50
        assert bot.alerts == []

        # qty 50 -> 30 == a 20-unit sale.
        await api_poller.reconcile_items(bot, 206, [{"price": 881_801, "amount": 30}, {"price": 880_000, "amount": 3}])
        await api_poller.drain_sale_tasks()   # sale handling runs as a background task now
        assert len(bot.alerts) == 1
        assert bot.alerts[0]["seller_name"] == "MonChoon"
        assert bot.alerts[0]["value_change"] == 20 * 881_801
        await db.close_db()

    asyncio.run(scenario())


def test_item_quantity_drifted_not_attributed(tmp_path, monkeypatch):
    """STRICT fingerprint: if the captured quantity no longer matches the live quantity, we do NOT
    attribute a seller (we'd be guessing) — the listing stays untracked until it's re-captured."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "idrift.db"))
    _stub_enrich(monkeypatch)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=2250591, name="MonChoon")
        # captured at qty 50, but the market now shows 47 (3 sold since capture) -> quantity drifted.
        await db.store_item_listing(
            listing_id="L1", item_id=206, item_name="Xanax",
            seller_id=2250591, price=881_801, available=50,
        )
        bot = FakeBot()
        await api_poller.reconcile_items(bot, 206, [{"price": 881_801, "amount": 47}])
        assert await db.get_tracked_items(206) == {}        # not attributed — qty didn't match capture
        # the item still surfaces to the matcher (one live listing, nothing covered).
        await db.watchlist_add(206, "Xanax", is_gear=False)
        await db.watchlist_mark_polled(206, 1, is_gear=False)
        assert any(i["item_id"] == 206 for i in await db.unmatched_items())
        await db.close_db()

    asyncio.run(scenario())


def test_item_price_collision_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "i2.db"))
    _stub_enrich(monkeypatch)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=2250591, name="MonChoon")
        await db.store_item_listing(
            listing_id="L1", item_id=206, item_name="Xanax",
            seller_id=2250591, price=4_000_000, available=5,
        )
        bot = FakeBot()
        # two listings at the round price 4M -> ambiguous -> not tracked, even on a drop.
        await api_poller.reconcile_items(bot, 206, [{"price": 4_000_000, "amount": 5}, {"price": 4_000_000, "amount": 2}])
        assert await db.get_tracked_items(206) == {}
        await api_poller.reconcile_items(bot, 206, [{"price": 4_000_000, "amount": 1}, {"price": 4_000_000, "amount": 2}])
        assert bot.alerts == []
        await db.close_db()

    asyncio.run(scenario())


def test_item_full_vanish_default_no_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "i3.db"))
    monkeypatch.setattr(config, "ITEM_ALERT_ON_REMOVE", False)
    monkeypatch.setattr(config, "ITEM_VANISH_CONFIRM_POLLS", 1)
    _stub_enrich(monkeypatch)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=2250591, name="MonChoon")
        await db.store_item_listing(
            listing_id="L1", item_id=206, item_name="Xanax",
            seller_id=2250591, price=881_801, available=1,
        )
        bot = FakeBot()
        await api_poller.reconcile_items(bot, 206, [{"price": 881_801, "amount": 1}])
        assert 881_801 in await db.get_tracked_items(206)
        # vanishes; full-removal alerts are off by default (sale vs delist/reprice is ambiguous).
        await api_poller.reconcile_items(bot, 206, [])
        assert bot.alerts == []
        assert await db.get_tracked_items(206) == {}
        await db.close_db()

    asyncio.run(scenario())
