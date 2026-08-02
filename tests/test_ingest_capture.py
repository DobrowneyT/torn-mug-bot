"""Raw ingest capture is opt-in, non-blocking, and bounded.

It was a Phase 0 discovery aid that nothing ever turned off or cleaned up: every ingest POST wrote a
row to raw_events AND a line to a JSONL, forever, with the file write blocking the event loop the
poller and alerts run on.
"""

import asyncio
import os
import time

import config
from server import app as server_app
from server import db


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server_app, "DUMP_DIR", str(tmp_path / "dumps"))
    return TestClient(server_app.create_app(bot=None))


def _dump_files(tmp_path):
    d = tmp_path / "dumps"
    return sorted(os.listdir(d)) if d.exists() else []


def test_capture_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "off.db"))
    monkeypatch.setattr(config, "RAW_CAPTURE", False)

    async def scenario():
        await db.init_db()
        try:
            client = _client(monkeypatch, tmp_path)
            resp = client.post("/gear", json={"listingID": "L1", "item_id": 399, "lister": "42"})
            assert resp.status_code == 200 and resp.json()["ok"] is True

            assert await db._scalar1("SELECT COUNT(*) FROM raw_events") == 0
            assert _dump_files(tmp_path) == []
            # ...and the capture itself was still processed
            assert await db.get_listing("L1") is not None
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_capture_on_writes_both_sinks(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "on.db"))
    monkeypatch.setattr(config, "RAW_CAPTURE", True)

    async def scenario():
        await db.init_db()
        try:
            client = _client(monkeypatch, tmp_path)
            client.post("/market", json={"channel": "item-market_206", "data": []})
            assert await db._scalar1("SELECT COUNT(*) FROM raw_events") == 1
            files = _dump_files(tmp_path)
            assert len(files) == 1 and files[0].startswith("market-")
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_prune_removes_only_rows_past_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "prune.db"))
    monkeypatch.setattr(config, "RAW_RETENTION_DAYS", 7)

    async def scenario():
        await db.init_db()
        try:
            now = int(time.time())
            async with db.batch():
                for age_days, source in ((0, "fresh"), (6, "just-inside"), (8, "old"), (30, "ancient")):
                    await db._db.execute(
                        "INSERT INTO raw_events(source, channel, payload, received_at) VALUES (?,?,?,?)",
                        (source, None, "{}", now - age_days * 86400),
                    )
            assert await db._scalar1("SELECT COUNT(*) FROM raw_events") == 4

            assert await db.prune_raw_events() == 2
            cur = await db._db.execute("SELECT source FROM raw_events ORDER BY source")
            assert [r[0] for r in await cur.fetchall()] == ["fresh", "just-inside"]

            assert await db.prune_raw_events() == 0      # idempotent
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_retention_of_zero_disables_pruning(tmp_path, monkeypatch):
    """0 must mean "keep everything", not "delete everything" — an easy and destructive sign error."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "keep.db"))

    async def scenario():
        await db.init_db()
        try:
            await db._db.execute(
                "INSERT INTO raw_events(source, channel, payload, received_at) VALUES (?,?,?,?)",
                ("old", None, "{}", int(time.time()) - 999 * 86400),
            )
            await db._commit()
            assert await db.prune_raw_events(0) == 0
            assert await db._scalar1("SELECT COUNT(*) FROM raw_events") == 1
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_dump_write_happens_off_the_event_loop(tmp_path, monkeypatch):
    """A blocking write in an async handler stalls the poller and any in-flight alert."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "thread.db"))
    monkeypatch.setattr(config, "RAW_CAPTURE", True)

    async def scenario():
        await db.init_db()
        try:
            threads = []
            real = server_app.asyncio.to_thread

            async def spy(fn, *a, **k):
                threads.append(fn.__name__)
                return await real(fn, *a, **k)

            monkeypatch.setattr(server_app.asyncio, "to_thread", spy)
            client = _client(monkeypatch, tmp_path)
            client.post("/items", json={"listings": []})
            assert threads == ["_write_dump"]
        finally:
            await db.close_db()

    asyncio.run(scenario())
