"""Scrape walks: resumable, bounded, and unable to spin.

The two failure modes that matter are both silent. A cursor that fails to advance re-requests the
same page until the page cap stops it — burning key budget the poller needs and never progressing.
A cursor derived from rows we *stored* rather than rows we *saw* stalls on any page whose boundary
row we dropped. Neither raises anything; both just quietly stop collecting data.
"""

import asyncio

import config
from server import ah_db, ah_scraper, torn


def _api_row(auction_id, ended_at, *, uid=None, buyer=True, item=None):
    """One row in the shape the auctionhouse endpoint returns."""
    return {
        "id": auction_id,
        "seller": {"id": 100, "name": "Seller"},
        "buyer": {"id": 500, "name": "Buyer"} if buyer else None,
        "timestamp": ended_at,
        "price": 1_000_000,
        "bids": 4,
        "item": item if item is not None else {
            "uid": uid, "id": 399, "name": "ArmaLite M-15A4", "type": "Weapon", "sub_type": "Rifle",
            "stats": {"damage": 55.5, "accuracy": 44.4, "armor": None, "quality": 99.9},
            "bonuses": [{"id": 1, "title": "Puncture", "description": "…", "value": 20}],
            "rarity": "yellow",
        },
    }


class _FakeApi:
    """Serves pages from a fixed set of auctions, honouring sort/from/to/limit like Torn does.

    Recording the calls matters as much as the data: several of these tests are about how many
    requests a walk makes and with what bounds, not only about what ends up stored.
    """

    def __init__(self, auctions, *, fail_after=None):
        self.auctions = sorted(auctions, key=lambda r: r["timestamp"])
        self.calls = []
        self.fail_after = fail_after

    def install(self, monkeypatch):
        async def get_auctionhouse(client, *, item_id=None, limit=100, sort="DESC",
                                   from_ts=None, to_ts=None):
            self.calls.append({"sort": sort, "from": from_ts, "to": to_ts, "limit": limit})
            if self.fail_after is not None and len(self.calls) > self.fail_after:
                return None                                    # transient API failure
            rows = self.auctions
            if from_ts is not None:
                rows = [r for r in rows if r["timestamp"] >= from_ts]
            if to_ts is not None:
                rows = [r for r in rows if r["timestamp"] <= to_ts]
            rows = sorted(rows, key=lambda r: r["timestamp"], reverse=(sort == "DESC"))
            return rows[:limit]

        monkeypatch.setattr(torn, "get_auctionhouse", get_auctionhouse)
        monkeypatch.setattr(ah_scraper.torn, "get_auctionhouse", get_auctionhouse)


def _run(scenario, tmp_path, name="ah.db"):
    async def wrapper():
        await ah_db.init_db(str(tmp_path / name))
        try:
            await scenario()
        finally:
            await ah_db.close_db()

    asyncio.run(wrapper())


def _no_sleep(monkeypatch):
    async def sleep(_):
        return None
    monkeypatch.setattr(ah_scraper.asyncio, "sleep", sleep)


# --------------------------------- normalisation ---------------------------------------

def test_normalize_maps_a_gear_auction():
    row = ah_scraper.normalize(_api_row(7, 1_700_000_000, uid=123456789))
    assert row["auction_id"] == 7
    assert row["uid"] == "123456789", "uid must be TEXT to join the main database"
    assert row["item_id"] == 399 and row["damage"] == 55.5 and row["rarity"] == "yellow"
    assert row["buyer_id"] == 500 and row["seller_id"] == 100
    assert row["ended_at"] == 1_700_000_000
    assert '"Puncture"' in row["bonuses"]


def test_normalize_maps_a_stackable_auction():
    """The other branch of the `item` oneOf: uid and name, no stats block."""
    row = ah_scraper.normalize(_api_row(8, 1_700_000_000, item={
        "id": 206, "uid": 55, "name": "Xanax", "type": "Drug"}))
    assert row["uid"] == "55" and row["item_id"] == 206 and row["item_name"] == "Xanax"
    assert row["damage"] is None and row["bonuses"] is None


