"""Bonuses, normalised so they can be searched by id AND value range.

`auction_wins.bonuses` stays the source of truth; this table is a rebuildable index over it. That
framing decides the failure policy: anything wrong here is fixed by regenerating, never migrating.

The subtle risks are all about a range filter matching something it shouldn't — a NULL value read as
zero, or a bonus appearing twice and collapsing to one row. Both widen a search silently.
"""

import asyncio
import json

from server import ah_db, ah_scraper

LIVE_BONUSES = [
    {"id": 42, "title": "Wither", "description": "25% chance to Wither…", "value": 25},
    {"id": 101, "title": "Penetrate", "description": "Ignores 31% of armor mitigation", "value": 31},
]


def _auction(auction_id, *, bonuses=None, uid=None):
    return {
        "auction_id": auction_id, "uid": uid or str(auction_id), "item_id": 238,
        "item_name": "Sai", "item_type": "Weapon", "sub_type": "Piercing",
        "damage": 37.31, "accuracy": 60.48, "armor": None, "quality": 167.88,
        "rarity": "orange", "bonuses": json.dumps(bonuses) if bonuses is not None else None,
        "seller_id": 1, "seller_name": "S", "buyer_id": 2, "buyer_name": "B",
        "price": 153_900_001, "bids": 6, "ended_at": 1_700_000_000 + auction_id,
    }


def _run(scenario, tmp_path, name="b.db"):
    async def wrapper():
        await ah_db.init_db(str(tmp_path / name))
        try:
            await scenario()
        finally:
            await ah_db.close_db()

    asyncio.run(wrapper())


async def _index():
    cur = await ah_db._db.execute(
        "SELECT auction_id, bonus_id, value, ordinal FROM auction_bonuses "
        "ORDER BY auction_id, ordinal")
    return [tuple(r) for r in await cur.fetchall()]


# --------------------------------- ingest -----------------------------------------------

def test_bonuses_are_indexed_as_auctions_arrive(tmp_path):
    """Populated on the ingest path, so a live scrape never leaves the index behind."""
    async def scenario():
        await ah_db.record_auctions([_auction(1, bonuses=LIVE_BONUSES)])
        assert await _index() == [(1, 42, 25, 0), (1, 101, 31, 1)]

    _run(scenario, tmp_path)


def test_indexing_does_not_inflate_the_new_auction_count(tmp_path):
    """record_auctions counts new rows off the connection's cumulative total_changes, which the
    scrape logs and the run log both report. Folding bonus rows into it would overstate every
    scrape — and it did, until the reading was moved before the bonus insert."""
    async def scenario():
        assert await ah_db.record_auctions([_auction(1, bonuses=LIVE_BONUSES)]) == 1
        assert await ah_db.record_auctions([_auction(1, bonuses=LIVE_BONUSES)]) == 0
        assert await ah_db.record_auctions([
            _auction(2, bonuses=LIVE_BONUSES), _auction(3, bonuses=LIVE_BONUSES)]) == 2

    _run(scenario, tmp_path, "c.db")


def test_an_auction_with_no_bonuses_indexes_nothing(tmp_path):
    async def scenario():
        await ah_db.record_auctions([_auction(1), _auction(2, bonuses=[])])
        assert await _index() == []

    _run(scenario, tmp_path, "d.db")


def test_a_bonus_with_no_value_is_stored_as_NULL_not_zero(tmp_path):
    """Some bonuses are flags with no magnitude. Zero would silently satisfy any `value >= 0`
    filter and quietly widen every search that uses one."""
    async def scenario():
        await ah_db.record_auctions([_auction(1, bonuses=[{"id": 7, "title": "Flag"}])])
        assert await _index() == [(1, 7, None, 0)]

        cur = await ah_db._db.execute(
            "SELECT COUNT(*) FROM auction_bonuses WHERE bonus_id=7 AND value BETWEEN 0 AND 100")
        assert (await cur.fetchone())[0] == 0, "a NULL value matched a range filter"

    _run(scenario, tmp_path, "e.db")


def test_the_same_bonus_twice_is_kept_as_two_rows(tmp_path):
    """Whether Torn ever does this is unverified. If the key were (auction_id, bonus_id) the second
    would vanish, so `ordinal` is in the key — cheap insurance against a silent loss."""
    async def scenario():
        await ah_db.record_auctions([_auction(1, bonuses=[
            {"id": 86, "title": "Disarm", "value": 5},
            {"id": 86, "title": "Disarm", "value": 9},
        ])])
        assert await _index() == [(1, 86, 5, 0), (1, 86, 9, 1)]
        assert (await ah_db.bonus_index_health())["same_bonus_twice"] == 1

    _run(scenario, tmp_path, "f.db")


def test_malformed_bonuses_do_not_abort_the_ingest(tmp_path):
    """A page is inserted as one batch. A single unparseable blob must not take the batch with it —
    that would lose real auctions over a cosmetic field."""
    async def scenario():
        await ah_db.record_auctions([
            {**_auction(1), "bonuses": "not json at all"},
            {**_auction(2), "bonuses": json.dumps({"id": 5})},        # object, not a list
            {**_auction(3), "bonuses": json.dumps([{"title": "no id"}, {"id": 9, "value": 3}])},
            _auction(4, bonuses=LIVE_BONUSES),
        ])
        assert await ah_db.count_auctions() == 4, "a bad bonuses blob lost real auctions"
        assert await _index() == [(3, 9, 3, 1), (4, 42, 25, 0), (4, 101, 31, 1)]

    _run(scenario, tmp_path, "g.db")


