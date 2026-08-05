"""Can we tell whether the anon-match inference is any good?

Before this, no. `sales` did not record how the seller was known and `tracked_gear` is deleted the
instant a listing vanishes — so the attribution was destroyed at the exact moment the alert fired.
Four weeks of history, 86% of it unattributable.

The subtle risk here is not a wrong number, it is a FLATTERING one. Two ways that happens, both
tested below: counting the model's own output as ground truth, and counting unlabelled cases as
successes.
"""

import asyncio
import time

import config
from server import ah_scraper, db

DAY = 86400


def _run(scenario, tmp_path, monkeypatch, name="v"):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / f"{name}.db"))

    async def wrapper():
        await db.init_db()
        try:
            await scenario()
        finally:
            await db.close_db()

    asyncio.run(wrapper())


async def _sale(uid, seller_id, *, at, attribution=None, confidence=None):
    return await db.record_sale(
        listing_id=f"uid:{uid}", item_id=399, item_name="M", seller_id=seller_id,
        qty_sold=1, unit_price=1_000_000, sold_at=at,
        attribution=attribution, confidence=confidence)


# --------------------------------- recording --------------------------------------------

def test_a_sale_records_how_the_seller_was_known(tmp_path, monkeypatch):
    """The whole foundation. Without it the alert path cannot be judged on its hit rate, and the
    decision to keep or delete the scoring cannot be made either way."""
    async def scenario():
        now = int(time.time())
        await _sale("u1", 111, at=now, attribution="auction", confidence=0.88)
        await _sale("u2", 222, at=now, attribution="matcher")

        rows = {a["uid"]: a for a in await db.alerts_by_attribution()}
        assert rows["u1"]["attribution"] == "auction" and rows["u1"]["confidence"] == 0.88
        assert rows["u2"]["attribution"] == "matcher"
        assert rows["u2"]["confidence"] is None, "a direct capture is not probabilistic"

    _run(scenario, tmp_path, monkeypatch, "record")


# --------------------------------- the honest denominator -------------------------------

def test_a_later_capture_naming_someone_else_is_a_contradiction(tmp_path, monkeypatch):
    async def scenario():
        now = int(time.time())
        await _sale("u1", 111, at=now - 2 * DAY, attribution="auction", confidence=0.88)
        await db.upsert_listing(listing_id="L1", item_id=399, item_name="M", seller_id=999,
                                price=1, available=1, anonymous=False, is_gear=True,
                                armoury_uid="u1")

        row = (await db.alerts_by_attribution())[0]
        assert row["verdict"] == "contradicted"

    _run(scenario, tmp_path, monkeypatch, "contra")


def test_a_later_capture_naming_the_same_player_confirms_it(tmp_path, monkeypatch):
    async def scenario():
        now = int(time.time())
        await _sale("u1", 111, at=now - 2 * DAY, attribution="auction", confidence=0.88)
        await db.upsert_listing(listing_id="L1", item_id=399, item_name="M", seller_id=111,
                                price=1, available=1, anonymous=False, is_gear=True,
                                armoury_uid="u1")

        assert (await db.alerts_by_attribution())[0]["verdict"] == "confirmed"

    _run(scenario, tmp_path, monkeypatch, "conf")


def test_an_unlabelled_alert_is_unknown_and_not_a_success(tmp_path, monkeypatch):
    """The flattering failure. An anonymous seller who keeps listing anonymously never produces a
    capture, so most alerts have NO label — and if `unknown` were folded in with `confirmed` the
    report would show near-perfect accuracy built almost entirely from cases it never checked."""
    async def scenario():
        await _sale("u1", 111, at=int(time.time()), attribution="auction", confidence=0.88)
        assert (await db.alerts_by_attribution())[0]["verdict"] == "unknown"

    _run(scenario, tmp_path, monkeypatch, "unknown")


