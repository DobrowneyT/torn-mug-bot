import asyncio

import config
from server import db
from server import keys as keypool


def test_key_error_codes():
    assert 2 in keypool.KEY_ERROR_CODES        # incorrect key
    assert 10 in keypool.KEY_ERROR_CODES       # temporarily disabled
    assert 5 not in keypool.KEY_ERROR_CODES    # rate limit is NOT a key problem


def test_api_keys_crud_and_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "k.db"))

    async def scenario():
        await db.init_db()
        await db.seed_api_keys_from_env(["AAAA1111", "BBBB2222"])
        assert sorted(await db.get_enabled_api_keys()) == ["AAAA1111", "BBBB2222"]
        await keypool.refresh()
        assert keypool.active_count() == 2

        await db.api_key_set_owner("AAAA1111", 2250591, "MonChoon", 100)
        info = await db.api_key_info("AAAA1111")
        assert info["name"] == "MonChoon" and info["user_id"] == 2250591

        # a key-level error disables the key + queues exactly one status alert
        await keypool.mark_key_error("BBBB2222", 2, "Incorrect key")
        assert await db.get_enabled_api_keys() == ["AAAA1111"]
        assert keypool.active_count() == 1
        alerts = keypool.drain_alerts()
        assert len(alerts) == 1 and alerts[0]["code"] == 2
        assert keypool.drain_alerts() == []        # drained

        # removing by owner id works
        assert await keypool.remove_key(2250591) == 1
        assert await db.get_enabled_api_keys() == []
        await db.close_db()

    asyncio.run(scenario())