# --------------------------------- rebuild ----------------------------------------------

def test_rebuild_regenerates_the_index_from_the_source(tmp_path):
    """The whole point of it being derived: corrupt it however you like, a rebuild fixes it."""
    async def scenario():
        await ah_db.record_auctions([_auction(i, bonuses=LIVE_BONUSES) for i in range(1, 6)])
        await ah_db._db.execute("DELETE FROM auction_bonuses WHERE auction_id < 4")
        await ah_db._db.execute(
            "INSERT INTO auction_bonuses(auction_id, bonus_id, value, ordinal) VALUES (99,1,1,0)")
        await ah_db._db.commit()

        result = await ah_db.rebuild_bonus_index()
        assert result == {"scanned": 5, "indexed": 10}
        rows = await _index()
        assert len(rows) == 10
        assert not [r for r in rows if r[0] == 99], "rebuild kept a row with no source auction"
        assert (await ah_db.bonus_index_health())["in_step"] is True

    _run(scenario, tmp_path, "h.db")


def test_rebuild_chunks_without_losing_or_duplicating(tmp_path):
    """Chunked so a 548k-row pass is not one enormous transaction against a live database. The
    chunk cursor is the thing to get wrong — an off-by-one skips or repeats a whole chunk."""
    async def scenario():
        await ah_db.record_auctions([_auction(i, bonuses=LIVE_BONUSES) for i in range(1, 26)])
        result = await ah_db.rebuild_bonus_index(chunk=4)
        assert result == {"scanned": 25, "indexed": 50}

        cur = await ah_db._db.execute("SELECT COUNT(DISTINCT auction_id) FROM auction_bonuses")
        assert (await cur.fetchone())[0] == 25

    _run(scenario, tmp_path, "i.db")


def test_rebuild_is_idempotent(tmp_path):
    async def scenario():
        await ah_db.record_auctions([_auction(i, bonuses=LIVE_BONUSES) for i in range(1, 4)])
        first = await ah_db.rebuild_bonus_index()
        assert await ah_db.rebuild_bonus_index() == first

    _run(scenario, tmp_path, "j.db")


def test_health_reports_drift(tmp_path):
    """Nobody watches a derived table. "in step" has to be answerable without running a search and
    noticing the answers are wrong."""
    async def scenario():
        await ah_db.record_auctions([_auction(i, bonuses=LIVE_BONUSES) for i in range(1, 4)])
        assert (await ah_db.bonus_index_health())["in_step"] is True

        await ah_db._db.execute("DELETE FROM auction_bonuses WHERE auction_id = 2")
        await ah_db._db.commit()
        health = await ah_db.bonus_index_health()
        assert health["in_step"] is False
        assert health["auctions_indexed"] == 2 and health["auctions_with_bonuses"] == 3

    _run(scenario, tmp_path, "k.db")


def test_the_lookup_the_search_endpoint_will_make_is_indexed(tmp_path):
    """The reason this table exists: bonus id + value window, without scanning 548k JSON blobs."""
    async def scenario():
        await ah_db.record_auctions([
            _auction(1, bonuses=[{"id": 86, "title": "Disarm", "value": 5}]),
            _auction(2, bonuses=[{"id": 86, "title": "Disarm", "value": 7}]),
            _auction(3, bonuses=[{"id": 86, "title": "Disarm", "value": 12}]),
            _auction(4, bonuses=[{"id": 42, "title": "Wither", "value": 7}]),
        ])
        cur = await ah_db._db.execute(
            "SELECT auction_id FROM auction_bonuses WHERE bonus_id=? AND value BETWEEN ? AND ? "
            "ORDER BY auction_id", (86, 5, 9))
        assert [r[0] for r in await cur.fetchall()] == [1, 2]

        cur = await ah_db._db.execute(
            "EXPLAIN QUERY PLAN SELECT auction_id FROM auction_bonuses "
            "WHERE bonus_id=? AND value BETWEEN ? AND ?", (86, 5, 9))
        plan = " ".join(str(r[3]) for r in await cur.fetchall())
        assert "idx_auction_bonuses_lookup" in plan, f"query is not using the index: {plan}"

    _run(scenario, tmp_path, "l.db")


def test_live_bonus_shape_round_trips(tmp_path):
    """Against the real payload captured in tests/test_ah_live_payload.py — the Sai carries two."""
    from tests.test_ah_live_payload import ROWS

    async def scenario():
        rows = [ah_scraper.normalize(r) for r in ROWS]
        await ah_db.record_auctions(rows)
        cur = await ah_db._db.execute(
            "SELECT bonus_id, value FROM auction_bonuses WHERE auction_id=548354 ORDER BY ordinal")
        assert [tuple(r) for r in await cur.fetchall()] == [(42, 25), (101, 31)]
        assert (await ah_db.bonus_index_health())["in_step"] is True

    _run(scenario, tmp_path, "m.db")
