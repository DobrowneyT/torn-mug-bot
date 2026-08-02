"""Hot-path caches in server.db: they must be fast, correct, and invalidated.

A stale cache here is worse than a slow query — a /watch add that doesn't take effect, or an anon
uid the poller keeps re-classifying, are silent wrong behaviour rather than visible slowness.
"""

import asyncio

import config
from server import api_poller, db


class _SQLRecorder:
    """Records the SQL issued while active, so a test can assert a query is NOT re-run."""

    def __init__(self):
        self.statements = []

    def __enter__(self):
        self._real = db._db.execute

        async def spy(sql, *args, **kwargs):
            self.statements.append(sql)
            return await self._real(sql, *args, **kwargs)

        db._db.execute = spy
        return self

    def __exit__(self, *exc):
        db._db.execute = self._real
        return False

    def matching(self, needle):
        return [s for s in self.statements if needle in s]


def test_anon_set_is_read_once_then_served_from_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "anon.db"))

    async def scenario():
        await db.init_db()
        try:
            for i in range(5):
                await db.mark_anon_uid(uid=f"u{i}")
            assert await db.get_anon_uid_set() == {"u0", "u1", "u2", "u3", "u4"}

            with _SQLRecorder() as rec:
                for _ in range(20):          # stands in for one poll cycle over many items
                    await db.get_anon_uid_set()
                assert rec.matching("FROM anon_uids") == []
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_marking_an_anon_uid_updates_the_cache_without_a_reread(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "anon2.db"))

    async def scenario():
        await db.init_db()
        try:
            await db.get_anon_uid_set()      # prime
            with _SQLRecorder() as rec:
                await db.mark_anon_uid(uid="fresh")
                assert "fresh" in await db.get_anon_uid_set()
                assert rec.matching("SELECT uid FROM anon_uids") == []
            # and it survives a round trip through the DB layer
            assert "fresh" in await db.get_anon_uid_set()
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_anon_cache_does_not_leak_between_databases(tmp_path, monkeypatch):
    """Each test/DB gets its own file; a cache surviving init_db would silently poison the next."""

    async def scenario():
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "one.db"))
        await db.init_db()
        await db.mark_anon_uid(uid="from-first-db")
        assert "from-first-db" in await db.get_anon_uid_set()
        await db.close_db()

        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "two.db"))
        await db.init_db()
        try:
            assert await db.get_anon_uid_set() == frozenset()
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_reconcile_classifies_anon_without_touching_the_table(tmp_path, monkeypatch):
    """The real hot path: reconcile_gear asked the DB once per item purely for this membership test."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "recon.db"))

    async def scenario():
        await db.init_db()
        try:
            await db.mark_anon_uid(uid="333")
            await db.get_anon_uid_set()      # prime, as a running poller would have
            with _SQLRecorder() as rec:
                await api_poller.reconcile_gear(
                    None, 399, {"333": {"price": 300, "stats": None}}, "ArmaLite M-15A4")
                assert rec.matching("FROM anon_uids") == []
            assert await db.unmatched_count() == 0      # classified anon, not left unmatched
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_watch_add_takes_effect_on_the_next_tick(tmp_path, monkeypatch):
    """Invalidation that matters operationally: /watch add must not wait for a restart."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "watch.db"))

    async def scenario():
        await db.init_db()
        try:
            await db.watchlist_add(206, "Xanax", is_gear=False)
            assert [t[0] for t in await db.poll_targets()] == [206]

            await db.watchlist_add(399, "ArmaLite M-15A4", is_gear=True)
            assert sorted(t[0] for t in await db.poll_targets()) == [206, 399]

            await db.watchlist_set_enabled(399, False)
            assert [t[0] for t in await db.poll_targets()] == [206]

            await db.watchlist_remove(206)
            assert await db.poll_targets() == []
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_repeat_polls_stop_rewriting_unchanged_is_gear_and_category(tmp_path, monkeypatch):
    """mark_polled re-sends is_gear/category every poll; once settled they must not be written
    again, or the poll-target cache would be invalidated on every poll and never serve anything."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "polled.db"))

    async def scenario():
        await db.init_db()
        try:
            await db.watchlist_add(399, "ArmaLite M-15A4")
            # First poll legitimately writes is_gear/category and invalidates; the tick after it
            # rebuilds the cache. Steady state starts from there, so prime before measuring.
            await db.watchlist_mark_polled(399, 10, is_gear=True, category="Primary")
            await db.poll_targets()

            with _SQLRecorder() as rec:
                for _ in range(5):
                    await db.watchlist_mark_polled(399, 10, is_gear=True, category="Primary")
                assert rec.matching("SET is_gear=") == []
                assert rec.matching("SET category=") == []
                assert rec.matching("FROM watchlist") == []      # cache served the comparison

            # ...but a genuine change is still written, and visible immediately
            await db.watchlist_mark_polled(399, 10, is_gear=True, category="Secondary")
            assert [t[2] for t in await db.poll_targets()] == ["Secondary"]
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_gear_all_bonus_is_cached_and_invalidated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bonus.db"))

    async def scenario():
        await db.init_db()
        try:
            await db.add_gear_all_bonus("Melee")
            assert await db.get_gear_all_bonus() == ["Melee"]

            with _SQLRecorder() as rec:
                for _ in range(10):
                    await db.get_gear_all_bonus()
                assert rec.matching("FROM config") == []

            await db.remove_gear_all_bonus("Melee")          # /gearbonus remove
            assert await db.get_gear_all_bonus() == []
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_gear_all_bonus_caller_cannot_corrupt_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bonus2.db"))

    async def scenario():
        await db.init_db()
        try:
            await db.add_gear_all_bonus("Melee")
            got = await db.get_gear_all_bonus()
            got.append("Primary")                     # a caller mutating what it was handed
            assert await db.get_gear_all_bonus() == ["Melee"]
        finally:
            await db.close_db()

    asyncio.run(scenario())
