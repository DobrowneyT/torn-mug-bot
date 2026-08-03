"""Calibration: how often AH-inferred ownership would have been right, by evidence age.

The auction house only sees one of the ways a uid changes hands — trades, bazaars and item-market
sales are all invisible to it. So "most recent AH win" is a decaying estimate of current ownership,
and #12's confidence bands have to come from the measured decay rather than a chosen number.

The failure that matters here is OVERSTATING accuracy, because that is what would justify pinging
on evidence that is actually stale.
"""

import asyncio
import time

import config
from server import ah_db, ah_scraper, db

DAY = 86400


def _win(auction_id, uid, buyer_id, ended_at):
    return {
        "auction_id": auction_id, "uid": uid, "item_id": 399, "item_name": "M-15A4",
        "item_type": "Weapon", "sub_type": "Rifle", "damage": 1.0, "accuracy": 1.0,
        "armor": None, "quality": 1.0, "rarity": None, "bonuses": None,
        "seller_id": 1, "seller_name": "S", "buyer_id": buyer_id, "buyer_name": "B",
        "price": 100, "bids": 1, "ended_at": ended_at,
    }


def _run(scenario, tmp_path):
    async def wrapper():
        await ah_db.init_db(str(tmp_path / "ah.db"))
        try:
            await scenario()
        finally:
            await ah_db.close_db()

    asyncio.run(wrapper())


# --------------------------------- win_as_of --------------------------------------------

def test_win_as_of_picks_the_most_recent_win_before_the_moment():
    """The whole point: a uid auctioned repeatedly has a DIFFERENT owner at different times, so
    'who did the AH say owned this' is only answerable relative to an instant."""
    wins = [_win(3, "u", 333, 3000), _win(2, "u", 222, 2000), _win(1, "u", 111, 1000)]
    assert ah_db.win_as_of(wins, 2500)["buyer_id"] == 222
    assert ah_db.win_as_of(wins, 3500)["buyer_id"] == 333
    assert ah_db.win_as_of(wins, 1500)["buyer_id"] == 111


def test_win_as_of_is_strictly_before():
    """A win at the same instant we observed the seller cannot have informed that observation —
    counting it would let the measurement grade itself against its own answer."""
    wins = [_win(1, "u", 111, 2000)]
    assert ah_db.win_as_of(wins, 2000) is None
    assert ah_db.win_as_of(wins, 2001)["buyer_id"] == 111


def test_win_as_of_returns_none_when_every_win_is_later():
    wins = [_win(1, "u", 111, 5000)]
    assert ah_db.win_as_of(wins, 1000) is None


def test_all_wins_for_uids_returns_full_history_newest_first(tmp_path):
    async def scenario():
        await ah_db.record_auctions([
            _win(1, "u", 111, 1000), _win(2, "u", 222, 3000), _win(3, "v", 333, 2000),
        ])
        wins = await ah_db.all_wins_for_uids({"u", "v"})
        assert [w["buyer_id"] for w in wins["u"]] == [222, 111], "must be newest-first"
        assert len(wins["v"]) == 1

    _run(scenario, tmp_path)


# --------------------------------- observed owners --------------------------------------

def test_observed_owners_reads_gear_sales_from_the_sales_table(tmp_path, monkeypatch):
    """The load-bearing source. Gear sales are recorded with listing_id 'uid:<uid>', which makes
    `sales` an accidental but complete historical uid->seller log — and unlike listings and
    tracked_gear it survives the listing vanishing, which is exactly when a sale happens."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "m.db"))

    async def scenario():
        await db.init_db()
        try:
            await db.record_sale(listing_id="uid:777", item_id=399, item_name="M",
                                 seller_id=42, qty_sold=1, unit_price=100, sold_at=5000)
            # a stackable sale has no uid and must not be picked up
            await db.record_sale(listing_id="item:206:500", item_id=206, item_name="X",
                                 seller_id=43, qty_sold=1, unit_price=500, sold_at=5000)
            # ...nor a WS-path sale, whose listing_id is Torn's listingID
            await db.record_sale(listing_id="88123", item_id=399, item_name="M",
                                 seller_id=44, qty_sold=1, unit_price=100, sold_at=5000)

            obs = await db.observed_gear_owners()
            assert obs == [{"uid": "777", "seller_id": 42, "observed_at": 5000, "source": "sale"}]
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_observed_owners_includes_live_captures(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "m2.db"))

    async def scenario():
        await db.init_db()
        try:
            await db.upsert_listing(listing_id="L1", item_id=399, item_name="M", seller_id=7,
                                    price=1, available=1, anonymous=False, is_gear=True,
                                    armoury_uid="555")
            await db.upsert_tracked_gear(uid="666", item_id=399, item_name="M", price=1,
                                         seller_id=8)
            obs = {(o["uid"], o["seller_id"], o["source"]) for o in await db.observed_gear_owners()}
            assert ("555", 7, "listing") in obs
            assert ("666", 8, "tracked") in obs
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_observed_owners_skips_anonymous_listings(tmp_path, monkeypatch):
    """An anonymous listing has no seller, so it is the thing we are trying to RESOLVE, not
    evidence to resolve it with. Counting it would be circular."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "m3.db"))

    async def scenario():
        await db.init_db()
        try:
            await db.upsert_listing(listing_id="L2", item_id=399, item_name="M", seller_id=None,
                                    price=1, available=1, anonymous=True, is_gear=True,
                                    armoury_uid="999")
            assert await db.observed_gear_owners() == []
        finally:
            await db.close_db()

    asyncio.run(scenario())


