"""The coverage report: how much of the anon backlog the ledger can actually de-anonymise.

This is #12's go/no-go number, so it has to be right in the direction that matters — it must not
overstate. An inflated "83% resolve" would justify building a feature that then fires on almost
nothing, and the failure would only show up weeks later as alerts that never arrive.
"""

import asyncio
import time

import config
from server import ah_db, db


def _win(auction_id, uid, *, buyer_id=500, ended_at=None, days_ago=None):
    if days_ago is not None:
        ended_at = int(time.time()) - int(days_ago * 86400)
    return {
        "auction_id": auction_id, "uid": uid, "item_id": 399, "item_name": "ArmaLite M-15A4",
        "item_type": "Weapon", "sub_type": "Rifle", "damage": 55.5, "accuracy": 44.4,
        "armor": None, "quality": 99.9, "rarity": None, "bonuses": None,
        "seller_id": 100, "seller_name": "Seller", "buyer_id": buyer_id, "buyer_name": "Buyer",
        "price": 1000, "bids": 3, "ended_at": ended_at or 1_700_000_000,
    }


def _run(scenario, tmp_path):
    async def wrapper():
        await ah_db.init_db(str(tmp_path / "ah.db"))
        try:
            await scenario()
        finally:
            await ah_db.close_db()

    asyncio.run(wrapper())


def test_wins_for_uids_returns_the_latest_per_uid(tmp_path):
    """The bulk form of owner_of_uid, so it must obey the same rule: latest observation wins."""
    async def scenario():
        await ah_db.record_auctions([
            _win(1, "a", buyer_id=111, ended_at=1_700_000_000),
            _win(2, "a", buyer_id=222, ended_at=1_700_900_000),      # newest for 'a'
            _win(3, "b", buyer_id=333, ended_at=1_700_000_000),
        ])
        wins = await ah_db.wins_for_uids({"a", "b", "missing"})
        assert set(wins) == {"a", "b"}
        assert wins["a"]["buyer_id"] == 222

    _run(scenario, tmp_path)


def test_wins_for_uids_survives_more_uids_than_sqlites_parameter_cap(tmp_path):
    """The caller's set is the whole anon backlog — thousands of uids. SQLite's default host
    parameter limit is 999, so an unchunked IN (...) raises rather than returning fewer rows."""
    async def scenario():
        await ah_db.record_auctions([_win(i, str(i)) for i in range(1, 1501)])
        wins = await ah_db.wins_for_uids({str(i) for i in range(1, 2001)})
        assert len(wins) == 1500

    _run(scenario, tmp_path)


def test_wins_for_uids_accepts_integer_uids(tmp_path):
    """Callers hold uids in whatever form their source gave them."""
    async def scenario():
        await ah_db.record_auctions([_win(1, "987")])
        assert set(await ah_db.wins_for_uids([987])) == {"987"}

    _run(scenario, tmp_path)


def test_turnover_counts_how_often_a_uid_is_resold(tmp_path):
    """#12 trusts "won at auction, nothing since says otherwise". How safe that is depends on how
    fast items change hands — measurable, so measure it rather than picking a staleness cutoff."""
    async def scenario():
        await ah_db.record_auctions([
            _win(1, "once", ended_at=1_700_000_000),
            _win(2, "twice", ended_at=1_700_000_000),
            _win(3, "twice", ended_at=1_700_864_000),           # +10 days
            _win(4, "thrice", ended_at=1_700_000_000),
            _win(5, "thrice", ended_at=1_700_172_800),          # +2 days
            _win(6, "thrice", ended_at=1_700_345_600),          # +2 days
        ])
        assert await ah_db.uid_turnover() == {"seen_1x": 1, "seen_2x": 1, "seen_3x": 1}
        # gaps in days: 10, 2, 2 -> median 2
        assert await ah_db.median_days_between_wins() == 2.0

    _run(scenario, tmp_path)


def test_turnover_ignores_uid_less_rows_and_handles_no_repeats(tmp_path):
    async def scenario():
        await ah_db.record_auctions([_win(1, None), _win(2, "solo")])
        assert await ah_db.uid_turnover() == {"seen_1x": 1}
        assert await ah_db.median_days_between_wins() is None

    _run(scenario, tmp_path)


def test_coverage_does_not_overstate_what_resolves(tmp_path, monkeypatch, capsys):
    """The number that decides whether #12 gets built. Only uids the ledger genuinely holds may be
    counted — an inflated figure would justify a feature that then fires on almost nothing."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "main.db"))

    async def scenario():
        await ah_db.init_db(str(tmp_path / "ah.db"))
        await db.init_db()
        try:
            # five anon uids; the ledger knows two of them
            for uid in ("a1", "a2", "a3", "a4", "a5"):
                await db.mark_anon_uid(uid=uid, item_name="X", stat=1.0, accuracy=1.0, price=1)
            await ah_db.record_auctions([
                _win(1, "a1", days_ago=2), _win(2, "a3", days_ago=40),
                _win(3, "nobody-asked", days_ago=1),      # in the ledger, not in the backlog
            ])

            from server.ah_scraper import show_coverage
            await show_coverage()
            out = capsys.readouterr().out

            assert "2 of 5 resolve (40.0%)" in out, out
            assert "3 auctions" in out
            # The age spread is what decides how far the evidence can be trusted. Ages are 2d and
            # 40d; the median is index-based, so on an even count it takes the upper — good enough
            # for an ops report, and it errs toward looking staler rather than fresher.
            assert "median 40d" in out and "1 within a week" in out
            assert "oldest 40d" in out
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(scenario())


def test_coverage_reports_an_empty_backlog_without_dividing_by_zero(tmp_path, monkeypatch):
    """A fresh box has no anon uids at all. The report is the first thing anyone runs, so it must
    not blow up before it prints the ledger size."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "main2.db"))

    async def scenario():
        await ah_db.init_db(str(tmp_path / "ah2.db"))
        await db.init_db()
        try:
            from server.ah_scraper import show_coverage
            await show_coverage()          # must not raise
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(scenario())