def test_normalize_drops_rows_with_no_buyer():
    """An auction that expired unsold transfers nothing. The spec marks `buyer` required, but a
    NOT NULL failure mid-page would abort a whole scrape over a row we did not want anyway."""
    assert ah_scraper.normalize(_api_row(9, 1_700_000_000, buyer=False)) is None
    assert ah_scraper.normalize({"id": 1, "timestamp": 1, "buyer": {"id": 0}}) is None
    assert ah_scraper.normalize({"id": 1, "buyer": {"id": 5}}) is None          # no timestamp


# --------------------------------- forward ---------------------------------------------

def test_forward_seeds_from_the_newest_page_when_the_ledger_is_empty(tmp_path, monkeypatch):
    """A routine cron tick on a fresh box must not walk the entire history — that is `backfill`,
    and it is opt-in precisely so it cannot happen by accident."""
    api = _FakeApi([_api_row(i + 1, 1_700_000_000 + i * 60, uid=i) for i in range(500)])
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        result = await ah_scraper.scrape_forward(None, limit=100, max_pages=40)
        assert result["seeded"] is True and result["pages"] == 1
        assert len(api.calls) == 1 and api.calls[0]["sort"] == "DESC"
        assert await ah_db.count_auctions() == 100

    _run(scenario, tmp_path)


def test_forward_pages_until_it_catches_up(tmp_path, monkeypatch):
    api = _FakeApi([_api_row(i + 1, 1_700_000_000 + i * 60, uid=i) for i in range(250)])
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        # pretend we already hold the oldest auction, so forward mode has a watermark to work from
        await ah_db.record_auctions([ah_scraper.normalize(api.auctions[0])])
        result = await ah_scraper.scrape_forward(None, limit=100, max_pages=40, overlap=0)

        assert [c["sort"] for c in api.calls] == ["ASC"] * 3
        assert result["new"] == 249 and await ah_db.count_auctions() == 250
        # each page picks up where the last ended
        assert api.calls[0]["from"] == 1_700_000_000
        assert api.calls[1]["from"] == api.auctions[99]["timestamp"]

    _run(scenario, tmp_path)


def test_forward_respects_the_page_cap_and_resumes(tmp_path, monkeypatch):
    """Hitting the cap is not a failure — the next run must continue, not restart."""
    api = _FakeApi([_api_row(i + 1, 1_700_000_000 + i * 60, uid=i) for i in range(250)])
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        await ah_db.record_auctions([ah_scraper.normalize(api.auctions[0])])
        first = await ah_scraper.scrape_forward(None, limit=100, max_pages=1, overlap=0)
        assert first["pages"] == 1 and await ah_db.count_auctions() == 100

        second = await ah_scraper.scrape_forward(None, limit=100, max_pages=40, overlap=0)
        assert second["pages"] == 2 and await ah_db.count_auctions() == 250

    _run(scenario, tmp_path)


def test_forward_overlap_refetches_the_boundary_without_duplicating(tmp_path, monkeypatch):
    """`from` may or may not be inclusive, and auctions share end timestamps. Re-requesting a few
    seconds is the cheap answer — but only because a re-delivered auction is a no-op insert."""
    api = _FakeApi([_api_row(i + 1, 1_700_000_000 + i, uid=i) for i in range(10)])
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        await ah_db.record_auctions([ah_scraper.normalize(r) for r in api.auctions])
        assert await ah_db.count_auctions() == 10

        result = await ah_scraper.scrape_forward(None, limit=100, max_pages=40, overlap=5)
        assert api.calls[0]["from"] == 1_700_000_009 - 5, "overlap window not applied"
        assert result["seen"] == 6 and result["new"] == 0, "re-fetched rows must not duplicate"
        assert await ah_db.count_auctions() == 10

    _run(scenario, tmp_path)


