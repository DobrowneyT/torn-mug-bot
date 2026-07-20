"""
Drives the detection pipeline with the exact WebSocket payloads captured live (item 985),
using a temp DB and a fake bot. enrich_seller is stubbed so no network is touched.
"""

import asyncio
import time

import config
from server import db, detection

# Real captured frames (server receives {channel, data:[ msg.data ]}).
ADD = {"channel": "item-market_985", "data": [{"message": {"namespace": "item-market", "action": "add", "data": [
    {"listingID": 78614891, "anonymous": False, "status": "online",
     "user": {"ID": 4106545, "name": "Vglost"}, "faction": {"ID": 38063, "name": "HiHk"},
     "price": 344000, "available": 1, "relatedItemID": 985}]}}]}
REMOVE = {"channel": "item-market_985", "data": [{"message": {"namespace": "item-market", "action": "remove", "data": [
    {"listingID": 78614891}]}}]}


class FakeBot:
    def __init__(self):
        self.alerts = []

    async def post_mug_alert(self, event):
        self.alerts.append(event)


def test_add_then_remove_is_a_sale(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)        # tiny sale should clear the bar
        bot = FakeBot()

        await detection.process_market_payload(ADD, bot)
        listing = await db.get_listing(78614891)
        assert listing and listing["seller_id"] == 4106545 and listing["available"] == 1

        await detection.process_market_payload(REMOVE, bot)
        assert await db.get_listing(78614891) is None      # listing removed

        assert len(bot.alerts) == 1
        a = bot.alerts[0]
        assert a["seller_name"] == "Vglost"
        assert a["value_change"] == 344000
        assert a["variant"] == "mug"

        # After alerting we claim the sale (assumed mugged), so the window is now empty and the
        # same cash won't be re-counted in a later alert.
        win = await db.seller_window(4106545, 10_000_000)
        assert win["total"] == 0 and win["count"] == 0
        await db.close_db()

    asyncio.run(scenario())


def test_clothing_store_suppresses_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t2.db"))

    async def fake_enrich(user_id, client, force=False):
        await db.update_seller_enrichment(user_id, {"in_clothing_store": 1})
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        # 344000 * 0.0995 * 0.25 ~= 8.5k, which is under this threshold => no alert.
        await db.set_config("min_mug_value", 200_000)
        bot = FakeBot()
        await detection.process_market_payload(ADD, bot)
        await detection.process_market_payload(REMOVE, bot)
        assert bot.alerts == []
        await db.close_db()

    asyncio.run(scenario())


def test_delist_active_secs_helper():
    """_delist_active_secs flags a GEAR uid vanish where the seller acted within the buffer —
    regardless of the (unreliable) online badge — but never old sellers, item sales, or buffer 0."""
    import time as _t
    import types
    now = int(_t.time())
    online_recent = {"online_status": "Online", "last_action_ts": now - 5}
    assert detection._delist_active_secs(online_recent, "uid:999", 120) == 5
    # Stackable items (item:...) get the same de-list treatment as gear now (the Armor Cache case).
    assert detection._delist_active_secs(online_recent, "item:1118:300", 120) == 5
    # Offline badge but a recent action STILL counts — the badge lags real activity (the $450M
    # Guandao case: acted 88s ago, badge said Offline).
    assert detection._delist_active_secs({"online_status": "Offline", "last_action_ts": now - 5},
                                         "uid:999", 120) == 5
    assert detection._delist_active_secs({"online_status": "Online", "last_action_ts": now - 9999},
                                         "uid:999", 120) is None
    assert detection._delist_active_secs(online_recent, "12345", 120) is None   # neither uid:/item:
    assert detection._delist_active_secs(online_recent, "uid:999", 0) is None   # buffer disabled

    # _build_event stores whatever value it's handed (computation lives in _evaluate_alert now).
    res = types.SimpleNamespace(mug_min=1, mug_max=2)
    ev = detection._build_event(1, online_recent, {"total": 1, "count": 1, "sales": []}, res,
                                last_item={"item_name": "AK"}, delist_active_secs=7)
    assert ev["delist_active_secs"] == 7