# --------------------------------- the report -------------------------------------------

async def _calibration_fixture(now):
    """A uid sold by the AH buyer soon after (agrees) and one sold by someone else much later
    (disagrees) — the two outcomes the report has to tell apart."""
    await ah_db.record_auctions([
        _win(1, "fresh", 100, now - 2 * DAY),
        _win(2, "stale", 200, now - 200 * DAY),
        _win(3, "changed", 300, now - 200 * DAY),
    ])
    await db.record_sale(listing_id="uid:fresh", item_id=399, item_name="M", seller_id=100,
                         qty_sold=1, unit_price=1, sold_at=now - 1 * DAY)          # agrees
    await db.record_sale(listing_id="uid:stale", item_id=399, item_name="M", seller_id=200,
                         qty_sold=1, unit_price=1, sold_at=now - 1 * DAY)          # agrees, old
    await db.record_sale(listing_id="uid:changed", item_id=399, item_name="M", seller_id=999,
                         qty_sold=1, unit_price=1, sold_at=now - 1 * DAY)          # disagrees


def test_calibration_buckets_by_evidence_age(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "c.db"))

    async def scenario():
        await ah_db.init_db(str(tmp_path / "cah.db"))
        await db.init_db()
        try:
            now = int(time.time())
            await _calibration_fixture(now)
            await ah_scraper.show_calibration()
            out = capsys.readouterr().out

            # 'fresh': evidence 1 day old at observation, agreed
            assert "1-7d" in out
            # 'stale' + 'changed': evidence 199 days old, one agreed one not -> 50%
            assert "90-365d" in out
            lines = [ln for ln in out.splitlines() if "90-365d" in ln]
            assert "50.0%" in lines[0], lines
            assert "overall" in out
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(scenario())


def test_calibration_warns_when_the_sample_is_too_small(tmp_path, monkeypatch, capsys):
    """Three comparisons is a smoke test, not a calibration. Presenting it as one would invite
    setting a confidence threshold from noise — the exact mistake this report exists to prevent."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "c2.db"))

    async def scenario():
        await ah_db.init_db(str(tmp_path / "cah2.db"))
        await db.init_db()
        try:
            await _calibration_fixture(int(time.time()))
            await ah_scraper.show_calibration()
            out = capsys.readouterr().out
            assert "NOT ENOUGH DATA" in out
            assert "Set the #12 confidence bands" not in out
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(scenario())


def test_calibration_ignores_observations_with_no_prior_win(tmp_path, monkeypatch, capsys):
    """A uid auctioned only AFTER we saw it sold tells us nothing about inference accuracy, and
    counting it as a miss would understate the method as badly as skipping misses overstates it."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "c3.db"))

    async def scenario():
        await ah_db.init_db(str(tmp_path / "cah3.db"))
        await db.init_db()
        try:
            now = int(time.time())
            await ah_db.record_auctions([_win(1, "later", 100, now)])
            await db.record_sale(listing_id="uid:later", item_id=399, item_name="M", seller_id=555,
                                 qty_sold=1, unit_price=1, sold_at=now - 10 * DAY)
            await ah_scraper.show_calibration()
            out = capsys.readouterr().out
            assert "1 had no prior AH win to test" in out
            assert "overall" not in out, "an untestable observation must not reach the table"
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(scenario())


def test_calibration_says_so_when_there_is_nothing_to_measure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "c4.db"))

    async def scenario():
        await ah_db.init_db(str(tmp_path / "cah4.db"))
        await db.init_db()
        try:
            await ah_scraper.show_calibration()
            assert "nothing to calibrate against" in capsys.readouterr().out
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(scenario())