def test_forward_stops_rather_than_spinning_on_a_page_that_cannot_advance(tmp_path, monkeypatch):
    """Every row sharing one timestamp means the next request returns exactly this page again.
    Without the guard the walk re-requests it until the page cap, burning key budget."""
    api = _FakeApi([_api_row(i + 1, 1_700_000_000, uid=i) for i in range(5)])
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        await ah_db.record_auctions([ah_scraper.normalize(api.auctions[0])])
        result = await ah_scraper.scrape_forward(None, limit=5, max_pages=40, overlap=0)
        assert len(api.calls) == 1, f"walk did not stop: {len(api.calls)} requests"
        assert result["pages"] == 1

    _run(scenario, tmp_path)


def test_forward_cursor_advances_over_rows_it_chose_not_to_store(tmp_path, monkeypatch):
    """The newest row on a page can be one we drop (an unsold auction). Deriving the cursor from
    stored rows would leave it behind that row and re-request the same page forever."""
    rows = [_api_row(i + 1, 1_700_000_000 + i * 60, uid=i) for i in range(100)]
    rows[-1] = _api_row(99, 1_700_000_000 + 99 * 60, buyer=False)     # newest row is unsellable
    rows.append(_api_row(100, 1_700_000_000 + 100 * 60, uid=100))
    api = _FakeApi(rows)
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        await ah_db.record_auctions([ah_scraper.normalize(api.auctions[0])])
        result = await ah_scraper.scrape_forward(None, limit=100, max_pages=40, overlap=0)
        assert api.calls[1]["from"] == 1_700_000_000 + 99 * 60, "cursor stalled on a dropped row"
        assert result["new"] == 99          # 100 fetched rows minus the seed, minus the unsold one
        assert await ah_db.count_auctions() == 100

    _run(scenario, tmp_path)


def test_a_failed_request_raises_rather_than_looking_like_the_end(tmp_path, monkeypatch):
    """An empty page means "end of history" and stops the walk. An API error must never be read
    that way, or a transient failure would permanently truncate the backfill."""
    api = _FakeApi([_api_row(i + 1, 1_700_000_000 + i * 60, uid=i) for i in range(250)], fail_after=1)
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        await ah_db.record_auctions([ah_scraper.normalize(api.auctions[0])])
        try:
            await ah_scraper.scrape_forward(None, limit=100, max_pages=40, overlap=0)
            raise AssertionError("a failed request was swallowed")
        except ah_scraper.ScrapeError:
            pass
        assert await ah_db.count_auctions() == 100, "the page that DID succeed must still be stored"
        assert await ah_db.get_state("backfill_complete", False) is False

    _run(scenario, tmp_path)


# --------------------------------- backfill --------------------------------------------

def test_backfill_walks_backwards_and_records_that_history_ran_out(tmp_path, monkeypatch):
    """How far back Torn will serve is undocumented, so the walk discovers it: it stops when a page
    comes back short and records the depth it reached."""
    api = _FakeApi([_api_row(i + 1, 1_700_000_000 + i * 60, uid=i) for i in range(250)])
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        result = await ah_scraper.scrape_backfill(None, limit=100, max_pages=40)
        assert result["complete"] is True and result["pages"] == 3
        assert [c["sort"] for c in api.calls] == ["DESC"] * 3
        assert api.calls[0]["to"] is None
        assert await ah_db.count_auctions() == 250
        assert await ah_db.oldest_ended_at() == 1_700_000_000
        assert await ah_db.get_state("backfill_complete", False) is True

    _run(scenario, tmp_path)


def test_backfill_resumes_below_where_it_stopped(tmp_path, monkeypatch):
    api = _FakeApi([_api_row(i + 1, 1_700_000_000 + i * 60, uid=i) for i in range(250)])
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        first = await ah_scraper.scrape_backfill(None, limit=100, max_pages=1)
        assert first["complete"] is False and await ah_db.count_auctions() == 100
        floor = await ah_db.oldest_ended_at()

        api.calls.clear()
        second = await ah_scraper.scrape_backfill(None, limit=100, max_pages=40)
        assert api.calls[0]["to"] == floor - 1, "resumed from the wrong point"
        assert second["complete"] is True and await ah_db.count_auctions() == 250

    _run(scenario, tmp_path)