def test_hospital_due_skips_when_banked_since_sale(tmp_path, monkeypatch):
    """A held seller who's been online since the sale (last action well after it) has banked the
    cash -> the near-release post is suppressed and the stale sale is claimed."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "hb.db"))
    monkeypatch.setattr(config, "HOSPITAL_RELEASE_LEAD_SECONDS", 120)
    now = int(time.time())

    async def fake_enrich(uid, client, force=False):
        # Now out of hospital; last action 10 min ago — but the sale was 30 min ago, so they've
        # been active (and able to bank) since selling.
        await db.update_seller_enrichment(uid, {"status_state": "Okay", "status_until": None,
                                                 "last_action_ts": now - 600})
        return await db.get_seller(uid)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.set_config("hosp_mode", "hold")
        await db.upsert_seller_seen(user_id=70, name="Banked")
        # Stored status is due within the lead so the query selects them; the sale was 30 min ago.
        await db.update_seller_enrichment(70, {"status_state": "Hospital", "status_until": now + 60})
        await db.record_sale(listing_id="uid:1", item_id=399, item_name="AK", seller_id=70,
                             qty_sold=1, unit_price=500_000_000, sold_at=now - 1800)
        bot = FakeBot()
        await detection.post_hospital_due(bot)
        assert bot.alerts == []                                    # suppressed — assumed banked
        assert (await db.seller_window(70, 7200))["total"] == 0     # and the stale sale is claimed
        await db.close_db()

    asyncio.run(scenario())


def test_hospital_due_reholds_when_rehospitalised(tmp_path, monkeypatch):
    """post_hospital_due must re-verify the LIVE status before posting: a seller whose stored
    release was near but who got re-hospitalised (fresh release far beyond the lead) is re-held,
    not posted early. When they're genuinely leaving within the lead, it posts."""
    import time as _t
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "h.db"))
    monkeypatch.setattr(config, "HOSPITAL_RELEASE_LEAD_SECONDS", 120)

    ctl = {"until_offset": 0}  # controls the FRESH status the (mocked) enrich returns

    async def fake_enrich(user_id, client, force=False):
        now = int(_t.time())
        base = await db.get_seller(user_id) or {}
        # Return live status WITHOUT persisting, so sellers_in_hospital_due keeps selecting the row.
        return {**base, "status_state": "Hospital", "status_until": now + ctl["until_offset"],
                "last_action_ts": now}

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.set_config("hosp_mode", "hold")   # re-hold guard applies to hold/remind, not live
        now = int(_t.time())
        await db.upsert_seller_seen(user_id=42, name="Held")
        # Stored release is within the lead, so the query selects them; record a qualifying sale.
        await db.update_seller_enrichment(42, {"status_state": "Hospital", "status_until": now + 60})
        await db.record_sale(listing_id="x", item_id=1, item_name="X", seller_id=42,
                             qty_sold=1, unit_price=500_000_000)
        bot = FakeBot()

        ctl["until_offset"] = 99999          # re-hospitalised: far beyond the 120s lead -> re-hold
        await detection.post_hospital_due(bot)
        assert bot.alerts == []

        ctl["until_offset"] = 60             # genuinely leaving within the lead -> post
        await detection.post_hospital_due(bot)
        assert len(bot.alerts) == 1 and bot.alerts[0]["variant"] == "hospital"
        await db.close_db()

    asyncio.run(scenario())


