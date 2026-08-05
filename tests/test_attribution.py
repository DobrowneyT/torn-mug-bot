"""The uid -> owner rules, exercised WITHOUT a database or a poll.

That is the point of the extraction. These rules were previously reachable only by driving a full
`reconcile_gear` against two databases (see tests/test_anon_match_alert.py), which is why the one
real bug in #12 — a tracked uid never re-consulting the item-matcher — survived review: nothing
could state the rule on its own, so nothing noticed it was missing.

Every test here monkeypatches the two DB reads. If any of them starts needing a real connection,
the seam has leaked.
"""

import asyncio

import pytest

from server import attribution, db
from server.attribution import AUCTION, MATCHER, Attribution

CAPTURE = {"seller_id": 111, "item_name": "Sai"}
WIN = {"uid": "u1", "buyer_id": 222, "buyer_name": "Winner", "confidence": 0.95}


@pytest.fixture
def no_ah(monkeypatch):
    """The auction house, wired to explode. Used to assert it is never consulted."""
    async def boom(uid):
        raise AssertionError("the AH was consulted when a direct capture was available")
    monkeypatch.setattr(db, "get_anon_match", boom)


@pytest.fixture
def ah(monkeypatch):
    """The auction house with one win in it, and ping mode on so confidence decides silence."""
    async def get_anon_match(uid):
        return WIN if uid == "u1" else None

    async def get_config(key, default=None):
        return {"anonmatch_mode": "ping", "anonmatch_min_confidence": 0.88}.get(key, default)

    monkeypatch.setattr(db, "get_anon_match", get_anon_match)
    monkeypatch.setattr(db, "get_config", get_config)


# --------------------------------- precedence -------------------------------------------

def test_a_direct_capture_wins_and_does_not_even_ask_the_auction_house(no_ah):
    """Precedence is not a tie-break applied afterwards — the weaker source is never queried.
    Correctness first, but it also keeps a per-uid DB read off the poll's hot path."""
    attrib = asyncio.run(attribution.resolve("u1", {"u1": CAPTURE}))
    assert attrib.seller_id == 111 and attrib.source == MATCHER
    assert attrib.confidence == 1.0 and attrib.silent is False


def test_an_auction_win_is_used_when_nothing_direct_exists(ah):
    attrib = asyncio.run(attribution.resolve("u1", {}, item_label="Sai", anonymous=True))
    assert attrib.seller_id == 222 and attrib.source == AUCTION
    assert attrib.confidence == 0.95 and attrib.item_name == "Sai"


def test_a_uid_no_source_can_speak_for_is_unmatched(ah):
    """Deliberately no price fallback. Matching on price alone pins one player's weapon onto
    another who listed at the same round number — the $300M Swiss Army Knife mis-match."""
    attrib = asyncio.run(attribution.resolve("unknown-uid", {}, anonymous=True))
    assert attrib.matched is False and attrib.seller_id is None and attrib.source is None


def test_a_weak_auction_win_is_used_but_silenced(monkeypatch):
    """Below the ping threshold the match is still worth posting — 69% at a year out is not
    nothing — it just must not ping as if it were observed."""
    async def get_anon_match(uid):
        return {**WIN, "confidence": 0.60}

    async def get_config(key, default=None):
        return {"anonmatch_mode": "ping", "anonmatch_min_confidence": 0.88}.get(key, default)

    monkeypatch.setattr(db, "get_anon_match", get_anon_match)
    monkeypatch.setattr(db, "get_config", get_config)

    attrib = asyncio.run(attribution.resolve("u1", {}, anonymous=True))
    assert attrib.matched is True and attrib.silent is True


# --------------------------------- upgrade ----------------------------------------------

