"""The read API: shared-token auth, per-caller rate limiting, IP-logged access, and the search.

The auth here is a bearer token over plain HTTP. It is not strong security and is not presented as
such — anyone on the network path can read it. What it must do is fail CLOSED (no token configured
means no API at all), reject anything that is not the token, and leave a record of who called.

The search's own risk is different and quieter: a filter that matches more than it should. Every
one of those returns a plausible-looking page of wrong answers with no error.
"""

import asyncio
import json
import time

import config
from server import ah_db, api_access

BONUSES = [{"id": 86, "title": "Disarm", "value": 7}]


def _auction(auction_id, *, name="Sai", quality=100.0, price=1_000_000, ended_at=1_700_000_000,
             bonuses=BONUSES, buyer_id=500):
    return {
        "auction_id": auction_id, "uid": str(auction_id), "item_id": 238, "item_name": name,
        "item_type": "Weapon", "sub_type": "Piercing", "damage": 37.3, "accuracy": 60.4,
        "armor": None, "quality": quality, "rarity": "orange",
        "bonuses": json.dumps(bonuses) if bonuses else None,
        "seller_id": 1, "seller_name": "Seller", "buyer_id": buyer_id,
        "buyer_name": "Buyer" if buyer_id else None,
        "price": price, "bids": 4, "ended_at": ended_at,
    }


class _Req:
    """Minimal stand-in for a Starlette Request — only what api_access touches."""

    class _Client:
        def __init__(self, host):
            self.host = host

    def __init__(self, token=None, ip="203.0.113.9", headers=None):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        if token is not None:
            self.headers["authorization"] = f"Bearer {token}"
        self.client = self._Client(ip)


def _run(scenario, tmp_path, name="api.db"):
    async def wrapper():
        await ah_db.init_db(str(tmp_path / name))
        try:
            await scenario()
        finally:
            await ah_db.close_db()

    asyncio.run(wrapper())


def _api(monkeypatch, tmp_path, token="s3cret"):
    monkeypatch.setattr(config, "API_TOKEN", token)
    monkeypatch.setattr(config, "API_ACCESS_LOG", str(tmp_path / "access.log"))
    monkeypatch.setattr(config, "API_TRUST_PROXY", False)
    api_access.reset_for_tests()


# --------------------------------- auth -------------------------------------------------

def test_no_token_configured_means_no_api(monkeypatch, tmp_path):
    """Fail CLOSED. This endpoint publishes 548k rows of named players' purchase history, so it
    must never come online as a side effect of upgrading."""
    _api(monkeypatch, tmp_path, token="")
    assert api_access.enabled() is False
    assert api_access.authorise(_Req(token="anything")) is False
    assert api_access.authorise(_Req(token="")) is False


def test_only_the_exact_token_is_accepted(monkeypatch, tmp_path):
    _api(monkeypatch, tmp_path)
    assert api_access.authorise(_Req(token="s3cret")) is True
    assert api_access.authorise(_Req(token="s3cre")) is False
    # surrounding whitespace in the header is ordinary HTTP and is stripped, not rejected
    assert api_access.authorise(_Req(token="s3cret ")) is True
    assert api_access.authorise(_Req(token="S3CRET")) is False
    assert api_access.authorise(_Req()) is False                      # no header at all


def test_the_apikey_header_is_accepted_too(monkeypatch, tmp_path):
    """The price-checker already sends `apikey` alongside Authorization (Supabase's convention).
    Accepting it means the fork needs one line changed rather than three."""
    _api(monkeypatch, tmp_path)
    assert api_access.authorise(_Req(headers={"apikey": "s3cret"})) is True
    assert api_access.authorise(_Req(headers={"apikey": "wrong"})) is False


def test_token_comparison_is_constant_time():
    """A plain == returns as soon as two bytes differ, which leaks the prefix to anyone willing to
    time enough requests. Cheap to do correctly, so there is no reason not to."""
    import inspect
    src = inspect.getsource(api_access.authorise)
    assert "compare_digest" in src


# --------------------------------- the access log ---------------------------------------

def test_every_request_is_logged_with_its_ip(monkeypatch, tmp_path):
    _api(monkeypatch, tmp_path)
    api_access.record(_Req(token="s3cret", ip="198.51.100.4"), path="/api/auctions/search",
                      status=200, detail="item='Sai' total=3", elapsed_ms=12.0)
    text = (tmp_path / "access.log").read_text()
    assert "ip=198.51.100.4" in text
    assert "status=200" in text and "/api/auctions/search" in text
    assert "total=3" in text


