"""Resolving un-attributed uids against the ledger, and keeping that resolution honest.

Two properties matter more than the happy path:

  INVALIDATION  a uid the item-matcher has since captured a real seller for must lose its inferred
                row. Direct evidence beats inferred, and a stale inference keeps blaming an old
                auction winner for someone else's listing.
  SENTINELS     Torn's live ledger has one uid on 49,435 auctions — 9% of every row on record. It
                is not an item. Matching on it would attribute a huge number of unrelated listings
                to whoever last "won" it.
"""

import asyncio
import time

import config
from server import ah_db, ah_scraper, db

DAY = 86400


def _win(auction_id, uid, buyer_id, ended_at, price=1_000_000):
    return {
        "auction_id": auction_id, "uid": uid, "item_id": 399, "item_name": "M-15A4",
        "item_type": "Weapon", "sub_type": "Rifle", "damage": 1.0, "accuracy": 1.0,
        "armor": None, "quality": 1.0, "rarity": None, "bonuses": None,
        "seller_id": 1, "seller_name": "S", "buyer_id": buyer_id, "buyer_name": f"B{buyer_id}",
        "price": price, "bids": 1, "ended_at": ended_at,
    }


def _both(scenario, tmp_path, monkeypatch, name="m"):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / f"{name}_main.db"))

    async def wrapper():
        await ah_db.init_db(str(tmp_path / f"{name}_ah.db"))
        await db.init_db()
        try:
            await scenario()
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(wrapper())


# --------------------------------- confidence -------------------------------------------

def test_confidence_comes_from_the_measured_curve():
    """Each band is an accuracy measured against directly-observed sellers, not a judgement call."""
    assert ah_scraper.confidence_for_age(0) == 0.923
    assert ah_scraper.confidence_for_age(1) == 0.923
    assert ah_scraper.confidence_for_age(5) == 0.880
    assert ah_scraper.confidence_for_age(29) == 0.787
    assert ah_scraper.confidence_for_age(400) == 0.583
    assert ah_scraper.confidence_for_age(99_999) == 0.583


def test_confidence_decreases_with_age():
    """The whole premise. If this ever inverts, the bands were mis-transcribed."""
    ages = [0, 1, 7, 30, 90, 365, 1000]
    scores = [ah_scraper.confidence_for_age(a) for a in ages]
    assert scores == sorted(scores, reverse=True), scores
    assert all(0 < s <= 1 for s in scores)


# --------------------------------- the match step ---------------------------------------

def test_match_resolves_anon_uids_and_records_provenance(tmp_path, monkeypatch):
    async def scenario():
        now = int(time.time())
        for uid in ("a1", "a2", "a3"):
            await db.mark_anon_uid(uid=uid, item_name="M", stat=1.0, accuracy=1.0, price=1)
        await ah_db.record_auctions([
            _win(1, "a1", 500, now - 2 * DAY, price=7_000_000),
            _win(2, "a2", 600, now - 200 * DAY),
        ])

        result = await ah_scraper.run_match()
        assert result["candidates"] == 3 and result["resolved"] == 2

        m = await db.get_anon_match("a1")
        assert m["buyer_id"] == 500 and m["buyer_name"] == "B500"
        assert m["price"] == 7_000_000 and m["age_days"] == 2
        assert m["confidence"] == 0.880                 # 1-7d band
        assert await db.get_anon_match("a3") is None    # no win, stays a dead end

        stale = await db.get_anon_match("a2")
        assert stale["confidence"] == 0.693             # 90-365d band, materially lower

    _both(scenario, tmp_path, monkeypatch, "r")


def test_match_uses_the_most_recent_win(tmp_path, monkeypatch):
    """A uid is resold repeatedly — only the latest observation can be current ownership."""
    async def scenario():
        now = int(time.time())
        await db.mark_anon_uid(uid="u", item_name="M", stat=1.0, accuracy=1.0, price=1)
        await ah_db.record_auctions([
            _win(1, "u", 111, now - 300 * DAY),
            _win(3, "u", 333, now - 5 * DAY),          # newest
            _win(2, "u", 222, now - 100 * DAY),
        ])
        await ah_scraper.run_match()
        m = await db.get_anon_match("u")
        assert m["buyer_id"] == 333 and m["age_days"] == 5

    _both(scenario, tmp_path, monkeypatch, "l")


def test_a_directly_captured_seller_drops_the_inference(tmp_path, monkeypatch):
    """THE invalidation rule. Once the item-matcher knows the real seller, the inferred owner is
    not merely redundant — it is a claim about a listing whose seller we now know, and keeping it
    would blame an old auction winner for someone else's sale."""
    async def scenario():
        now = int(time.time())
        await db.mark_anon_uid(uid="u", item_name="M", stat=1.0, accuracy=1.0, price=1)
        await ah_db.record_auctions([_win(1, "u", 500, now - DAY)])
        await ah_scraper.run_match()
        assert await db.get_anon_match("u") is not None

        # the uid is no longer anonymous / unmatched — a seller was captured
        await db._db.execute("DELETE FROM anon_uids WHERE uid='u'")
        await db._commit()
        db._reset_caches()

        result = await ah_scraper.run_match()
        assert result["dropped"] == 1
        assert await db.get_anon_match("u") is None, "inference outlived the direct evidence"

    _both(scenario, tmp_path, monkeypatch, "i")