def test_an_earlier_capture_is_not_evidence_about_a_later_sale(tmp_path, monkeypatch):
    """Ownership moves. A capture from BEFORE the sale says who held it then, which is not a claim
    about who held it at the moment it sold — and counting it would mark correct inferences wrong."""
    async def scenario():
        now = int(time.time())
        await db.upsert_listing(listing_id="L1", item_id=399, item_name="M", seller_id=999,
                                price=1, available=1, anonymous=False, is_gear=True,
                                armoury_uid="u1")
        await _sale("u1", 111, at=now + 10 * DAY, attribution="auction", confidence=0.88)

        assert (await db.alerts_by_attribution())[0]["verdict"] == "unknown"

    _run(scenario, tmp_path, monkeypatch, "earlier")


def test_the_first_later_capture_is_the_one_that_counts(tmp_path, monkeypatch):
    """The tightest test available: the longer the gap, the more likely ownership simply changed,
    so a nearer capture is stronger evidence than a distant one."""
    async def scenario():
        now = int(time.time())
        await _sale("u1", 111, at=now - 30 * DAY, attribution="auction", confidence=0.88)
        await _sale("u1", 111, at=now - 20 * DAY)                     # nearest, agrees
        await _sale("u1", 777, at=now - 1 * DAY)                      # later, disagrees

        first = (await db.alerts_by_attribution())[0]
        assert first["verdict"] == "confirmed"

    _run(scenario, tmp_path, monkeypatch, "nearest")


# --------------------------------- circularity ------------------------------------------

def test_an_inferred_sale_is_not_used_as_ground_truth(tmp_path, monkeypatch):
    """The circularity trap. `observed_gear_owners` reads the sales table, which now contains sales
    whose seller was itself INFERRED. Scoring against those has the model confirm its own output —
    and it would look like excellent accuracy, because the same rule produced both sides.

    Before attribution was recorded there was no way to filter them, so this could only have got
    worse as the feature produced more of them.
    """
    async def scenario():
        now = int(time.time())
        await _sale("u1", 111, at=now - 10 * DAY, attribution="auction", confidence=0.88)
        # A second inferred sale for the same uid, naming the same player. Not evidence.
        await _sale("u1", 111, at=now - 1 * DAY, attribution="auction", confidence=0.88)

        first = (await db.alerts_by_attribution())[0]
        assert first["verdict"] == "unknown", "an inference was used to confirm an inference"

        uids = {o["uid"] for o in await db.observed_gear_owners()}
        assert "u1" not in uids

    _run(scenario, tmp_path, monkeypatch, "circular")


def test_calibration_ground_truth_excludes_inferred_tracked_gear(tmp_path, monkeypatch):
    """Same rule for the live view, which is the other source `observed_gear_owners` reads."""
    async def scenario():
        await db.upsert_tracked_gear(uid="g1", item_id=399, item_name="M", price=1,
                                     seller_id=111, attribution="auction")
        await db.upsert_tracked_gear(uid="g2", item_id=399, item_name="M", price=1,
                                     seller_id=222, attribution="matcher")

        uids = {o["uid"] for o in await db.observed_gear_owners()}
        assert "g2" in uids and "g1" not in uids

    _run(scenario, tmp_path, monkeypatch, "tracked")


# --------------------------------- the report -------------------------------------------

def test_the_report_separates_bands_and_shows_the_control(tmp_path, monkeypatch, capsys):
    """The control row is what makes the rest readable: a direct capture should score ~100%, and if
    it does not then the ground truth is unreliable and no band above it means anything."""
    async def scenario():
        now = int(time.time())
        await _sale("hi", 111, at=now - 10 * DAY, attribution="auction", confidence=0.923)
        await _sale("lo", 222, at=now - 10 * DAY, attribution="auction", confidence=0.583)
        await _sale("cap", 333, at=now - 10 * DAY, attribution="matcher")
        for uid, seller in (("hi", 111), ("lo", 888), ("cap", 333)):
            await db.upsert_listing(listing_id=f"L{uid}", item_id=399, item_name="M",
                                    seller_id=seller, price=1, available=1, anonymous=False,
                                    is_gear=True, armoury_uid=uid)

        await ah_scraper.show_verdict()
        out = capsys.readouterr().out
        assert ">= 0.88  (pings)" in out and "< 0.65" in out
        assert "<- control" in out
        assert "LOWER BOUND" in out, "the caveat has to travel with the numbers"
        assert "DELETED rather than tuned" in out

    _run(scenario, tmp_path, monkeypatch, "report")