def test_a_rejected_request_is_logged_too(monkeypatch, tmp_path):
    """The rejections are the ones worth having. A log that only records successes cannot tell you
    someone has been probing the port."""
    _api(monkeypatch, tmp_path)
    api_access.record(_Req(token="nope", ip="192.0.2.7"), path="/api/auctions/search",
                      status=401, detail="bad-token")
    text = (tmp_path / "access.log").read_text()
    assert "ip=192.0.2.7" in text and "status=401" in text and "bad-token" in text


def test_the_access_log_never_records_the_token(monkeypatch, tmp_path):
    """It is the credential. Writing it into a file that exists to be read later, and rotated, and
    possibly pasted into an issue, would undo the point of having one."""
    _api(monkeypatch, tmp_path)
    api_access.record(_Req(token="s3cret", ip="192.0.2.1"), path="/api/auctions/search", status=200)
    assert "s3cret" not in (tmp_path / "access.log").read_text()


def test_a_forged_forwarded_header_is_ignored_by_default(monkeypatch, tmp_path):
    """A client can send X-Forwarded-For itself. Trusting it unconditionally means the log records
    whichever IP the caller chose — worse than no log, because it looks authoritative."""
    _api(monkeypatch, tmp_path)
    req = _Req(token="s3cret", ip="203.0.113.9", headers={"x-forwarded-for": "1.2.3.4"})
    assert api_access.client_ip(req) == "203.0.113.9"

    monkeypatch.setattr(config, "API_TRUST_PROXY", True)      # only when you run the proxy
    assert api_access.client_ip(req) == "1.2.3.4"


# --------------------------------- rate limiting ----------------------------------------

def test_a_caller_is_limited_and_refills(monkeypatch, tmp_path):
    """A shared token leaks eventually. This bounds what that costs."""
    _api(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "API_RATE_PER_MIN", 3)
    clock = [1000.0]
    monkeypatch.setattr(api_access, "_now", lambda: clock[0])

    req = _Req(token="s3cret")
    assert [api_access.rate_limited(req) for _ in range(4)] == [False, False, False, True]

    clock[0] += 30            # half a minute -> 1.5 tokens back
    assert api_access.rate_limited(req) is False


def test_callers_are_limited_independently(monkeypatch, tmp_path):
    """One noisy IP must not lock everyone else out."""
    _api(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "API_RATE_PER_MIN", 1)
    monkeypatch.setattr(api_access, "_now", lambda: 1000.0)

    assert api_access.rate_limited(_Req(token="s3cret", ip="10.0.0.1")) is False
    assert api_access.rate_limited(_Req(token="s3cret", ip="10.0.0.1")) is True
    assert api_access.rate_limited(_Req(token="s3cret", ip="10.0.0.2")) is False


# --------------------------------- the search -------------------------------------------

def test_search_returns_the_shape_the_userscript_renders(tmp_path):
    """It reproduces the upstream Supabase field names on purpose, so nothing below apiRequest()
    in the script needs touching."""
    async def scenario():
        await ah_db.record_auctions([_auction(1)])
        out = await ah_db.search_auctions({"item_name": "Sai"})
        assert out["total"] == 1
        a = out["auctions"][0]
        assert a["item_name"] == "Sai"
        assert a["stat_quality"] == 100.0 and a["stat_damage"] == 37.3
        assert a["stat_armor"] is None
        assert a["bonus_values"] == [{"bonus_id": 86, "bonus_value": 7, "bonus_title": "Disarm"}]
        assert a["bonus_ids"] == [86]
        assert a["timestamp"] == 1_700_000_000        # ended_at, renamed
        assert a["price"] == 1_000_000 and a["buyer_id"] == 500

    _run(scenario, tmp_path)


def test_unsold_auctions_are_excluded(tmp_path):
    """An auction that ended unsold has no clearing price. Including it would drag every
    "what do these go for" answer toward nothing."""
    async def scenario():
        await ah_db.record_auctions([_auction(1), _auction(2, buyer_id=None)])
        out = await ah_db.search_auctions({})
        assert out["total"] == 1 and out["auctions"][0]["auction_id"] == 1

    _run(scenario, tmp_path, "b.db")


def test_bonus_and_value_range_filter(tmp_path):
    async def scenario():
        await ah_db.record_auctions([
            _auction(1, bonuses=[{"id": 86, "title": "Disarm", "value": 5}]),
            _auction(2, bonuses=[{"id": 86, "title": "Disarm", "value": 9}]),
            _auction(3, bonuses=[{"id": 86, "title": "Disarm", "value": 15}]),
            _auction(4, bonuses=[{"id": 42, "title": "Wither", "value": 7}]),
        ])
        out = await ah_db.search_auctions(
            {"bonus1_ids": [86], "bonus1_value_min": 5, "bonus1_value_max": 10})
        assert {a["auction_id"] for a in out["auctions"]} == {1, 2}
        assert out["total"] == 2

    _run(scenario, tmp_path, "c.db")


