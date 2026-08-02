import asyncio

import config
from server import keys as keypool
from server import torn


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


class _Client:
    """Returns queued responses in order (last one repeats); records the key used per call."""
    def __init__(self, responses):
        self._responses, self.calls, self.keys_used = list(responses), 0, []

    async def get(self, url, params=None, timeout=None, headers=None):
        self.keys_used.append((params or {}).get("key"))
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        if isinstance(r, Exception):
            raise r
        return r


def _pool(monkeypatch, keys):
    monkeypatch.setattr(keypool, "_active", list(keys))
    monkeypatch.setattr(keypool, "_cooldowns", {})
    monkeypatch.setattr(keypool, "_global_pause_until", 0.0)


def test_get_retries_with_different_key_on_key_error(monkeypatch):
    _pool(monkeypatch, ["k1", "k2", "k3"])
    monkeypatch.setattr(config, "API_MAX_KEY_RETRIES", 3)

    async def _noop(key, code, msg):  # don't touch the DB
        pass
    monkeypatch.setattr(keypool, "mark_key_error", _noop)

    client = _Client([_Resp({"error": {"code": 2, "error": "Incorrect key"}}),
                      _Resp({"itemmarket": {"listings": []}})])
    data = asyncio.run(torn._get(client, "market/1/itemmarket"))
    assert data == {"itemmarket": {"listings": []}}
    assert client.calls == 2                          # retried once
    assert len(set(client.keys_used)) == 2            # on a DIFFERENT key


def test_get_does_not_retry_on_torn_side_error(monkeypatch):
    _pool(monkeypatch, ["k1", "k2"])
    monkeypatch.setattr(config, "API_MAX_KEY_RETRIES", 3)
    client = _Client([_Resp({"error": {"code": 17, "error": "Backend error"}}),
                      _Resp({"itemmarket": {"listings": []}})])
    assert asyncio.run(torn._get(client, "market/1/itemmarket")) is None
    assert client.calls == 1                          # Torn-side: no retry-spam


def test_get_does_not_retry_on_http_5xx(monkeypatch):
    _pool(monkeypatch, ["k1", "k2"])
    monkeypatch.setattr(config, "API_MAX_KEY_RETRIES", 3)
    client = _Client([_Resp({}, status=504), _Resp({"itemmarket": {"listings": []}})])
    assert asyncio.run(torn._get(client, "market/1/itemmarket")) is None
    assert client.calls == 1


def test_get_sends_timestamp_in_seconds(monkeypatch):
    _pool(monkeypatch, ["k1"])
    captured = {}

    class _Cap(_Client):
        async def get(self, url, params=None, timeout=None, headers=None):
            captured.update(params or {})
            return _Resp({"itemmarket": {"listings": []}})

    asyncio.run(torn._get(_Cap([]), "market/1/itemmarket"))
    # seconds (~10 digits), NOT milliseconds (~13) — the fresh-cache trigger.
    assert 1_000_000_000 <= captured["timestamp"] < 10_000_000_000


def test_clothing_store_7star_plus():
    assert torn.is_clothing_store({"type_id": 5, "rating": 10}) is True
    assert torn.is_clothing_store({"type_id": 5, "rating": 7}) is True


def test_clothing_store_below_7star():
    assert torn.is_clothing_store({"type_id": 5, "rating": 6}) is False


def test_not_a_clothing_store_company():
    assert torn.is_clothing_store({"type_id": 1, "rating": 10}) is False


def test_missing_or_none():
    assert torn.is_clothing_store(None) is False
    assert torn.is_clothing_store({}) is False