def test_delist_off_drops_and_silent_skips_ping(tmp_path, monkeypatch):
    """/delist mode=off drops a likely de-list (no alert, sale removed from the window); mode=silent
    posts it but flags the event silent (no role ping)."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.setattr(config, "MUG_CLAIM_ON_ALERT", False)
    now = int(time.time())

    async def fake_enrich(uid, client, force=False):
        # Online + just acted = the de-list signature.
        await db.update_seller_enrichment(uid, {"status_state": "Okay", "online_status": "Online",
                                                 "last_action_ts": now - 3})
        return await db.get_seller(uid)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=50, name="Puller")
        bot = FakeBot()

        await db.set_config("delist_mode", "off")
        sid = await db.record_sale(listing_id="uid:1", item_id=399, item_name="AK", seller_id=50,
                                   qty_sold=1, unit_price=500_000_000)
        await detection._evaluate_alert(50, await db.get_seller(50), bot,
                                        last_item={"item_id": 399, "item_name": "AK"},
                                        sale_id=sid, listing_id="uid:1")
        assert bot.alerts == []                                    # suppressed
        assert (await db.seller_window(50, 3600))["total"] == 0     # phantom removed from window

        await db.set_config("delist_mode", "silent")
        sid2 = await db.record_sale(listing_id="uid:2", item_id=399, item_name="AK", seller_id=50,
                                    qty_sold=1, unit_price=500_000_000)
        await detection._evaluate_alert(50, await db.get_seller(50), bot,
                                        last_item={"item_id": 399, "item_name": "AK"},
                                        sale_id=sid2, listing_id="uid:2")
        assert len(bot.alerts) == 1
        assert bot.alerts[0]["silent"] is True
        assert bot.alerts[0]["delist_active_secs"] is not None
        await db.close_db()

    asyncio.run(scenario())


def test_hospital_live_posts_immediately_and_silent_when_far(tmp_path, monkeypatch):
    """Default hosp_mode=live posts an in-hospital sale right away (variant=hospital). It posts
    SILENTLY (no role ping) when release is further than the lead, but pings when within the lead."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "hl.db"))
    now = int(time.time())
    until = {"v": now + 600}   # 10 min out -> far (lead defaults to 120s)

    async def fake_enrich(uid, client, force=False):
        await db.update_seller_enrichment(uid, {"status_state": "Hospital", "status_until": until["v"],
                                                 "online_status": "Offline", "last_action_ts": now - 600})
        return await db.get_seller(uid)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)   # hosp_mode defaults to "live", lead 120s
        await db.upsert_seller_seen(user_id=60, name="Hosp")
        bot = FakeBot()

        # Far out (release in 10 min) -> post, but silent (no ping).
        sid = await db.record_sale(listing_id="uid:9", item_id=399, item_name="AK", seller_id=60,
                                   qty_sold=1, unit_price=500_000_000)
        await detection._evaluate_alert(60, await db.get_seller(60), bot,
                                        last_item={"item_id": 399, "item_name": "AK"},
                                        sale_id=sid, listing_id="uid:9")
        assert len(bot.alerts) == 1
        assert bot.alerts[0]["variant"] == "hospital" and bot.alerts[0]["silent"] is True

        # Within the lead (release in 60s) -> ping (not silent). New seller to dodge the cooldown.
        until["v"] = now + 60
        await db.upsert_seller_seen(user_id=61, name="Hosp2")
        sid2 = await db.record_sale(listing_id="uid:10", item_id=399, item_name="AK", seller_id=61,
                                    qty_sold=1, unit_price=500_000_000)
        await detection._evaluate_alert(61, await db.get_seller(61), bot,
                                        last_item={"item_id": 399, "item_name": "AK"},
                                        sale_id=sid2, listing_id="uid:10")
        assert len(bot.alerts) == 2 and bot.alerts[1]["silent"] is False
        await db.close_db()

    asyncio.run(scenario())


def test_anonymous_listing_never_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t3.db"))

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    anon_add = {"channel": "item-market_985", "data": [{"message": {"action": "add", "data": [
        {"listingID": 999, "anonymous": True, "price": 5_000_000_000, "available": 1, "relatedItemID": 985}]}}]}
    anon_remove = {"channel": "item-market_985", "data": [{"message": {"action": "remove", "data": [
        {"listingID": 999}]}}]}

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        bot = FakeBot()
        await detection.process_market_payload(anon_add, bot)
        await detection.process_market_payload(anon_remove, bot)
        assert bot.alerts == []                          # anonymous => no seller to mug
        await db.close_db()

    asyncio.run(scenario())