def test_backfill_is_a_no_op_once_complete(tmp_path, monkeypatch):
    """Left on a cron, a finished backfill must stop making requests rather than re-walking."""
    api = _FakeApi([_api_row(i + 1, 1_700_000_000 + i * 60, uid=i) for i in range(50)])
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        assert (await ah_scraper.scrape_backfill(None, limit=100, max_pages=40))["complete"] is True
        api.calls.clear()
        result = await ah_scraper.scrape_backfill(None, limit=100, max_pages=40)
        assert api.calls == [] and result["complete"] is True and result["pages"] == 0

    _run(scenario, tmp_path)


# --------------------------------- run wrapper -----------------------------------------

def test_run_logs_success(tmp_path, monkeypatch):
    api = _FakeApi([_api_row(i + 1, 1_700_000_000 + i * 60, uid=i) for i in range(10)])
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        result = await ah_scraper.run("forward")
        assert result["new"] == 10
        run = (await ah_db.recent_runs())[0]
        assert run["mode"] == "forward" and run["ended_at"] is not None
        assert run["rows_new"] == 10 and run["error"] is None

    _run(scenario, tmp_path)


def test_run_records_the_failure_instead_of_leaving_a_dangling_row(tmp_path, monkeypatch):
    """A run that dies has to be distinguishable from one that is still going — that is the whole
    reason the log exists. Nobody watches a scraper."""
    api = _FakeApi([_api_row(i + 1, 1_700_000_000 + i * 60, uid=i) for i in range(10)], fail_after=0)
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        try:
            await ah_scraper.run("forward")
            raise AssertionError("failure was swallowed")
        except ah_scraper.ScrapeError:
            pass
        run = (await ah_db.recent_runs())[0]
        assert run["ended_at"] is not None and "ScrapeError" in run["error"]

    _run(scenario, tmp_path)


def test_a_scraped_auction_joins_to_the_main_database_end_to_end(tmp_path, monkeypatch):
    """The join #12 depends on, exercised through the real path rather than a hand-built row: API
    response -> normalize -> ledger -> looked up by the uid form the main database holds.

    Note this passes even if normalize stops calling str(): SQLite's TEXT affinity coerces the
    integer on the way in, and applies to the bound parameter on the way out. The uid convention is
    enforced by the normalize unit tests, because the place it actually matters is Python — the
    `uid in tracked` / `in anon` membership tests in reconcile_gear that #12 hooks into. This test
    covers the storage path; it is not the guard on the type.
    """
    from server import db

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "main.db"))
    api = _FakeApi([_api_row(1, 1_700_000_000, uid=123456789)])     # integer uid, as Torn sends it
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        await ah_db.init_db(str(tmp_path / "ah.db"))
        await db.init_db()
        try:
            await ah_scraper.scrape_forward(None, limit=100, max_pages=1)
            # the poller records the same weapon appearing on the item market
            await db.upsert_tracked_gear(uid=123456789, item_id=399, item_name="ArmaLite M-15A4",
                                         price=1000, seller_id=None)
            cur = await db._db.execute("SELECT uid FROM tracked_gear")
            main_uid = (await cur.fetchone())[0]

            win = await ah_db.owner_of_uid(main_uid)
            assert win is not None, "scraped auction did not join to the main database's uid"
            assert win["buyer_id"] == 500
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(scenario())


def test_page_delay_is_configured_low_enough_to_not_stall_a_run():
    """The delay exists to stop a cron burst starving the poller's key budget, not to throttle
    meaningfully — 40 pages must stay well inside a cron interval."""
    assert 0 <= config.AH_PAGE_DELAY_SECONDS <= 2
    assert config.AH_PAGE_LIMIT <= 100, "Torn caps limit at 100; a larger value is silently clamped"