def test_match_also_covers_unmatched_gear(tmp_path, monkeypatch):
    """Gear on the market with no captured seller is the other place an AH win helps."""
    async def scenario():
        now = int(time.time())
        await db.upsert_unmatched_gear(uid="g1", item_id=399, item_name="M", price=1,
                                       stat=1.0, accuracy=1.0)
        await ah_db.record_auctions([_win(1, "g1", 700, now - DAY)])
        result = await ah_scraper.run_match()
        assert result["resolved"] == 1
        assert (await db.get_anon_match("g1"))["buyer_id"] == 700

    _both(scenario, tmp_path, monkeypatch, "g")


def test_match_is_idempotent_and_refreshes_a_newer_win(tmp_path, monkeypatch):
    async def scenario():
        now = int(time.time())
        await db.mark_anon_uid(uid="u", item_name="M", stat=1.0, accuracy=1.0, price=1)
        await ah_db.record_auctions([_win(1, "u", 111, now - 100 * DAY)])
        await ah_scraper.run_match()
        await ah_scraper.run_match()
        assert await db._scalar1("SELECT COUNT(*) FROM anon_matches") == 1

        await ah_db.record_auctions([_win(2, "u", 222, now - DAY)])    # resold since
        await ah_scraper.run_match()
        m = await db.get_anon_match("u")
        assert m["buyer_id"] == 222 and m["confidence"] == 0.923
        assert await db._scalar1("SELECT COUNT(*) FROM anon_matches") == 1

    _both(scenario, tmp_path, monkeypatch, "d")


def test_nothing_to_match_is_not_an_error(tmp_path, monkeypatch):
    async def scenario():
        result = await ah_scraper.run_match()
        assert result["candidates"] == 0 and result["written"] == 0

    _both(scenario, tmp_path, monkeypatch, "e")


# --------------------------------- sentinel uids ----------------------------------------

def test_a_sentinel_uid_is_never_matched(tmp_path, monkeypatch):
    """Torn's ledger has one uid on 49,435 auctions. It identifies no item, and matching on it
    would hand a large slice of the market to whoever last won it."""
    async def scenario():
        now = int(time.time())
        await db.mark_anon_uid(uid="sentinel", item_name="M", stat=1.0, accuracy=1.0, price=1)
        await db.mark_anon_uid(uid="real", item_name="M", stat=1.0, accuracy=1.0, price=1)
        wins = [_win(i, "sentinel", 900 + i, now - i * 3600)
                for i in range(config.AH_MAX_UID_REUSE + 5)]
        wins.append(_win(99_999, "real", 42, now - DAY))
        await ah_db.record_auctions(wins)

        result = await ah_scraper.run_match()
        assert await db.get_anon_match("sentinel") is None, "matched on a sentinel uid"
        assert (await db.get_anon_match("real"))["buyer_id"] == 42
        assert result["resolved"] == 1

    _both(scenario, tmp_path, monkeypatch, "s")


def test_a_uid_at_the_reuse_limit_is_still_matched(tmp_path, monkeypatch):
    """The guard must not quietly discard genuinely popular items — the limit is an upper bound on
    plausible resales, not a popularity filter."""
    async def scenario():
        now = int(time.time())
        await db.mark_anon_uid(uid="busy", item_name="M", stat=1.0, accuracy=1.0, price=1)
        await ah_db.record_auctions([_win(i, "busy", 800 + i, now - i * DAY)
                                     for i in range(1, config.AH_MAX_UID_REUSE + 1)])
        await ah_scraper.run_match()
        assert await db.get_anon_match("busy") is not None

    _both(scenario, tmp_path, monkeypatch, "b")


def test_sentinels_are_excluded_from_turnover_statistics(tmp_path, monkeypatch):
    """Live data: one uid contributed 28% of every repeat-gap measured, so the reported 'median
    days apart' described that sentinel rather than the market."""
    async def scenario():
        now = int(time.time())
        wins = [_win(i, "sentinel", 900, now - i * 60)               # thousands of 1-minute gaps
                for i in range(config.AH_MAX_UID_REUSE + 20)]
        wins += [_win(90_001, "real", 1, now - 40 * DAY), _win(90_002, "real", 2, now - 20 * DAY)]
        await ah_db.record_auctions(wins)

        turnover = await ah_db.uid_turnover()
        assert not any(int(k[5:-1]) > config.AH_MAX_UID_REUSE for k in turnover), turnover
        # only the real uid's single 20-day gap survives
        assert await ah_db.median_days_between_wins() == 20.0

    _both(scenario, tmp_path, monkeypatch, "t")


def test_overused_uids_are_reportable(tmp_path, monkeypatch):
    """The operator needs to see WHICH uid is pathological, not just that matching skipped one."""
    async def scenario():
        now = int(time.time())
        await ah_db.record_auctions([_win(i, "sentinel", 900, now - i * 60)
                                     for i in range(config.AH_MAX_UID_REUSE + 3)])
        assert await ah_db.overused_uids(config.AH_MAX_UID_REUSE) == {
            "sentinel": config.AH_MAX_UID_REUSE + 3}

    _both(scenario, tmp_path, monkeypatch, "o")
