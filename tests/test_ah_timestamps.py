"""Undatable auctions must not enter the ledger, and must never steer the backfill.

Torn serves some auctions with an epoch-adjacent `timestamp` — a real auction, real uid, real buyer,
unusable date. The first full backfill against live Torn ended at
`history reaches back to 1970-01-01 00:00:01Z`, which is how they were found.

  DATA (confirmed)   ownership is "most recent wins" and #44's confidence is a function of evidence
                     age, so a win that cannot be placed in time is not evidence. Storing them also
                     wrecks MIN(ended_at) — which is both the reported coverage span AND the floor a
                     resumed backfill would start from, so a re-walk would request `to=1` and
                     instantly "complete" having fetched nothing.

  CURSOR (defensive) the backfill bounds its next request on the oldest timestamp it saw, so an
                     epoch row can drag the cursor to 1970. Measured against the real API this costs
                     wasted requests and NOT data: Torn sorts DESC by timestamp, so epoch rows come
                     after every datable auction and there is nothing left below them to skip. The
                     guard is kept because that safety rests entirely on an ordering guarantee Torn
                     never made, and the failure if it changed would be silent.
"""

import asyncio
import time

import config
from server import ah_db, ah_scraper, torn

REAL = 1_700_000_000          # 2023-11-14
EPOCH_ROWS = (1, 44, 100)     # the shapes actually observed


def _api_row(auction_id, ended_at, *, uid=None):
    return {
        "id": auction_id,
        "seller": {"id": 100, "name": "Seller"},
        "buyer": {"id": 500, "name": "Buyer"},
        "timestamp": ended_at,
        "price": 1_000_000,
        "bids": 4,
        "item": {"uid": uid if uid is not None else auction_id, "id": 399, "name": "M-15A4",
                 "type": "Weapon", "sub_type": "Rifle",
                 "stats": {"damage": 1.0, "accuracy": 1.0, "armor": None, "quality": 1.0},
                 "bonuses": None, "rarity": None},
    }


class _FakeApi:
    def __init__(self, auctions):
        self.auctions = sorted(auctions, key=lambda r: r["timestamp"])
        self.calls = []

    def install(self, monkeypatch):
        async def get_auctionhouse(client, *, item_id=None, limit=100, sort="DESC",
                                   from_ts=None, to_ts=None):
            self.calls.append({"sort": sort, "from": from_ts, "to": to_ts, "limit": limit})
            rows = self.auctions
            if from_ts is not None:
                rows = [r for r in rows if r["timestamp"] >= from_ts]
            if to_ts is not None:
                rows = [r for r in rows if r["timestamp"] <= to_ts]
            return sorted(rows, key=lambda r: r["timestamp"], reverse=(sort == "DESC"))[:limit]

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


# --------------------------------- ingest ----------------------------------------------

def test_undatable_auctions_are_dropped_at_ingest():
    for ts in EPOCH_ROWS:
        assert ah_scraper.normalize(_api_row(1, ts)) is None, f"stored an auction dated {ts}"


def test_datable_auctions_are_still_kept():
    """The floor must not be so aggressive it eats real history. Torn launched in 2004."""
    assert ah_scraper.normalize(_api_row(1, config.AH_MIN_TIMESTAMP)) is not None
    assert ah_scraper.normalize(_api_row(1, REAL))["ended_at"] == REAL


def test_the_floor_is_before_any_auction_torn_could_have():
    assert config.AH_MIN_TIMESTAMP <= 1_072_915_200, "floor is after 2004 — would discard real data"
    assert config.AH_MIN_TIMESTAMP > 0


# --------------------------------- the cursor ------------------------------------------

def test_the_cursor_is_never_bound_below_the_floor(tmp_path, monkeypatch):
    """Defence in depth, and labelled as such rather than as a fixed data-loss bug.

    Measured against the real ordering: Torn sorts DESC by timestamp, so epoch rows always come
    AFTER every datable auction. By the time one appears there is nothing left below it to skip — so
    on today's API this costs wasted requests (`to=72`, `to=23` were observed in simulation), not
    data. The guard is worth having because that safety rests entirely on an ordering guarantee
    Torn never made, and the failure if it changed would be silent.
    """
    rows = [_api_row(i, REAL - i * 86400) for i in range(1, 51)]
    rows += [_api_row(9000 + k, k + 1) for k in range(120)]     # the epoch tail, fills a full page
    api = _FakeApi(rows)
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        result = await ah_scraper.scrape_backfill(None, limit=50, max_pages=40)
        assert result["complete"] is True
        assert await ah_db.count_auctions() == 50, "an undatable auction reached the ledger"
        below = [c["to"] for c in api.calls
                 if c["to"] is not None and c["to"] < config.AH_MIN_TIMESTAMP]
        assert not below, f"cursor was dragged below the floor: {below}"

    _run(scenario, tmp_path)


def test_a_page_of_only_undatable_rows_ends_the_walk(tmp_path, monkeypatch):
    """The legitimate floor: once Torn is serving nothing but epoch rows, that IS the end of usable
    history and the walk should say so rather than spin."""
    rows = [_api_row(i, REAL - i * 86400) for i in range(1, 51)]
    rows += [_api_row(9000 + i, i + 1) for i in range(120)]
    api = _FakeApi(rows)
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        result = await ah_scraper.scrape_backfill(None, limit=50, max_pages=40)
        assert result["complete"] is True
        assert await ah_db.count_auctions() == 50, "kept an undatable auction"
        assert (await ah_db.oldest_ended_at()) >= config.AH_MIN_TIMESTAMP

    _run(scenario, tmp_path)