def test_the_report_says_how_much_history_it_cannot_see(tmp_path, monkeypatch, capsys):
    """8,271 sales predate this instrumentation. A report that quietly ignored them would imply the
    sample is the whole history rather than a few days of it."""
    async def scenario():
        await _sale("old", 111, at=int(time.time()) - 30 * DAY)      # no attribution recorded
        await ah_scraper.show_verdict()
        out = capsys.readouterr().out
        assert "predate attribution being recorded" in out
        assert "cannot be backfilled" in out

    _run(scenario, tmp_path, monkeypatch, "old")


def test_the_report_does_not_crash_on_an_empty_database(tmp_path, monkeypatch, capsys):
    async def scenario():
        await ah_scraper.show_verdict()
        assert "nothing to judge" in capsys.readouterr().out

    _run(scenario, tmp_path, monkeypatch, "empty")


# --------------------------------- the human label stream --------------------------------

def test_feedback_stores_the_amount_and_the_prediction_it_was_judged_against(tmp_path, monkeypatch):
    """The amount, never a verdict. "$0" means banked OR wrong match, and which of those it is
    depends on a banking model that will change — so baking today's reading into the stored data
    would make the record unusable the moment the reading improves.

    The predicted range is frozen alongside it, because /mugmin and the protection hours are both
    live-tunable: without this, changing them would silently rewrite what we had promised."""
    async def scenario():
        now = int(time.time())
        sale_id = await _sale("u1", 111, at=now, attribution="auction", confidence=0.88)
        await db.record_mug_feedback(seller_id=111, amount=0,
                                     sale={"id": sale_id, "attribution": "auction",
                                           "confidence": 0.88},
                                     expected={"min": 5_000_000, "max": 15_000_000})

        row = (await db.mug_feedback_rows())[0]
        assert row["amount"] == 0, "a zero mug is a real observation, not a missing one"
        assert row["attribution"] == "auction" and row["confidence"] == 0.88
        assert row["expected_min"] == 5_000_000 and row["expected_max"] == 15_000_000
        assert "verdict" not in row

    _run(scenario, tmp_path, monkeypatch, "fb_store")


def test_the_report_keeps_human_feedback_apart_from_the_passive_labels(tmp_path, monkeypatch, capsys):
    """Merging them would produce a number that is neither, and hide both biases while doing it:
    this sample is selected on alerts someone chose to act on, the other on uids that happen to
    reappear in a named listing."""
    async def scenario():
        now = int(time.time())
        sale_id = await _sale("u1", 111, at=now, attribution="auction", confidence=0.88)
        for amount in (0, 1_000_000, 9_000_000):
            await db.record_mug_feedback(
                seller_id=111, amount=amount,
                sale={"id": sale_id, "attribution": "auction", "confidence": 0.88},
                expected={"min": 5_000_000, "max": 15_000_000})

        await ah_scraper.show_verdict()
        out = capsys.readouterr().out
        assert "/mugged feedback on inferred sellers (3 reported)" in out
        assert "returned $0" in out and "below the prediction" in out
        assert "Kept separate from the table above" in out
        # the band table must be unchanged by it
        assert "checkable" in out

    _run(scenario, tmp_path, monkeypatch, "fb_report")


def test_feedback_on_a_captured_seller_is_not_counted_as_evidence_about_inference(tmp_path,
                                                                                  monkeypatch,
                                                                                  capsys):
    """A $0 mug on a seller we OBSERVED listing the item says something about banking, and nothing
    at all about whether the auction-house inference works."""
    async def scenario():
        now = int(time.time())
        sale_id = await _sale("u1", 111, at=now, attribution="matcher")
        await db.record_mug_feedback(seller_id=111, amount=0,
                                     sale={"id": sale_id, "attribution": "matcher"},
                                     expected={"min": 5_000_000, "max": 15_000_000})

        await ah_scraper.show_verdict()
        out = capsys.readouterr().out
        assert "none of them on an inferred seller yet" in out

    _run(scenario, tmp_path, monkeypatch, "fb_matcher")