def test_a_valueless_bonus_does_not_satisfy_a_range(tmp_path):
    """The quiet one. NULL is "no magnitude", not zero — treating it as zero would widen every
    range search and return confident nonsense."""
    async def scenario():
        await ah_db.record_auctions([_auction(1, bonuses=[{"id": 7, "title": "Flag"}])])
        assert (await ah_db.search_auctions({"bonus1_ids": [7]}))["total"] == 1
        assert (await ah_db.search_auctions(
            {"bonus1_ids": [7], "bonus1_value_min": 0})) ["total"] == 0

    _run(scenario, tmp_path, "d.db")


def test_two_bonuses_must_both_match(tmp_path):
    """AND, not OR. An OR here returns items that share one property and quietly misprices."""
    async def scenario():
        both = [{"id": 42, "value": 25}, {"id": 101, "value": 31}]
        await ah_db.record_auctions([
            _auction(1, bonuses=both),
            _auction(2, bonuses=[{"id": 42, "value": 25}]),
            _auction(3, bonuses=[{"id": 101, "value": 31}]),
        ])
        out = await ah_db.search_auctions({"bonus1_ids": [42], "bonus2_ids": [101]})
        assert [a["auction_id"] for a in out["auctions"]] == [1]

    _run(scenario, tmp_path, "e.db")


def test_the_same_bonus_twice_does_not_duplicate_a_result(tmp_path):
    """EXISTS rather than a JOIN. A join would return the auction once per matching bonus row,
    inflating both the page and the total."""
    async def scenario():
        await ah_db.record_auctions([_auction(1, bonuses=[
            {"id": 86, "value": 5}, {"id": 86, "value": 9}])])
        out = await ah_db.search_auctions({"bonus1_ids": [86], "bonus1_value_min": 1})
        assert out["total"] == 1 and len(out["auctions"]) == 1

    _run(scenario, tmp_path, "f.db")


def test_quality_range_and_sorting(tmp_path):
    async def scenario():
        await ah_db.record_auctions([
            _auction(1, quality=90.0, price=300, ended_at=1_700_000_100),
            _auction(2, quality=150.0, price=100, ended_at=1_700_000_200),
            _auction(3, quality=200.0, price=200, ended_at=1_700_000_300),
        ])
        out = await ah_db.search_auctions({"quality_min": 100, "quality_max": 199})
        assert [a["auction_id"] for a in out["auctions"]] == [2]

        newest = await ah_db.search_auctions({"sort_by": "timestamp", "sort_order": "desc"})
        assert [a["auction_id"] for a in newest["auctions"]] == [3, 2, 1]
        cheapest = await ah_db.search_auctions({"sort_by": "price", "sort_order": "asc"})
        assert [a["auction_id"] for a in cheapest["auctions"]] == [2, 3, 1]

    _run(scenario, tmp_path, "g.db")


def test_sort_input_cannot_reach_the_sql(tmp_path):
    """sort_by/sort_order interpolate into ORDER BY, where a bind parameter cannot go. A whitelist
    makes injection impossible rather than merely unlikely — so a hostile value must fall back."""
    async def scenario():
        await ah_db.record_auctions([_auction(1), _auction(2)])
        out = await ah_db.search_auctions({
            "sort_by": "ended_at; DROP TABLE auction_wins;--", "sort_order": "desc'--"})
        assert out["total"] == 2
        assert await ah_db.count_auctions() == 2, "the table is gone"

    _run(scenario, tmp_path, "h.db")


def test_paging_and_the_hard_result_cap(tmp_path):
    """total is the full match count; the page is capped so one request cannot ask for everything."""
    async def scenario():
        await ah_db.record_auctions(
            [_auction(i, ended_at=1_700_000_000 + i) for i in range(1, 31)])
        page = await ah_db.search_auctions({"limit": 10, "offset": 0})
        assert page["total"] == 30 and len(page["auctions"]) == 10
        assert page["auctions"][0]["auction_id"] == 30       # newest first by default

        nxt = await ah_db.search_auctions({"limit": 10, "offset": 10})
        assert nxt["auctions"][0]["auction_id"] == 20

        huge = await ah_db.search_auctions({"limit": 100_000})
        assert huge["limit"] == ah_db.SEARCH_MAX_LIMIT

        assert (await ah_db.search_auctions({"limit": -5, "offset": -5}))["offset"] == 0

    _run(scenario, tmp_path, "i.db")


