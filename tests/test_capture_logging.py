"""What the item-matcher actually sent, and what it did to the unmatched queue.

uvicorn logs `POST /gear 200 OK`, which says a capture happened and nothing else — not which item,
not which uid, and crucially not whether it resolved a listing the poller was waiting on. That last
one is the whole point: the same 200 covers "exactly what the queue existed for" and "a uid we have
never heard of", and telling those apart is the difference between a working pipeline and a silent
mismatch between what the userscript sees on the page and what the poller sees on the market.

The disposition has to be computed BEFORE the write, because the write is what destroys the
evidence: storing a capture deletes the unmatched row.
"""

import asyncio
import logging

import config
from server import app as appmod
from server import db


def _run(scenario, tmp_path, monkeypatch, name="c"):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / f"{name}.db"))

    async def wrapper():
        await db.init_db()
        try:
            await scenario()
        finally:
            await db.close_db()

    asyncio.run(wrapper())


def _gear(uid="777", *, item_id=238, name="Sai", price=153_900_001, seller=2250591):
    return {"listingID": f"L{uid}", "item_id": item_id, "item_name": name, "price": price,
            "uid": uid, "damage_armor": 37.31, "accuracy": 60.48,
            "lister": seller, "seller_name": "MonChoon"}


# --------------------------------- what was sent ----------------------------------------

def test_the_capture_line_carries_what_the_matcher_saw(tmp_path, monkeypatch, caplog):
    """Item, uid, price and the stats off the tile — the fields you would need to reproduce the
    capture by hand, which is what debugging a mismatch actually requires."""
    async def scenario():
        with caplog.at_level(logging.INFO, logger="mug.server"):
            await appmod._store_gear(_gear(), anonymous=False)
        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "Sai" in line
        assert "uid=777" in line
        assert "$153,900,001" in line
        assert "dmg=37.31" in line and "acc=60.48" in line
        assert "MonChoon[2250591]" in line

    _run(scenario, tmp_path, monkeypatch, "sent")


def test_an_anonymous_capture_says_so_rather_than_showing_a_blank_seller(tmp_path, monkeypatch,
                                                                        caplog):
    async def scenario():
        with caplog.at_level(logging.INFO, logger="mug.server"):
            await appmod._store_gear(
                {"listingID": "L1", "itemName": "Kodachi", "item_id": 224, "price": 550_000_000,
                 "armouryID": "555", "stats": {"damage": 41.2, "accuracy": 58.9}}, anonymous=True)
        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "ANONYMOUS" in line and "Kodachi" in line and "uid=555" in line

    _run(scenario, tmp_path, monkeypatch, "anon")


# --------------------------------- what it did ------------------------------------------

async def _disposition(caplog):
    for r in caplog.records:
        msg = r.getMessage()
        if msg.startswith("capture "):
            return msg.split()[1]
    return None


def test_a_capture_that_clears_a_queued_listing_says_resolved(tmp_path, monkeypatch, caplog):
    async def scenario():
        await db.upsert_unmatched_gear(uid="777", item_id=238, item_name="Sai",
                                       price=153_900_001, stat=37.31, accuracy=60.48)
        with caplog.at_level(logging.INFO, logger="mug.server"):
            await appmod._store_gear(_gear(), anonymous=False)
        assert await _disposition(caplog) == "resolved"

    _run(scenario, tmp_path, monkeypatch, "resolved")


def test_re_submitting_the_same_listing_says_refresh(tmp_path, monkeypatch, caplog):
    """Not a discrepancy — the human re-clicked a tile. It must not read like one."""
    async def scenario():
        await db.upsert_tracked_gear(uid="777", item_id=238, item_name="Sai", price=1,
                                     seller_id=2250591, attribution="matcher")
        with caplog.at_level(logging.INFO, logger="mug.server"):
            await appmod._store_gear(_gear(), anonymous=False)
        assert await _disposition(caplog) == "refresh"

    _run(scenario, tmp_path, monkeypatch, "refresh")


def test_a_capture_over_an_ah_inference_is_called_out(tmp_path, monkeypatch, caplog):
    """The strongest case there is: a human just replaced a ~77% guess with an observation."""
    async def scenario():
        await db.upsert_tracked_gear(uid="777", item_id=238, item_name="Sai", price=1,
                                     seller_id=4242, attribution="auction")
        with caplog.at_level(logging.INFO, logger="mug.server"):
            await appmod._store_gear(_gear(), anonymous=False)
        assert await _disposition(caplog) == "replaces-inference"

    _run(scenario, tmp_path, monkeypatch, "supersede")


def test_the_uid_changing_hands_is_a_warning(tmp_path, monkeypatch, caplog):
    """Tracked as one player, captured as another. Whatever the cause, an alert was about to name
    somebody who no longer owns it."""
    async def scenario():
        await db.upsert_tracked_gear(uid="777", item_id=238, item_name="Sai", price=1,
                                     seller_id=111, attribution="matcher")
        with caplog.at_level(logging.INFO, logger="mug.server"):
            await appmod._store_gear(_gear(), anonymous=False)
        assert await _disposition(caplog) == "seller-changed"
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    _run(scenario, tmp_path, monkeypatch, "changed")


# --------------------------------- the noise floor --------------------------------------

def test_an_unwatched_item_is_not_reported_as_a_discrepancy(tmp_path, monkeypatch, caplog):
    """The matcher runs on whatever page a human opens; the poller only looks at the watchlist. A
    capture for an unwatched item is the NORMAL case, and warning about it would bury the real
    discrepancies under a constant stream of warnings about working correctly."""
    async def scenario():
        with caplog.at_level(logging.INFO, logger="mug.server"):
            await appmod._store_gear(_gear(item_id=999_999), anonymous=False)
        assert await _disposition(caplog) == "not-watched"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    _run(scenario, tmp_path, monkeypatch, "unwatched")


def test_a_watched_item_the_poller_never_queued_IS_a_discrepancy(tmp_path, monkeypatch, caplog):
    """The case worth catching: we are polling this item, so the poller should have seen this
    listing and queued it — and it did not."""
    async def scenario():
        await db.watchlist_add(238, "Sai")
        with caplog.at_level(logging.INFO, logger="mug.server"):
            await appmod._store_gear(_gear(), anonymous=False)
        assert await _disposition(caplog) == "not-queued"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "the discrepancy was logged at INFO, where nobody will find it"

    _run(scenario, tmp_path, monkeypatch, "notqueued")


def test_the_queue_length_is_logged_only_when_it_changes(tmp_path, monkeypatch, caplog):
    """The matcher polls /cache/uncached every 30s. Logging each hit is noise uvicorn already
    makes; logging the CHANGE is the thing worth watching, because a count that never falls means
    captures are not clearing the queue."""
    from fastapi.testclient import TestClient

    async def scenario():
        app = appmod.create_app(None)
        with caplog.at_level(logging.INFO, logger="mug.server"):
            with TestClient(app) as client:
                client.get("/cache/uncached")
                client.get("/cache/uncached")          # unchanged -> silent
                lines = [r for r in caplog.records if "unmatched queue" in
                         (r.getMessage())]
                assert len(lines) == 1

                await db.upsert_unmatched_gear(uid="1", item_id=238, item_name="Sai", price=1,
                                               stat=1.0, accuracy=1.0)
                client.get("/cache/uncached")          # changed -> logged
                lines = [r for r in caplog.records if "unmatched queue" in
                         (r.getMessage())]
                assert len(lines) == 2

    _run(scenario, tmp_path, monkeypatch, "queue")