def test_validate_all_does_not_disable_on_transient(tmp_path, monkeypatch):
    """Regression for the mass key-nuke: a transient failure (rate-limit/Torn-side/network) must NOT
    disable a key, a key that now validates is RE-ENABLED, but a genuine key error still disables."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "kv.db"))

    async def scenario():
        await db.init_db()
        await db.seed_api_keys_from_env(["GOOD1111", "RLIM2222", "DEAD3333"])
        await db.api_key_mark_error("DEAD3333", "wrongly disabled earlier")  # was nuked before
        await keypool.refresh()

        async def fake_validate(client, key):
            if key == "RLIM2222":
                return {"ok": False, "code": 5, "reason": "Too many requests (code 5)"}  # transient
            return {"ok": True, "reason": "ok", "user_id": 1, "name": "Owner", "level": 1}
        monkeypatch.setattr(keypool, "validate_key", fake_validate)

        await keypool.validate_all(client=None)
        enabled = set(await db.get_enabled_api_keys())
        assert enabled == {"GOOD1111", "RLIM2222", "DEAD3333"}   # transient kept, dead one recovered

        # A real key-level error (code 2) DOES still disable.
        async def fake_bad(client, key):
            return {"ok": False, "code": 2, "reason": "Incorrect key (code 2)"}
        monkeypatch.setattr(keypool, "validate_key", fake_bad)
        await keypool.validate_all(client=None)
        assert await db.get_enabled_api_keys() == []

        # /keys enable brings them all back.
        assert await keypool.enable_keys() == 3
        assert len(await db.get_enabled_api_keys()) == 3
        await db.close_db()

    asyncio.run(scenario())


def _reset_pool():
    keypool._active = []
    keypool._cooldowns.clear()
    keypool._tokens.clear()          # budget state is per-key and persists across calls
    keypool._refilled_at.clear()
    keypool._global_pause_until = 0.0
    keypool._cursor = 0


def test_rotation_is_round_robin_not_clock_based():
    """Regression: next_key used to index by int(time.time()), so every call inside the same second
    got the SAME key — a burst of concurrent polls piled onto one key and burned its rate budget
    while the others idled. Consecutive calls must advance regardless of the clock."""
    _reset_pool()
    keypool._active = ["K1", "K2", "K3"]

    # 9 calls in the same instant must spread evenly across the 3 keys, in order.
    got = [keypool.next_key() for _ in range(9)]
    assert got == ["K1", "K2", "K3"] * 3

    # An immediate retry excludes the key that just failed and still moves on.
    _reset_pool()
    keypool._active = ["K1", "K2", "K3"]
    first = keypool.next_key()
    assert keypool.next_key(exclude={first}) != first
    _reset_pool()


def test_rotation_skips_cooled_down_keys_and_keeps_cycling(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_BACKOFF_SECONDS", 60)
    monkeypatch.setattr(config, "RATE_LIMIT_GLOBAL_PAUSE", False)
    _reset_pool()
    keypool._active = ["K1", "K2", "K3"]
    keypool.mark_rate_limited("K2")

    got = [keypool.next_key() for _ in range(6)]
    assert set(got) == {"K1", "K3"}          # the cooling key is never handed out
    assert got.count("K1") == 3 and got.count("K3") == 3   # ...and the rest still alternate
    _reset_pool()


def test_rate_limit_backs_off_just_that_key(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_BACKOFF_SECONDS", 60)
    monkeypatch.setattr(config, "RATE_LIMIT_GLOBAL_PAUSE", False)
    _reset_pool()
    keypool._active = ["K1", "K2"]

    assert keypool.next_key() in ("K1", "K2")
    keypool.mark_rate_limited("K1")
    assert {keypool.next_key() for _ in range(6)} == {"K2"}   # K1 skipped while cooling down
    keypool.mark_rate_limited("K2")
    assert keypool.next_key() is None                          # all cooling down -> calls pause
    _reset_pool()


def test_rate_limit_global_pause(monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_BACKOFF_SECONDS", 60)
    monkeypatch.setattr(config, "RATE_LIMIT_GLOBAL_PAUSE", True)
    _reset_pool()
    keypool._active = ["K1", "K2"]
    keypool.mark_rate_limited("K1")                            # one limit pauses everything
    assert keypool.next_key() is None
    _reset_pool()


def _fake_clock(monkeypatch, start=1_000_000.0):
    """Drive keys._now() by hand: budget behaviour is time-based and sleeping would be slow+flaky."""
    clock = {"t": start}
    monkeypatch.setattr(keypool, "_now", lambda: clock["t"])
    return clock


def test_budget_exhausts_a_key_before_torn_has_to_tell_us(monkeypatch):
    monkeypatch.setattr(config, "KEY_BUDGET_PER_MIN", 100)
    _fake_clock(monkeypatch)
    _reset_pool()
    keypool._active = ["K1"]

    got = [keypool.next_key() for _ in range(100)]
    assert got == ["K1"] * 100          # exactly the budget
    assert keypool.next_key() is None   # the 101st is refused by US, not by Torn
    _reset_pool()


def test_budget_refills_continuously_not_in_steps(monkeypatch):
    monkeypatch.setattr(config, "KEY_BUDGET_PER_MIN", 100)
    clock = _fake_clock(monkeypatch)
    _reset_pool()
    keypool._active = ["K1"]

    for _ in range(100):
        keypool.next_key()
    assert keypool.next_key() is None

    clock["t"] += 30                     # half a minute -> about half the budget back
    assert 45 <= keypool.budget_remaining("K1") <= 55
    assert keypool.next_key() == "K1"

    clock["t"] += 3600                   # long idle must not overfill
    assert keypool.budget_remaining("K1") == 100
    _reset_pool()


def test_budget_is_per_key(monkeypatch):
    monkeypatch.setattr(config, "KEY_BUDGET_PER_MIN", 10)
    _fake_clock(monkeypatch)
    _reset_pool()
    keypool._active = ["K1", "K2"]

    got = [keypool.next_key() for _ in range(20)]
    assert got.count("K1") == 10 and got.count("K2") == 10   # 2 keys => 2x the budget
    assert keypool.next_key() is None
    _reset_pool()


def test_budget_and_cooldown_are_independent(monkeypatch):
    """A rate-limited key with budget left is still skipped, and vice versa."""
    monkeypatch.setattr(config, "KEY_BUDGET_PER_MIN", 100)
    monkeypatch.setattr(config, "RATE_LIMIT_BACKOFF_SECONDS", 60)
    monkeypatch.setattr(config, "RATE_LIMIT_GLOBAL_PAUSE", False)
    clock = _fake_clock(monkeypatch)
    _reset_pool()
    keypool._active = ["K1", "K2"]

    keypool.mark_rate_limited("K1")                 # cooling down despite a full budget
    assert {keypool.next_key() for _ in range(5)} == {"K2"}

    clock["t"] += 61                                # cooldown elapsed -> K1 usable again
    assert "K1" in {keypool.next_key() for _ in range(5)}
    _reset_pool()


def test_a_rate_limit_zeroes_our_own_accounting(monkeypatch):
    """If Torn says the key is spent, believe it over our estimate."""
    monkeypatch.setattr(config, "KEY_BUDGET_PER_MIN", 100)
    monkeypatch.setattr(config, "RATE_LIMIT_BACKOFF_SECONDS", 0)
    _fake_clock(monkeypatch)
    _reset_pool()
    keypool._active = ["K1"]
    keypool.next_key()
    assert keypool.budget_remaining("K1") > 90

    keypool.mark_rate_limited("K1")
    assert keypool.budget_remaining("K1") == 0
    _reset_pool()


def test_budget_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "KEY_BUDGET_PER_MIN", 0)
    _fake_clock(monkeypatch)
    _reset_pool()
    keypool._active = ["K1"]
    assert all(keypool.next_key() == "K1" for _ in range(500))
    _reset_pool()
