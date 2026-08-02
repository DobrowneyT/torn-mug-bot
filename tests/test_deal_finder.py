"""
Deal-finder: a watched item listed at/under (100 - item_deal_pct)% of market value posts one
deal to the items channel, and the same cheap price is not re-alerted until it leaves the market.
"""

import asyncio

import config
from server import db, deal_finder


class FakeBot:
    def __init__(self):
        self.deals = []

    async def post_deal(self, deal):
        self.deals.append(deal)


def test_deal_alert_dedup_and_reappear(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.setattr(config, "ITEMS_CHANNEL_ID", 123)
    deal_finder._alerted.clear()

    async def scenario():
        await db.init_db()
        await db.set_config("item_deal_pct", 50)            # threshold = 50% of market
        await db.upsert_item_values({206: {"name": "Xanax", "market_value": 850_000}})
        bot = FakeBot()

        # one listing under threshold (425,000), one above -> exactly one alert.
        listings = [{"price": 400_000, "amount": 3}, {"price": 800_000, "amount": 1}]
        await deal_finder.check_item_deals(bot, 206, listings, item_name="Xanax", category="Drug")
        assert len(bot.deals) == 1
        d = bot.deals[0]
        assert d["unit_price"] == 400_000 and d["quantity"] == 3 and d["total"] == 1_200_000
        assert d["market_value"] == 850_000 and round(d["discount_pct"]) == 53

        # same cheap price still present -> no re-alert.
        await deal_finder.check_item_deals(bot, 206, listings, item_name="Xanax", category="Drug")
        assert len(bot.deals) == 1

        # cheap price leaves, a new (still-cheap) price appears -> one new alert.
        await deal_finder.check_item_deals(
            bot, 206, [{"price": 410_000, "amount": 2}], item_name="Xanax", category="Drug")
        assert len(bot.deals) == 2 and bot.deals[1]["unit_price"] == 410_000
        await db.close_db()

    asyncio.run(scenario())


def test_no_alert_without_market_value(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "d2.db"))
    monkeypatch.setattr(config, "ITEMS_CHANNEL_ID", 123)
    deal_finder._alerted.clear()

    async def scenario():
        await db.init_db()
        bot = FakeBot()
        # no item_values row -> nothing to compare against -> no alert.
        await deal_finder.check_item_deals(bot, 999, [{"price": 1, "amount": 1}], item_name="X")
        assert bot.deals == []
        await db.close_db()

    asyncio.run(scenario())


def test_a_restart_does_not_re_alert_a_standing_deal(tmp_path, monkeypatch):
    """The bug: _alerted was memory-only, so every restart re-announced (and re-pinged for) every
    standing under-market listing. Auto-deploy restarts the bot on every merge, so this went from
    occasional to routine."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "deals_restart.db"))
    monkeypatch.setattr(config, "ITEMS_CHANNEL_ID", 123)

    async def scenario():
        await db.init_db()
        try:
            await db.upsert_item_values({206: {"name": "Xanax", "market_value": 1_000_000}})
            await db.set_config("item_deal_pct", 50)
            listings = [{"price": 400_000, "amount": 1}]

            bot = FakeBot()
            deal_finder._alerted.clear()
            deal_finder._loaded = False
            await deal_finder.check_item_deals(bot, 206, listings, item_name="Xanax")
            assert len(bot.deals) == 1

            # simulate a process restart: in-memory state gone, database intact
            deal_finder._alerted.clear()
            deal_finder._loaded = False

            bot2 = FakeBot()
            await deal_finder.check_item_deals(bot2, 206, listings, item_name="Xanax")
            assert bot2.deals == [], "re-alerted a standing deal after restart"
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_a_price_that_leaves_and_returns_can_alert_again(tmp_path, monkeypatch):
    """Persisting the dedup must not make it permanent — the existing behaviour is that a price
    which leaves the market is forgotten, so a genuinely new cheap listing still fires."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "deals_return.db"))
    monkeypatch.setattr(config, "ITEMS_CHANNEL_ID", 123)

    async def scenario():
        await db.init_db()
        try:
            await db.upsert_item_values({206: {"name": "Xanax", "market_value": 1_000_000}})
            await db.set_config("item_deal_pct", 50)
            cheap = [{"price": 400_000, "amount": 1}]

            deal_finder._alerted.clear()
            deal_finder._loaded = False
            bot = FakeBot()
            await deal_finder.check_item_deals(bot, 206, cheap, item_name="Xanax")
            assert len(bot.deals) == 1

            # the cheap listing sells; only a normal-priced one remains
            await deal_finder.check_item_deals(bot, 206, [{"price": 990_000, "amount": 1}],
                                               item_name="Xanax")
            assert await db.get_alerted_deals() == {}      # forgotten in the DB too, not just memory

            # ...and it comes back cheap after a restart -> alerts again
            deal_finder._alerted.clear()
            deal_finder._loaded = False
            bot2 = FakeBot()
            await deal_finder.check_item_deals(bot2, 206, cheap, item_name="Xanax")
            assert len(bot2.deals) == 1
        finally:
            await db.close_db()

    asyncio.run(scenario())
