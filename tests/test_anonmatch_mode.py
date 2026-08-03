"""Anon-match posting modes, and the uid=0 sentinel.

An inferred seller is a weaker claim than a captured one, so the default is `silent` and a ping has
to be earned. The gate is CONFIDENCE rather than age on purpose: measured accuracy decays smoothly
(92.3% under a day, 69.3% at 90-365d, 58.3% beyond) with no cliff to cut at, so an age cutoff would
discard thousands of usable matches to avoid a minority of wrong ones.
"""

import asyncio

import config
from server import ah_scraper, db, detection


def _match(confidence):
    return {"uid": "u", "buyer_id": 500, "buyer_name": "B", "confidence": confidence,
            "age_days": 3, "won_at": 1_700_000_000, "price": 1_000_000}


def _run(scenario, tmp_path, monkeypatch, name="a.db"):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / name))

    async def wrapper():
        await db.init_db()
        try:
            await scenario()
        finally:
            await db.close_db()

    asyncio.run(wrapper())


# --------------------------------- the sentinel -----------------------------------------

def test_uid_zero_is_stored_as_no_uid():
    """Confirmed live: uid=0 covers 49,435 auctions across 302 DIFFERENT item ids — it is Torn's
    'this has no uid' marker for stackables, not an identifier. NULL is what it actually means."""
    row = {"id": 1, "seller": {"id": 1, "name": "s"}, "buyer": {"id": 2, "name": "b"},
           "timestamp": 1_700_000_000, "price": 1, "bids": 1,
           "item": {"uid": 0, "id": 206, "name": "Xanax", "type": "Drug"}}
    assert ah_scraper.normalize(row)["uid"] is None
    row["item"]["uid"] = "0"
    assert ah_scraper.normalize(row)["uid"] is None
    row["item"]["uid"] = 12604734202
    assert ah_scraper.normalize(row)["uid"] == "12604734202"


def test_the_auction_itself_is_still_kept_without_a_uid():
    """A stackable auction still records a real price and buyer — useful for the flip-margin work
    (#42). Only the ownership join is impossible, and that is what uid=None says."""
    row = {"id": 7, "seller": {"id": 1, "name": "s"}, "buyer": {"id": 2, "name": "b"},
           "timestamp": 1_700_000_000, "price": 999, "bids": 3,
           "item": {"uid": 0, "id": 206, "name": "Xanax", "type": "Drug"}}
    out = ah_scraper.normalize(row)
    assert out is not None and out["price"] == 999 and out["item_name"] == "Xanax"


# --------------------------------- modes ------------------------------------------------

def test_default_mode_is_silent(tmp_path, monkeypatch):
    """An inferred seller is weaker evidence than a captured one. A ping that is wrong often enough
    trains you to ignore the channel, which costs more than the alerts it would have carried."""
    assert config.ANONMATCH_MODE == "silent"

    async def scenario():
        d = await detection.anon_match_decision(_match(0.99))
        assert d["use"] is True and d["silent"] is True

    _run(scenario, tmp_path, monkeypatch)


def test_off_declines_to_infer_at_all(tmp_path, monkeypatch):
    """`off` must reproduce today's behaviour exactly — an anonymous uid stays a dead end."""
    async def scenario():
        await db.set_config("anonmatch_mode", "off")
        d = await detection.anon_match_decision(_match(0.99))
        assert d["use"] is False

    _run(scenario, tmp_path, monkeypatch, "b.db")


def test_ping_is_gated_on_confidence(tmp_path, monkeypatch):
    """`ping` does not mean "always ping" — it means "ping when the evidence is strong enough".
    A weak inference still posts, just without waking anyone."""
    async def scenario():
        await db.set_config("anonmatch_mode", "ping")
        await db.set_config("anonmatch_min_confidence", 0.88)

        strong = await detection.anon_match_decision(_match(0.923))
        assert strong["use"] is True and strong["silent"] is False

        weak = await detection.anon_match_decision(_match(0.693))
        assert weak["use"] is True and weak["silent"] is True, "a weak match must not be dropped"
        assert "0.69" in weak["reason"] and "0.88" in weak["reason"]

    _run(scenario, tmp_path, monkeypatch, "c.db")


def test_the_threshold_is_inclusive_at_the_boundary(tmp_path, monkeypatch):
    async def scenario():
        await db.set_config("anonmatch_mode", "ping")
        await db.set_config("anonmatch_min_confidence", 0.88)
        assert (await detection.anon_match_decision(_match(0.88)))["silent"] is False
        assert (await detection.anon_match_decision(_match(0.879)))["silent"] is True

    _run(scenario, tmp_path, monkeypatch, "d.db")


def test_no_match_is_not_an_alert(tmp_path, monkeypatch):
    async def scenario():
        assert (await detection.anon_match_decision(None))["use"] is False
        # a row with no confidence recorded must not be treated as certain
        await db.set_config("anonmatch_mode", "ping")
        d = await detection.anon_match_decision({"uid": "u", "buyer_id": 1})
        assert d["use"] is True and d["silent"] is True

    _run(scenario, tmp_path, monkeypatch, "e.db")


def test_mode_is_live_tunable_without_a_restart(tmp_path, monkeypatch):
    """Same contract as /delist and /hosplock: the config table wins over the env default."""
    async def scenario():
        assert (await detection.anon_match_decision(_match(0.95)))["silent"] is True
        await db.set_config("anonmatch_mode", "ping")
        assert (await detection.anon_match_decision(_match(0.95)))["silent"] is False
        await db.set_config("anonmatch_mode", "off")
        assert (await detection.anon_match_decision(_match(0.95)))["use"] is False

    _run(scenario, tmp_path, monkeypatch, "f.db")


def test_default_ping_bar_matches_the_measured_week_old_accuracy():
    """Out of the box a ping should mean "won at auction within the last week" — the 88-92% region
    of the calibration curve, rather than a number picked for feeling about right."""
    week_band = dict((a if a is not None else 10**9, c) for a, c in config.AH_CONFIDENCE_BANDS)
    assert config.ANONMATCH_MIN_CONFIDENCE == week_band[7]


def test_summary_counts_what_clears_the_bar(tmp_path, monkeypatch):
    """/anonmatch reports it, so a wrong count would misrepresent how loud `ping` is about to be."""
    async def scenario():
        await db.replace_anon_matches([
            {"uid": "a", "buyer_id": 1, "won_at": 1, "confidence": 0.923},
            {"uid": "b", "buyer_id": 2, "won_at": 1, "confidence": 0.880},
            {"uid": "c", "buyer_id": 3, "won_at": 1, "confidence": 0.583},
        ], {"a", "b", "c"})
        s = await db.anon_match_summary()
        assert s["n"] == 3 and s["high_confidence"] == 2

    _run(scenario, tmp_path, monkeypatch, "g.db")


def test_the_slash_command_is_actually_registered(tmp_path, monkeypatch):
    """A command defined but never registered is invisible in Discord and fails silently — there is
    no error, it simply does not appear. Worth one assertion: the tree is built by _register_commands,
    not by __init__, so "the function exists in the file" proves nothing."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "bot.db"))
    from bot.discord_bot import MugBot

    bot = MugBot()
    bot._register_commands()
    by_name = {c.name: c for c in bot.tree.get_commands()}
    assert "anonmatch" in by_name

    cmd = by_name["anonmatch"]
    params = {p.name: p for p in cmd.parameters}
    assert set(params) == {"mode", "min_confidence"}
    assert not any(p.required for p in params.values()), "every parameter should be optional"
    assert [c.value for c in params["mode"].choices] == ["silent", "ping", "off"]
