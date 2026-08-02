"""Seller enrichment runs its sources concurrently and degrades rather than failing.

Everything here sits between "we know it sold" and the alert reaching Discord, so each extra serial
round trip is time spent not attacking. Concurrency is asserted by recording call start/end times
against a fake clock rather than by measuring wall time, which would be flaky in CI.
"""

import asyncio

import config
from server import bsp, db, enrich, torn


class _Recorder:
    """Stubs the three enrichment sources, recording when each was entered and left."""

    def __init__(self, delay=0.05, fail=()):
        self.delay = delay
        self.fail = set(fail)
        self.spans = {}

    def _make(self, name, result):
        async def call(*args, **kwargs):
            start = asyncio.get_event_loop().time()
            await asyncio.sleep(self.delay)
            self.spans[name] = (start, asyncio.get_event_loop().time())
            if name in self.fail:
                raise RuntimeError(f"{name} exploded")
            return result

        return call

    def install(self, monkeypatch):
        monkeypatch.setattr(torn, "get_profile", self._make("profile", {
            "status": {"state": "Okay", "until": None, "description": "", "details": ""},
            "last_action": {"timestamp": 1000, "status": "Online"},
            "faction_id": 42,
        }))
        monkeypatch.setattr(torn, "get_job", self._make("job", {"type_id": 5, "rating": 7, "name": "Threads"}))
        monkeypatch.setattr(bsp, "get_tbs", self._make("tbs", {"total": 12345, "str": 1, "spd": 2, "def": 3, "dex": 4}))

    def overlapped(self):
        """True if every span overlaps every other — i.e. they really ran together."""
        spans = list(self.spans.values())
        return all(a[0] < b[1] and b[0] < a[1] for a in spans for b in spans)


def test_sources_are_fetched_concurrently(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "e.db"))
    rec = _Recorder()
    rec.install(monkeypatch)

    async def scenario():
        await db.init_db()
        try:
            await db.upsert_seller_seen(user_id=7, name="Target")
            seller = await enrich.enrich_seller(7, client=None, force=True)
            assert set(rec.spans) == {"profile", "job", "tbs"}
            assert rec.overlapped(), f"sources ran in series: {rec.spans}"
            # ...and every field still lands
            assert seller["faction_id"] == 42
            assert seller["in_clothing_store"] == 1      # job type 5, rating 7
            assert seller["bsp_total"] == 12345
            assert seller["online_status"] == "Online"
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_a_failing_source_does_not_lose_the_others(tmp_path, monkeypatch):
    """BSP is a third party. If it errors the alert must still post, with TBS simply unknown."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "e2.db"))
    rec = _Recorder(fail=("tbs",))
    rec.install(monkeypatch)

    async def scenario():
        await db.init_db()
        try:
            await db.upsert_seller_seen(user_id=8, name="Target")
            seller = await enrich.enrich_seller(8, client=None, force=True)
            assert seller["bsp_total"] is None          # unknown, not fatal
            assert seller["faction_id"] == 42           # the others still landed
            assert seller["in_clothing_store"] == 1
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_all_sources_failing_still_returns_a_seller(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "e3.db"))
    rec = _Recorder(fail=("profile", "job", "tbs"))
    rec.install(monkeypatch)

    async def scenario():
        await db.init_db()
        try:
            await db.upsert_seller_seen(user_id=9, name="Target")
            seller = await enrich.enrich_seller(9, client=None, force=True)
            assert seller is not None and seller["name"] == "Target"
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_only_due_sources_are_fetched(tmp_path, monkeypatch):
    """The TTL gating must survive the rewrite: a fresh clothing check shouldn't refetch job/TBS."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "e4.db"))
    rec = _Recorder()
    rec.install(monkeypatch)

    async def scenario():
        import time as _t
        await db.init_db()
        try:
            await db.upsert_seller_seen(user_id=10, name="Target")
            # clothing fresh (24h TTL), status stale (120s TTL) -> profile only
            await db.update_seller_enrichment(10, {"clothing_checked_at": int(_t.time())})
            await enrich.enrich_seller(10, client=None)
            assert set(rec.spans) == {"profile"}
        finally:
            await db.close_db()

    asyncio.run(scenario())


def test_bsp_timeout_is_short_and_configurable():
    """TBS is display-only; it must never be able to hold an alert for Torn's 15s budget."""
    assert config.BSP_TIMEOUT_SECONDS <= 5