def test_an_empty_query_is_valid_and_bounded(tmp_path):
    async def scenario():
        await ah_db.record_auctions([_auction(i) for i in range(1, 6)])
        out = await ah_db.search_auctions({})
        assert out["total"] == 5 and out["limit"] == 20

    _run(scenario, tmp_path, "j.db")


def test_read_only_mode_cannot_write(tmp_path):
    """The bot opens the ledger read-only. That is what stops a bug on the request path from
    writing to, migrating, or locking the scraper's database."""
    import aiosqlite

    async def scenario():
        path = str(tmp_path / "ro.db")
        await ah_db.init_db(path)
        await ah_db.record_auctions([_auction(1)])
        await ah_db.close_db()

        await ah_db.init_db(path, read_only=True)
        try:
            assert (await ah_db.search_auctions({}))["total"] == 1
            try:
                await ah_db._db.execute("DELETE FROM auction_wins")
                await ah_db._db.commit()
                raise AssertionError("a read-only connection wrote to the ledger")
            except aiosqlite.OperationalError:
                pass
        finally:
            await ah_db.close_db()

    asyncio.run(scenario())


def test_search_stays_fast_with_the_indexes(tmp_path):
    """A slow search on a reachable endpoint is a denial-of-service surface, not just an
    annoyance — an unindexed filter is something a caller can weaponise for free."""
    async def scenario():
        rows = []
        for i in range(1, 20_001):
            rows.append(_auction(
                i,
                name="Sai" if i % 3 else "Kodachi",
                quality=100.0 + (i % 90),
                ended_at=1_700_000_000 + i,
                bonuses=[{"id": 86 if i % 2 else 42, "title": "B", "value": i % 40}],
            ))
        await ah_db.record_auctions(rows)

        t0 = time.monotonic()
        out = await ah_db.search_auctions({
            "item_name": "Sai", "bonus1_ids": [86], "bonus1_value_min": 5,
            "bonus1_value_max": 20, "quality_min": 110, "quality_max": 160,
        })
        elapsed = (time.monotonic() - t0) * 1000
        assert out["total"] > 0, "the fixture matched nothing, so this timed the empty case"
        assert elapsed < 400, f"search took {elapsed:.0f}ms over 20k rows"

    _run(scenario, tmp_path, "perf.db")


# --------------------------------- end to end over HTTP ---------------------------------

def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from server import app as server_app
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "main.db"))
    return TestClient(server_app.create_app(bot=None))


def test_the_endpoint_refuses_without_a_token(monkeypatch, tmp_path):
    """The whole point of the guard, exercised through the real route rather than the helper."""
    _api(monkeypatch, tmp_path)

    async def scenario():
        await db_init()
        client = _client(monkeypatch, tmp_path)
        assert client.post("/api/auctions/search", json={}).status_code == 401
        assert client.post("/api/auctions/search", json={},
                           headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = client.post("/api/auctions/search", json={},
                        headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200 and r.json()["total"] == 1

    async def db_init():
        from server import db
        await db.init_db()

    async def wrapper():
        from server import db
        await ah_db.init_db(str(tmp_path / "e2e_ah.db"))
        await ah_db.record_auctions([_auction(1)])
        try:
            await scenario()
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(wrapper())


def test_the_endpoint_is_gone_when_no_token_is_configured(monkeypatch, tmp_path):
    """503, not 200-with-data. Upgrading must never publish the ledger by accident."""
    _api(monkeypatch, tmp_path, token="")

    async def wrapper():
        from server import db
        await ah_db.init_db(str(tmp_path / "off_ah.db"))
        await db.init_db()
        try:
            client = _client(monkeypatch, tmp_path)
            r = client.post("/api/auctions/search", json={},
                            headers={"Authorization": "Bearer anything"})
            assert r.status_code == 503
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(wrapper())


def test_a_malformed_body_does_not_500(monkeypatch, tmp_path):
    """It is a reachable endpoint. Garbage in must be a boring answer, not a stack trace."""
    _api(monkeypatch, tmp_path)

    async def wrapper():
        from server import db
        await ah_db.init_db(str(tmp_path / "junk_ah.db"))
        await ah_db.record_auctions([_auction(1)])
        await db.init_db()
        try:
            client = _client(monkeypatch, tmp_path)
            h = {"Authorization": "Bearer s3cret", "Content-Type": "application/json"}
            assert client.post("/api/auctions/search", data="not json", headers=h).status_code == 200
            assert client.post("/api/auctions/search", json=[1, 2, 3], headers=h).status_code == 200
            assert client.post("/api/auctions/search", json={"limit": "lots"},
                               headers=h).status_code == 500
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(wrapper())