def test_restart_rewalks_from_the_newest_end(tmp_path, monkeypatch):
    """After a derailed walk the completed flag is set and the floor is wrong, so resuming from the
    floor would re-confirm the bad answer. A restart has to start over from the top."""
    api = _FakeApi([_api_row(i, REAL - i * 86400) for i in range(1, 250)])
    api.install(monkeypatch)
    _no_sleep(monkeypatch)

    async def scenario():
        await ah_ledger_marked_complete()
        api.calls.clear()

        result = await ah_scraper.scrape_backfill(None, limit=100, max_pages=40, restart=True)
        assert api.calls[0]["to"] is None, "restart resumed instead of starting over"
        assert result["complete"] is True
        assert await ah_db.count_auctions() == 249

    async def ah_ledger_marked_complete():
        await ah_db.record_auctions([ah_scraper.normalize(_api_row(1, REAL - 86400))])
        await ah_db.set_state("backfill_complete", True)
        # without --restart this is a no-op, which is the behaviour being overridden
        assert (await ah_scraper.scrape_backfill(None, limit=100))["pages"] == 0

    _run(scenario, tmp_path)


# --------------------------------- diagnostics -----------------------------------------

def test_status_flags_undatable_rows_already_in_the_ledger(tmp_path, monkeypatch, capsys):
    """The ingest guard stops new ones; rows stored before it existed still break MIN(ended_at),
    and nothing else in the tool would ever mention them."""
    async def scenario():
        # bypass normalize — these are rows a pre-guard build would have written
        await ah_db.record_auctions([{
            "auction_id": 1, "uid": "u", "item_id": 1, "item_name": "x", "item_type": None,
            "sub_type": None, "damage": None, "accuracy": None, "armor": None, "quality": None,
            "rarity": None, "bonuses": None, "seller_id": 1, "seller_name": "s",
            "buyer_id": 2, "buyer_name": "b", "price": 1, "bids": 1, "ended_at": 1,
        }])
        await ah_scraper.show_status()
        out = capsys.readouterr().out
        assert "WARNING" in out and "DELETE FROM auction_wins" in out

    _run(scenario, tmp_path)


def test_status_shows_a_year_histogram_with_gaps_marked(tmp_path, monkeypatch, capsys):
    """'complete' is not evidence a walk covered everything — a derailed one reports complete too.
    A year with nothing between two with plenty is the only visible signature."""
    async def scenario():
        rows = []
        for year, ts in ((2022, 1_640_995_200), (2024, 1_704_067_200)):   # 2023 deliberately absent
            for i in range(3):
                rows.append({
                    "auction_id": year * 10 + i, "uid": f"{year}{i}", "item_id": 1,
                    "item_name": "x", "item_type": None, "sub_type": None, "damage": None,
                    "accuracy": None, "armor": None, "quality": None, "rarity": None,
                    "bonuses": None, "seller_id": 1, "seller_name": "s", "buyer_id": 2,
                    "buyer_name": "b", "price": 1, "bids": 1, "ended_at": ts + i,
                })
        await ah_db.record_auctions(rows)
        await ah_scraper.show_status()
        out = capsys.readouterr().out
        assert "2023" in out and "<-- GAP" in out
        gap_line = [ln for ln in out.splitlines() if "GAP" in ln][0]
        assert "2023" in gap_line, gap_line

    _run(scenario, tmp_path)


def test_year_histogram_is_empty_for_an_empty_ledger(tmp_path, capsys):
    async def scenario():
        assert await ah_db.auctions_by_year() == []
        await ah_scraper.show_status()          # must not raise

    _run(scenario, tmp_path)


# --------------------------------- active vs ended-unsold -------------------------------

def test_probe_distinguishes_a_running_auction_from_one_that_ended_unsold(capsys, monkeypatch):
    """A row with no buyer is ambiguous, and the schema cannot disambiguate it — so the probe reads
    it off `timestamp` and says which reading each row supports.

    Against live Torn the answer was "neither": the epoch rows are 2013 seed data on staff accounts,
    and the feed carries no active auctions at all. The probe still has to handle a future timestamp
    correctly, because that is the case that would prove otherwise if Torn ever changes.
    """
    now = int(time.time())
    rows = [
        dict(_api_row(1, now + 7200), buyer=None),          # ends in 2h -> running
        dict(_api_row(2, 1), buyer=None),                   # epoch -> 2013 seed data
        dict(_api_row(3, now - 30 * 86400), buyer=None),    # real past date -> genuinely unsold
    ]
    for r in rows:
        r["buyer"] = None

    async def fake(client, *, item_id=None, limit=100, sort="DESC", from_ts=None, to_ts=None):
        assert sort == "ASC", "the no-buyer rows sort at the OLD end; probe must ask for them"
        return rows

    monkeypatch.setattr(ah_scraper.torn, "get_auctionhouse", fake)
    asyncio.run(ah_scraper.probe(None, limit=10, oldest=True))
    out = capsys.readouterr().out

    assert "3 without a buyer" in out
    assert "STILL RUNNING" in out          # the future-dated row
    assert "FUTURE" in out
    assert "genuinely ENDED UNSOLD" in out


def test_probe_says_so_when_a_page_has_no_ambiguous_rows(capsys, monkeypatch):
    async def fake(client, **kwargs):
        return [_api_row(1, 1_700_000_000)]

    monkeypatch.setattr(ah_scraper.torn, "get_auctionhouse", fake)
    asyncio.run(ah_scraper.probe(None, limit=1))
    assert "try --oldest" in capsys.readouterr().out
