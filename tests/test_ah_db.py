"""The auction house ledger: append-only, idempotent, and joinable to the main database.

uid is stored and looked up as TEXT, matching the main database and — more importantly — matching
the Python convention the rest of the codebase uses (api_poller normalises with str(uid);
reconcile_gear then classifies with `uid in tracked` / `in anon` against sets, where '123' != 123
fails silently). SQLite would cope with either type on its own; the Python side would not.
"""

import asyncio

import config
from server import ah_db, db


def _auction(auction_id, *, uid=None, buyer_id=500, ended_at=1_700_000_000, item_id=399, price=1000):
    return {
        "auction_id": auction_id, "uid": uid, "item_id": item_id, "item_name": "ArmaLite M-15A4",
        "item_type": "Weapon", "sub_type": "Rifle", "damage": 55.5, "accuracy": 44.4,
        "armor": None, "quality": 99.9, "rarity": "yellow", "bonuses": None,
        "seller_id": 100, "seller_name": "Seller", "buyer_id": buyer_id, "buyer_name": "Buyer",
        "price": price, "bids": 3, "ended_at": ended_at,
    }


def _run(scenario, tmp_path, name="ah.db"):
    async def wrapper():
        await ah_db.init_db(str(tmp_path / name))
        try:
            await scenario()
        finally:
            await ah_db.close_db()

    asyncio.run(wrapper())


def test_records_and_counts_new_rows(tmp_path):
    async def scenario():
        assert await ah_db.record_auctions([_auction(1), _auction(2)]) == 2
        assert await ah_db.count_auctions() == 2

    _run(scenario, tmp_path)


def test_recording_the_same_auction_twice_is_a_no_op(tmp_path):
    """The scrape overlap window re-fetches rows on purpose. A completed auction is immutable, so
    re-delivery must cost nothing and must not be counted as new work."""
    async def scenario():
        assert await ah_db.record_auctions([_auction(1), _auction(2)]) == 2
        assert await ah_db.record_auctions([_auction(2), _auction(3)]) == 1
        assert await ah_db.count_auctions() == 3

    _run(scenario, tmp_path)


def test_uid_is_stored_as_text_and_joins_to_the_main_database(tmp_path, monkeypatch):
    """A uid recorded from the AH API must resolve against the uid form the main database holds."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "main.db"))

    async def scenario():
        await ah_db.init_db(str(tmp_path / "ah.db"))
        await db.init_db()
        try:
            # the AH API hands out an integer uid; the scraper normalises it to str
            await ah_db.record_auctions([_auction(1, uid="123456789")])
            # the main database stores gear uids as TEXT, via the item-matcher / poller path
            await db.upsert_tracked_gear(uid="123456789", item_id=399, item_name="ArmaLite M-15A4",
                                         price=1000, seller_id=100)

            cur = await db._db.execute("SELECT uid FROM tracked_gear")
            main_uid = (await cur.fetchone())[0]
            assert isinstance(main_uid, str)

            # the lookup #12 performs, using the uid exactly as the main database holds it
            win = await ah_db.owner_of_uid(main_uid)
            assert win is not None, "AH ledger did not match the main database's uid form"
            assert win["buyer_id"] == 500
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(scenario())


def test_owner_of_uid_accepts_an_integer_uid_too(tmp_path):
    """Callers hold the uid in whichever form their source gave it. Both must resolve."""
    async def scenario():
        await ah_db.record_auctions([_auction(1, uid="987")])
        assert (await ah_db.owner_of_uid(987))["auction_id"] == 1
        assert (await ah_db.owner_of_uid("987"))["auction_id"] == 1
        assert await ah_db.owner_of_uid(None) is None
        assert await ah_db.owner_of_uid("nope") is None

    _run(scenario, tmp_path)


def test_owner_of_uid_returns_the_most_recent_win(tmp_path):
    """A uid changes hands repeatedly — the ledger is an observation log, so the answer is always
    the latest observation, not whichever row happened to be inserted last."""
    async def scenario():
        await ah_db.record_auctions([
            _auction(1, uid="42", buyer_id=111, ended_at=1_700_000_000),
            _auction(3, uid="42", buyer_id=333, ended_at=1_700_009_000),   # newest
            _auction(2, uid="42", buyer_id=222, ended_at=1_700_005_000),
        ])
        win = await ah_db.owner_of_uid("42")
        assert win["buyer_id"] == 333 and win["ended_at"] == 1_700_009_000

    _run(scenario, tmp_path)


def test_watermarks_come_from_the_data(tmp_path):
    """Derived rather than stored, so a crash between "insert rows" and "save cursor" cannot leave
    the scraper's idea of where it is disagreeing with what it actually has."""
    async def scenario():
        assert await ah_db.newest_ended_at() is None
        assert await ah_db.oldest_ended_at() is None
        await ah_db.record_auctions([
            _auction(1, ended_at=1_700_000_000), _auction(2, ended_at=1_700_500_000),
        ])
        assert await ah_db.newest_ended_at() == 1_700_500_000
        assert await ah_db.oldest_ended_at() == 1_700_000_000

    _run(scenario, tmp_path)


def test_state_round_trips_and_defaults(tmp_path):
    async def scenario():
        assert await ah_db.get_state("backfill_complete", False) is False
        await ah_db.set_state("backfill_complete", True)
        assert await ah_db.get_state("backfill_complete", False) is True
        await ah_db.set_state("backfill_complete", False)     # upsert, not a second row
        assert await ah_db.get_state("backfill_complete", True) is False

    _run(scenario, tmp_path)


def test_run_log_records_outcome_and_is_capped(tmp_path):
    async def scenario():
        rid = await ah_db.start_run("forward")
        runs = await ah_db.recent_runs()
        assert runs[0]["ended_at"] is None, "an unfinished run must be visible as unfinished"

        await ah_db.finish_run(rid, pages=2, rows_seen=150, rows_new=7)
        runs = await ah_db.recent_runs()
        assert runs[0]["pages"] == 2 and runs[0]["rows_new"] == 7 and runs[0]["error"] is None

        for _ in range(20):
            await ah_db.finish_run(await ah_db.start_run("forward"), pages=0, rows_seen=0, rows_new=0)
        assert await ah_db.prune_runs(keep=5) == 16
        assert len(await ah_db.recent_runs(50)) == 5

    _run(scenario, tmp_path)


def test_stats_reports_coverage(tmp_path):
    async def scenario():
        await ah_db.record_auctions([
            _auction(1, uid="1", buyer_id=10, ended_at=1_700_000_000),
            _auction(2, uid=None, buyer_id=10, ended_at=1_700_100_000),   # stackable, no uid
            _auction(3, uid="3", buyer_id=20, ended_at=1_700_200_000),
        ])
        s = await ah_db.stats()
        assert s["rows"] == 3 and s["with_uid"] == 2 and s["buyers"] == 2
        assert s["oldest"] == 1_700_000_000 and s["newest"] == 1_700_200_000
        assert s["backfill_complete"] is False

    _run(scenario, tmp_path)
