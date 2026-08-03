"""One real sale seen by BOTH detectors must produce one sale and one alert.

In `both` mode the WebSocket reports a listing's removal instantly and the poller notices the same
uid has vanished one cache cycle later. Nothing joins those two observations — they arrive with
different listing_ids ("555001" vs "uid:777…") — so before the guard each recorded its own row.

That is not merely cosmetic. Measured on the real pipeline: a $3M gear sale, already alerted and
mug-claimed, left ANOTHER unclaimed $3M in the seller's 6h window. The duplicate alert is suppressed
by the 10m cooldown, but only until it expires; the phantom value then either fires on its own or
inflates the next genuine alert. This is the blocker on turning `both` on (#14).
"""

import asyncio
import time

import config
from server import api_poller, db, detection

SELLER = 4106545
ITEM = 985
UID = "77771111"
PRICE = 3_000_000


class FakeBot:
    def __init__(self):
        self.alerts = []

    async def post_mug_alert(self, event):
        self.alerts.append(event)


def _ws_frame(action, rows):
    return {"channel": f"item-market_{ITEM}", "data": [{"message": {"action": action, "data": rows}}]}


def _add(listing_id, price=PRICE, available=1):
    return _ws_frame("add", [{
        "listingID": listing_id, "anonymous": False, "status": "online",
        "user": {"ID": SELLER, "name": "Vglost"}, "price": price,
        "available": available, "relatedItemID": ITEM,
    }])


def _remove(listing_id):
    return _ws_frame("remove", [{"listingID": listing_id}])


def _setup(monkeypatch, tmp_path, name, mode="both"):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / name))
    monkeypatch.setattr(config, "DETECTION_MODE", mode)

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)
    monkeypatch.setattr(detection, "enrich_clothing", fake_enrich)


def _lag(monkeypatch, seconds):
    """Advance the clock the DB stamps sales with, so the poller's row lands later than the WS's —
    which is the whole point: an equal timestamp would be caught by mug_claimed_until anyway."""
    real = db._now
    monkeypatch.setattr(db, "_now", lambda: real() + seconds)


async def _sale_rows():
    cur = await db._db.execute("SELECT listing_id, total_value, sold_at FROM sales ORDER BY id")
    return [dict(r) for r in await cur.fetchall()]


