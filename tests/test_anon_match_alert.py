"""#12 end to end: an anonymous uid resolved from an AH win, alerted with its provenance.

The precedence rules are the whole feature, and each of them is a way to blame the wrong person:

  direct beats inferred   a captured seller must override an AH inference, and must do so even
                          when the inference got there first
  later beats earlier     ownership is the most recent observation, from any source
  off means off           `off` must reproduce today's behaviour exactly — an anon uid stays a
                          dead end — or turning the feature off would not actually turn it off
"""

import asyncio
import time

import config
from bot import formatter
from server import ah_db, api_poller, db, detection

DAY = 86400
ITEM = 399
UID = "555000111"


class FakeBot:
    def __init__(self):
        self.alerts = []

    async def post_mug_alert(self, event):
        self.alerts.append(event)


def _win(auction_id, uid, buyer_id, ended_at, price=9_000_000):
    return {
        "auction_id": auction_id, "uid": uid, "item_id": ITEM, "item_name": "ArmaLite M-15A4",
        "item_type": "Weapon", "sub_type": "Rifle", "damage": 1.0, "accuracy": 1.0,
        "armor": None, "quality": 1.0, "rarity": None, "bonuses": None,
        "seller_id": 1, "seller_name": "S", "buyer_id": buyer_id, "buyer_name": f"Buyer{buyer_id}",
        "price": price, "bids": 5, "ended_at": ended_at,
    }