def test_a_capture_replaces_an_earlier_inference():
    """THE bug this module exists for. `reconcile_gear`'s tracked branch returns before the
    resolution chain runs, so without this the first answer wins for the life of the listing —
    and it fails silently: the alert still fires, on time, naming the wrong player."""
    inferred = Attribution(seller_id=222, source=AUCTION)
    upgraded = attribution.upgrade(inferred, "u1", {"u1": CAPTURE})
    assert upgraded.seller_id == 111 and upgraded.source == MATCHER


def test_an_existing_capture_is_left_alone():
    already = Attribution(seller_id=111, source=MATCHER, item_name="Sai")
    assert attribution.upgrade(already, "u1", {"u1": CAPTURE}) is already


def test_nothing_new_means_nothing_changes():
    inferred = Attribution(seller_id=222, source=AUCTION)
    assert attribution.upgrade(inferred, "u1", {}) is inferred


def test_an_unattributed_tracked_uid_can_still_be_upgraded():
    """A uid tracked with no seller at all (attributed, then unlinked) is the weakest state there
    is — a capture must be able to fill it in rather than being refused as "not stronger"."""
    upgraded = attribution.upgrade(Attribution(), "u1", {"u1": CAPTURE})
    assert upgraded.seller_id == 111 and upgraded.source == MATCHER


def test_precedence_is_stated_once_rather_than_per_rule():
    """The property that makes a fourth source cheap: `beats` is the only place ordering lives, so
    a new resolver inherits both rules without either being restated."""
    assert Attribution(source=MATCHER).beats(AUCTION) is True
    assert Attribution(source=AUCTION).beats(MATCHER) is False
    assert Attribution(source=AUCTION).beats(AUCTION) is False, "equal rank must not replace"
    assert Attribution(source=AUCTION).beats(None) is True
    assert Attribution(source=None).beats(None) is False
    assert Attribution(source="bazaar-scraper").beats(AUCTION) is False, \
        "an unregistered source must not outrank a known one"


# --------------------------------- the alert path ---------------------------------------

def test_the_alert_path_does_not_consult_the_ah_for_an_observed_seller(no_ah):
    """An item-matcher capture is not probabilistic, so there is nothing to be silent about."""
    match, silent = asyncio.run(attribution.silence_for(MATCHER, "u1"))
    assert match is None and silent is False


def test_the_alert_path_re_reads_rather_than_trusting_the_poll(ah, monkeypatch):
    """Attribution happens during a poll; the sale fires whenever the listing vanishes, which may
    be hours later. Re-deriving means the mode in force is the one at ALERT time — an operator who
    just ran `/anonmatch silent` expects the next alert to obey it, not the one after that."""
    match, silent = asyncio.run(attribution.silence_for(AUCTION, "u1"))
    assert match == WIN and silent is False          # 0.95 >= 0.88, so it pings

    async def get_config(key, default=None):
        return {"anonmatch_mode": "silent"}.get(key, default)
    monkeypatch.setattr(db, "get_config", get_config)

    _, silent_now = asyncio.run(attribution.silence_for(AUCTION, "u1"))
    assert silent_now is True, "the mode change did not take effect until a restart"


def test_a_missing_uid_is_not_an_ah_lookup(no_ah):
    assert asyncio.run(attribution.silence_for(AUCTION, None)) == (None, False)


def test_a_uid_the_matcher_has_not_seen_is_not_guessed_at(no_ah):
    """The auction house is not consulted at all — not weighed and rejected, not consulted. A uid
    awaiting capture is a queue entry for a human, and an inference would both pre-empt a certain
    answer and (via remove_unmatched_gear) delete the queue entry that produces it."""
    attrib = asyncio.run(attribution.resolve("u1", {}, anonymous=False))
    assert attrib.matched is False
    assert "item-matcher" in attrib.reason


def test_a_direct_capture_still_wins_for_a_uid_the_matcher_called_anonymous(no_ah):
    """The gate opens the AH path; it does not reorder precedence above it."""
    attrib = asyncio.run(attribution.resolve("u1", {"u1": CAPTURE}, anonymous=True))
    assert attrib.source == MATCHER and attrib.seller_id == 111
