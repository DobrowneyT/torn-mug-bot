"""The machine-readable schema, and the guard that keeps it honest.

Column descriptions live in Python rather than being parsed out of ah_schema.sql's comments, so
they can drift away from the table without anything failing. The test below is the whole reason
that is acceptable: a new column cannot ship undocumented.
"""

import asyncio
import json

from server import ah_db, ah_scraper


def _run(scenario, tmp_path, name="s.db"):
    async def wrapper():
        await ah_db.init_db(str(tmp_path / name))
        try:
            await scenario()
        finally:
            await ah_db.close_db()

    asyncio.run(wrapper())


def test_every_column_of_the_data_table_is_documented(tmp_path):
    """The anti-drift guard. Add a column without a description and this fails — which is the only
    thing stopping COLUMN_DOCS from quietly rotting away from the schema it describes."""
    async def scenario():
        schema = await ah_db.table_schema(ah_db.DATA_TABLE)
        undocumented = [c["name"] for c in schema["columns"] if not c["description"]]
        assert not undocumented, f"columns with no description: {undocumented}"

    _run(scenario, tmp_path)


def test_no_description_describes_a_column_that_no_longer_exists(tmp_path):
    """The other direction: a renamed or dropped column leaves a stale entry describing nothing."""
    async def scenario():
        schema = await ah_db.table_schema(ah_db.DATA_TABLE)
        real = {c["name"] for c in schema["columns"]}
        stale = set(ah_db.COLUMN_DOCS) - real
        assert not stale, f"descriptions for columns that do not exist: {sorted(stale)}"

    _run(scenario, tmp_path)


def test_the_null_semantics_are_actually_stated(tmp_path):
    """The three NULLs that mean something rather than nothing. An endpoint built without these
    produces confidently wrong aggregates — no error, just wrong numbers."""
    async def scenario():
        docs = {c["name"]: c["description"]
                for c in (await ah_db.table_schema(ah_db.DATA_TABLE))["columns"]}
        assert "ENDED UNSOLD" in docs["buyer_id"]
        assert "NO uid" in docs["uid"]
        assert "0 bids" in docs["price"]
        # and the timestamp pair, which is easy to conflate
        assert "TORN" in docs["ended_at"] and "OUR" in docs["seen_at"]

    _run(scenario, tmp_path)


def test_schema_reports_indexes_and_coverage(tmp_path):
    """Someone building endpoints needs to know what is cheap to query and how much is there."""
    async def scenario():
        schema = await ah_db.table_schema(ah_db.DATA_TABLE)
        by_index = {i["name"]: i["columns"] for i in schema["indexes"]}
        assert by_index["idx_auction_wins_uid"] == ["uid", "ended_at"]
        assert "idx_auction_wins_buyer" in by_index
        assert schema["row_count"] == 0
        assert set(schema["coverage"]) == {
            "unsold", "without_uid", "earliest", "latest", "last_scraped"}

    _run(scenario, tmp_path)


def test_bookkeeping_tables_carry_no_coverage_block(tmp_path):
    """`coverage` only means something for the data table; emitting it for ah_runs would invite
    someone to read scraper bookkeeping as market data."""
    async def scenario():
        assert "coverage" not in await ah_db.table_schema("ah_runs")

    _run(scenario, tmp_path)


def test_default_output_is_the_data_table_only(tmp_path, capsys, monkeypatch):
    """Torn's data is the only thing an external consumer wants. ah_state and ah_runs are scraper
    bookkeeping and contain nothing Torn said."""
    async def scenario():
        await ah_scraper.show_schema()
        out = json.loads(capsys.readouterr().out)
        assert [t["name"] for t in out["tables"]] == ["auction_wins"]
        assert "auctionhouse" in out["source"]

        await ah_scraper.show_schema(all_tables=True)
        names = [t["name"] for t in json.loads(capsys.readouterr().out)["tables"]]
        # auction_bonuses is a DERIVED index over auction_wins.bonuses, not a data table — it is
        # bookkeeping in the same sense as ah_state, so it belongs behind --all-tables too.
        assert set(names) == {"auction_wins", "auction_bonuses", "ah_state", "ah_runs"}

    _run(scenario, tmp_path, "s2.db")


def test_output_is_valid_json_and_round_trips(tmp_path, capsys):
    """It exists to be consumed by a program, so 'looks fine' is not the bar."""
    async def scenario():
        await ah_scraper.show_schema(all_tables=True)
        raw = capsys.readouterr().out
        parsed = json.loads(raw)
        assert json.loads(json.dumps(parsed)) == parsed

    _run(scenario, tmp_path, "s3.db")