def _setup(monkeypatch, tmp_path, name, last_action_ago=DAY):
    """`last_action_ago=None` simulates a profile lookup that gave us nothing — which is now a
    material state, not a cosmetic one: without a last action the de-list guard cannot run."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / f"{name}_main.db"))

    async def fake_enrich(user_id, client, force=False):
        row = dict(await db.get_seller(user_id) or {})
        row.setdefault("name", f"Player{user_id}")
        row["last_action_ts"] = (None if last_action_ago is None
                                 else int(time.time()) - last_action_ago)
        return row

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)
    monkeypatch.setattr(detection, "enrich_clothing", fake_enrich)


def _run(scenario, tmp_path, monkeypatch, name, last_action_ago=DAY):
    _setup(monkeypatch, tmp_path, name, last_action_ago)

    async def wrapper():
        await ah_db.init_db(str(tmp_path / f"{name}_ah.db"))
        await db.init_db()
        try:
            await db.set_config("min_mug_value", 1)
            await scenario()
        finally:
            await db.close_db()
            await ah_db.close_db()

    asyncio.run(wrapper())


async def _anon_uid_won_at_auction(buyer_id=4242, age_days=2):
    """An anonymous listing whose uid was won at auction `age_days` ago."""
    from server.ah_scraper import run_match
    now = int(time.time())
    await db.mark_anon_uid(uid=UID, item_name="ArmaLite M-15A4", stat=1.0, accuracy=1.0, price=1)
    await ah_db.record_auctions([_win(1, UID, buyer_id, now - age_days * DAY)])
    await run_match()


def _listing(price=50_000_000):
    return {UID: {"price": price, "stats": {"damage": 55.5, "accuracy": 44.4}}}


# --------------------------------- the happy path ---------------------------------------

def test_an_anon_uid_with_an_ah_win_is_attributed_and_alerts(tmp_path, monkeypatch):
    """The point of the whole epic: a listing that produced no alert at all now produces one."""
    async def scenario():
        await _anon_uid_won_at_auction()
        bot = FakeBot()

        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        tracked = await db.get_tracked_uids_for_item(ITEM)
        assert UID in tracked, "anon uid was not attributed from the auction house"
        assert tracked[UID]["seller_id"] == 4242
        assert tracked[UID]["attribution"] == "auction"

        # ...it sells
        await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
        await api_poller.drain_sale_tasks()

        assert len(bot.alerts) == 1
        event = bot.alerts[0]
        assert event["attribution"] == "auction"
        assert event["anon_match"]["buyer_id"] == 4242
        assert event["silent"] is True, "default mode is silent"

    _run(scenario, tmp_path, monkeypatch, "happy")


def test_the_embed_states_the_inference_and_its_evidence(tmp_path, monkeypatch):
    """Acting on an inference and acting on an observation are different decisions. They must not
    look the same at a glance, and the reader needs the evidence to judge it."""
    async def scenario():
        await _anon_uid_won_at_auction(buyer_id=4242, age_days=3)
        match = await db.get_anon_match(UID)
        embed = formatter.build_mug_alert_embed({
            "variant": "mug", "seller_id": 4242, "seller_name": "Buyer4242",
            "item_name": "ArmaLite M-15A4", "value_change": 50_000_000, "num_sales": 1,
            "mug_min": 1, "mug_max": 2, "detected_ts": int(time.time()),
            "attribution": "auction", "anon_match": match, "sales": [],
        })
        text = embed.description or ""
        assert "Anon match" in text
        assert "Buyer4242" in text
        assert "$9,000,000" in text
        assert "88%" in text, "the measured confidence must be shown, not just asserted"
        # A live Discord timestamp rather than a baked "3d ago": the embed stays in the channel and
        # its own claim about how old the evidence is has to age with it.
        assert f"<t:{match['won_at']}:R>" in text

    _run(scenario, tmp_path, monkeypatch, "embed")


# --------------------------------- precedence -------------------------------------------

def test_a_captured_seller_overrides_an_existing_inference(tmp_path, monkeypatch):
    """Direct evidence beats inferred, INCLUDING when the inference got there first. The upsert
    used to leave seller_id alone on conflict, which would have pinned the weaker answer."""
    async def scenario():
        await _anon_uid_won_at_auction(buyer_id=4242)
        bot = FakeBot()
        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        assert (await db.get_tracked_uids_for_item(ITEM))[UID]["seller_id"] == 4242

        # The item-matcher now captures the REAL seller for that uid, while it is STILL TRACKED
        # under the inference. Deliberately no delete here: the tracked branch short-circuits the
        # resolution chain, so this is the path where a stale inference actually survives.
        await db.upsert_listing(listing_id="L9", item_id=ITEM, item_name="ArmaLite M-15A4",
                                seller_id=7777, price=50_000_000, available=1, anonymous=False,
                                is_gear=True, armoury_uid=UID)

        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        row = (await db.get_tracked_uids_for_item(ITEM))[UID]
        assert row["seller_id"] == 7777, "an inference outranked a direct capture"
        assert row["attribution"] == "matcher"

    _run(scenario, tmp_path, monkeypatch, "prec")


def test_the_newest_auction_wins(tmp_path, monkeypatch):
    """A uid changes hands repeatedly; only the latest transfer can be current ownership."""
    async def scenario():
        from server.ah_scraper import run_match
        now = int(time.time())
        await db.mark_anon_uid(uid=UID, item_name="M", stat=1.0, accuracy=1.0, price=1)
        await ah_db.record_auctions([
            _win(1, UID, 111, now - 300 * DAY),
            _win(2, UID, 222, now - 2 * DAY),
        ])
        await run_match()
        bot = FakeBot()
        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        assert (await db.get_tracked_uids_for_item(ITEM))[UID]["seller_id"] == 222

    _run(scenario, tmp_path, monkeypatch, "newest")


# --------------------------------- the modes --------------------------------------------

def test_off_reproduces_todays_behaviour_exactly(tmp_path, monkeypatch):
    """If `off` did not fully disable it, there would be no way to back the feature out."""
    async def scenario():
        await _anon_uid_won_at_auction()
        await db.set_config("anonmatch_mode", "off")
        bot = FakeBot()

        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        assert UID not in await db.get_tracked_uids_for_item(ITEM)

        await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
        await api_poller.drain_sale_tasks()
        assert bot.alerts == [], "an anon uid alerted with the feature off"

    _run(scenario, tmp_path, monkeypatch, "off")


def test_ping_mode_pings_only_above_the_confidence_bar(tmp_path, monkeypatch):
    async def scenario():
        await db.set_config("anonmatch_mode", "ping")
        await _anon_uid_won_at_auction(age_days=2)          # 0.880, at the bar
        bot = FakeBot()
        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
        await api_poller.drain_sale_tasks()
        assert len(bot.alerts) == 1 and bot.alerts[0]["silent"] is False

    _run(scenario, tmp_path, monkeypatch, "ping")


def test_weak_evidence_posts_but_does_not_ping(tmp_path, monkeypatch):
    """Old evidence is weaker, not worthless — 58% at a year is still worth seeing. It must post."""
    async def scenario():
        await db.set_config("anonmatch_mode", "ping")
        await _anon_uid_won_at_auction(age_days=500)        # 0.583, well under the bar
        bot = FakeBot()
        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
        await api_poller.drain_sale_tasks()
        assert len(bot.alerts) == 1, "a weak inference was dropped instead of posted quietly"
        assert bot.alerts[0]["silent"] is True

    _run(scenario, tmp_path, monkeypatch, "weak")


def test_a_normal_capture_is_unaffected(tmp_path, monkeypatch):
    """The regression that would matter most: ordinary captured-seller alerts must not gain a
    badge, change their ping behaviour, or otherwise notice this feature exists."""
    async def scenario():
        await db.upsert_listing(listing_id="L1", item_id=ITEM, item_name="ArmaLite M-15A4",
                                seller_id=3333, price=50_000_000, available=1, anonymous=False,
                                is_gear=True, armoury_uid=UID)
        bot = FakeBot()
        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        row = (await db.get_tracked_uids_for_item(ITEM))[UID]
        assert row["seller_id"] == 3333 and row["attribution"] == "matcher"

        await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
        await api_poller.drain_sale_tasks()
        assert len(bot.alerts) == 1
        assert bot.alerts[0]["attribution"] == "matcher"
        assert bot.alerts[0]["anon_match"] is None
        assert bot.alerts[0]["silent"] is False, "a captured seller must still ping"

    _run(scenario, tmp_path, monkeypatch, "normal")


# --------------------------------- the evidence outlives the lookup ---------------------

def test_the_alert_still_has_its_evidence_after_the_match_table_is_rebuilt(tmp_path, monkeypatch):
    """The bug behind "seller inferred from the auction house: this player won this item".

    `anon_matches` is recomputed WHOLESALE by every scrape, and a uid drops out of the candidate
    set as soon as it is attributed. The sale fires whenever the listing vanishes, which is hours
    later — by then the row the alert wanted to read is gone, and the embed degraded to a sentence
    with no buyer, no price, no date and no confidence. Precisely the cases where you most need to
    judge the claim.

    Evidence is therefore captured at attribution time. Only the ping POLICY is re-read.
    """
    async def scenario():
        from server.ah_scraper import run_match

        bot = FakeBot()
        # The real sequence, not a simulation of it. An UNMATCHED uid (not one stashed as
        # anonymous) is the case that bites: reconcile removes it from unmatched_gear the moment it
        # is attributed, so the very next match pass no longer counts it as a candidate.
        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        await ah_db.record_auctions([_win(1, UID, 4242, int(time.time()) - 3 * DAY)])
        await run_match()
        assert await db.get_anon_match(UID), "precondition: the uid resolves at attribution time"

        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        assert (await db.get_tracked_uids_for_item(ITEM))[UID]["attribution"] == "auction"

        await run_match()
        assert await db.get_anon_match(UID) is None, "precondition: the lookup no longer resolves"

        await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
        await api_poller.drain_sale_tasks()

        assert len(bot.alerts) == 1
        m = bot.alerts[0]["anon_match"]
        assert m, "the alert lost the evidence for its own claim"
        assert m["buyer_name"] == "Buyer4242"
        assert m["price"] == 9_000_000
        assert m["confidence"] and m["won_at"]

        text = formatter.build_mug_alert_embed(bot.alerts[0]).description or ""
        assert "this player won this item" not in text, "degraded to the evidence-free sentence"
        assert "Buyer4242" in text and "$9,000,000" in text and "% likely correct" in text

    _run(scenario, tmp_path, monkeypatch, "evidence")


def test_a_capture_that_supersedes_an_inference_drops_the_stale_evidence(tmp_path, monkeypatch):
    """Once the item-matcher names the seller, the AH win is no longer why we believe it. Leaving
    the evidence behind would have the embed cite reasoning it is not using."""
    async def scenario():
        await _anon_uid_won_at_auction(buyer_id=4242)
        bot = FakeBot()
        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        assert (await db.get_tracked_uids_for_item(ITEM))[UID]["attribution_evidence"]

        await db.upsert_listing(listing_id="L1", item_id=ITEM, item_name="ArmaLite M-15A4",
                                seller_id=3333, price=50_000_000, available=1, anonymous=False,
                                is_gear=True, armoury_uid=UID)
        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")

        row = (await db.get_tracked_uids_for_item(ITEM))[UID]
        assert row["attribution"] == "matcher" and row["seller_id"] == 3333
        assert row["attribution_evidence"] is None, "kept AH evidence for a captured seller"

    _run(scenario, tmp_path, monkeypatch, "supersede")


# --------------------------------- the profile is mandatory -----------------------------

def test_an_inferred_seller_active_right_now_is_not_posted_at_all(tmp_path, monkeypatch):
    """A recent last action is not a caveat on an inferred alert, it is a competing explanation for
    the whole thing — and a likelier one. We never saw this player list anything; the item vanishing
    while they were active is far better explained by them pulling their own listing.

    Note the mode: `ping` would normally post this WITH a de-list tag. For an inference it does not
    get a vote."""
    async def scenario():
        await db.set_config("delist_mode", "ping")
        await _anon_uid_won_at_auction()
        bot = FakeBot()
        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
        await api_poller.drain_sale_tasks()
        assert bot.alerts == [], "posted an inference that was almost certainly a de-list"

    _run(scenario, tmp_path, monkeypatch, "delist", last_action_ago=30)


def test_a_captured_seller_active_right_now_is_still_posted_and_tagged(tmp_path, monkeypatch):
    """The other side of it. For a seller we OBSERVED listing the item, a recent action is a
    caveat worth showing rather than grounds to bin the alert — that judgement stays with
    /delist."""
    async def scenario():
        await db.set_config("delist_mode", "ping")
        await db.upsert_listing(listing_id="L1", item_id=ITEM, item_name="ArmaLite M-15A4",
                                seller_id=3333, price=50_000_000, available=1, anonymous=False,
                                is_gear=True, armoury_uid=UID)
        bot = FakeBot()
        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
        await api_poller.drain_sale_tasks()
        assert len(bot.alerts) == 1
        assert bot.alerts[0]["delist_active_secs"] is not None, "the de-list tag was lost"

    _run(scenario, tmp_path, monkeypatch, "delist_capture", last_action_ago=30)


def test_an_inferred_seller_with_no_profile_is_not_posted(tmp_path, monkeypatch):
    """Without a last action the de-list guard cannot run at all. Stacking that unknown on top of
    an inference is not an alert, it is a guess — and it is the case that produced embeds with a
    bare user id and no last-action line."""
    async def scenario():
        await _anon_uid_won_at_auction()
        bot = FakeBot()
        await api_poller.reconcile_gear(bot, ITEM, _listing(), item_label="ArmaLite M-15A4")
        await api_poller.reconcile_gear(bot, ITEM, {}, item_label="ArmaLite M-15A4")
        await api_poller.drain_sale_tasks()
        assert bot.alerts == []

    _run(scenario, tmp_path, monkeypatch, "noprofile", last_action_ago=None)


def test_the_embed_never_shows_a_bare_user_id_for_an_inferred_seller(tmp_path, monkeypatch):
    """You cannot recognise a target from a number. The AH win names the same player, so it is the
    fallback when the profile did not give us a name."""
    async def scenario():
        await _anon_uid_won_at_auction(buyer_id=4242)
        match = await db.get_anon_match(UID)
        event = detection._build_event(
            4242, {"last_action_ts": int(time.time()) - DAY}, {"total": 5, "count": 1, "sales": []},
            type("R", (), {"mug_min": 1, "mug_max": 2})(),
            attribution="auction", anon_match=match)
        assert event["seller_name"] == "Buyer4242"

        text = formatter.build_mug_alert_embed(event).description or ""
        assert "Owner: **4242**" not in text

    _run(scenario, tmp_path, monkeypatch, "name")


def test_the_last_action_line_is_always_present(tmp_path, monkeypatch):
    """A line that vanishes reads as "nothing to report" rather than "we could not find out"."""
    async def scenario():
        event = detection._build_event(
            4242, {}, {"total": 5, "count": 1, "sales": []},
            type("R", (), {"mug_min": 1, "mug_max": 2})())
        text = formatter.build_mug_alert_embed(event).description or ""
        assert "Last action:" in text and "*unknown*" in text

    _run(scenario, tmp_path, monkeypatch, "lastaction")