def test_ws_then_poller_records_one_sale(tmp_path, monkeypatch):
    """The realistic ordering: the WebSocket is faster, the poller confirms a cycle later."""
    _setup(monkeypatch, tmp_path, "d1.db")

    async def scenario():
        await db.init_db()
        try:
            await db.set_config("min_mug_value", 1)
            bot = FakeBot()

            await detection.process_market_payload(_add(555001), bot)
            # the poller independently tracks the same weapon, by uid
            await db.upsert_tracked_gear(uid=UID, item_id=ITEM, item_name="ArmaLite M-15A4",
                                         price=PRICE, seller_id=SELLER)

            await detection.process_market_payload(_remove(555001), bot)      # WS sees it sell
            assert len(await _sale_rows()) == 1 and len(bot.alerts) == 1

            _lag(monkeypatch, 45)                                             # one cache cycle later
            await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
            await api_poller.drain_sale_tasks()

            assert len(await _sale_rows()) == 1, "the poller recorded the same sale a second time"
            assert len(bot.alerts) == 1

            # and the seller is left with NO unclaimed phantom value
            win = await db.seller_window(SELLER, config.SALE_WINDOW_SECONDS)
            assert win["total"] == 0, f"phantom ${win['total']:,} left in the window"
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_poller_then_ws_records_one_sale(tmp_path, monkeypatch):
    """The other ordering. The WS is usually first, but a dropped frame or a reconnect can invert
    it, and a guard that only works one way would be worse than useless — it would look correct."""
    _setup(monkeypatch, tmp_path, "d2.db")

    async def scenario():
        await db.init_db()
        try:
            await db.set_config("min_mug_value", 1)
            bot = FakeBot()

            await detection.process_market_payload(_add(555002), bot)
            await db.upsert_tracked_gear(uid=UID, item_id=ITEM, item_name="ArmaLite M-15A4",
                                         price=PRICE, seller_id=SELLER)

            await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
            await api_poller.drain_sale_tasks()
            assert len(await _sale_rows()) == 1

            _lag(monkeypatch, 20)
            await detection.process_market_payload(_remove(555002), bot)

            assert len(await _sale_rows()) == 1, "the WS recorded the same sale a second time"
            assert len(bot.alerts) == 1
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_two_genuine_sales_from_the_same_path_both_count(tmp_path, monkeypatch):
    """The guard must not create the opposite bug. A seller listing and selling two identical items
    in a few minutes is ordinary — swallowing the second under-reports a real mug."""
    _setup(monkeypatch, tmp_path, "d3.db")

    async def scenario():
        await db.init_db()
        try:
            await db.set_config("min_mug_value", 1)
            bot = FakeBot()

            for listing_id in (555003, 555004):
                await detection.process_market_payload(_add(listing_id), bot)
            await detection.process_market_payload(_remove(555003), bot)
            _lag(monkeypatch, 30)
            await detection.process_market_payload(_remove(555004), bot)

            rows = await _sale_rows()
            assert len(rows) == 2, "two genuine sales were collapsed into one"
            assert sum(r["total_value"] for r in rows) == 2 * PRICE
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_a_different_price_is_a_different_sale(tmp_path, monkeypatch):
    """Dedup matches on seller+item+price+qty. Two weapons of the same model at different prices
    are two items, and the poller reporting one must not mask the WS reporting the other."""
    _setup(monkeypatch, tmp_path, "d4.db")

    async def scenario():
        await db.init_db()
        try:
            await db.set_config("min_mug_value", 1)
            bot = FakeBot()

            await detection.process_market_payload(_add(555005, price=PRICE), bot)
            await db.upsert_tracked_gear(uid=UID, item_id=ITEM, item_name="ArmaLite M-15A4",
                                         price=PRICE + 500_000, seller_id=SELLER)

            await detection.process_market_payload(_remove(555005), bot)
            _lag(monkeypatch, 45)
            await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
            await api_poller.drain_sale_tasks()

            assert len(await _sale_rows()) == 2, "two distinct sales were treated as one"
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_the_guard_is_inert_when_only_one_detector_is_live(tmp_path, monkeypatch):
    """Today's production is DETECTION_MODE=api. With one observer per sale there is nothing to
    reconcile, so the guard must be unreachable — not merely unlikely to fire."""
    _setup(monkeypatch, tmp_path, "d5.db", mode="api")

    async def scenario():
        await db.init_db()
        try:
            assert await detection._duplicate_from_other_path(SELLER, ITEM, PRICE, 1, "uid:x") is None
            await db.record_sale(listing_id="555006", item_id=ITEM, item_name="ArmaLite M-15A4",
                                 seller_id=SELLER, qty_sold=1, unit_price=PRICE)
            # an identical cross-path sale would be deduped in `both`; in `api` it must not be
            assert await detection._duplicate_from_other_path(SELLER, ITEM, PRICE, 1, "uid:x") is None
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_an_old_sale_is_not_treated_as_a_duplicate(tmp_path, monkeypatch):
    """The window has to be short. A seller who sells the same weapon model at the same price twice
    in an evening is normal; only observations minutes apart can be the same event."""
    _setup(monkeypatch, tmp_path, "d6.db")

    async def scenario():
        await db.init_db()
        try:
            old = int(time.time()) - (config.SALE_DEDUP_SECONDS + 60)
            await db.record_sale(listing_id="555007", item_id=ITEM, item_name="ArmaLite M-15A4",
                                 seller_id=SELLER, qty_sold=1, unit_price=PRICE, sold_at=old)
            assert await detection._duplicate_from_other_path(SELLER, ITEM, PRICE, 1, "uid:x") is None
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_sale_source_classifies_both_poller_listing_shapes():
    """The prefix is the only thing that says which detector saw a sale, so it has to cover both
    shapes the poller emits — gear vanish and stackable quantity drop."""
    assert detection._sale_source("uid:12345") == "poll"
    assert detection._sale_source("item:985:3000000") == "poll"
    assert detection._sale_source("78614891") == "ws"
    assert detection._sale_source(78614891) == "ws"
    assert detection._sale_source(None) == "ws"
