"""
Gear API poller: an item-matcher-captured seller + a uid that appears then vanishes across two
polls = a sale. Drives api_poller.reconcile_item directly (no Torn API), temp DB, fake bot,
enrich stubbed.
"""

import asyncio
import time

import config
from server import api_poller, db, detection


class FakeBot:
    def __init__(self):
        self.alerts = []

    async def post_mug_alert(self, event):
        self.alerts.append(event)


def test_adaptive_schedule_next(monkeypatch):
    """_schedule_next: a fresh cache_timestamp schedules the next poll just after the NEXT snapshot
    is due (cts + offset); a repeated/None cache_timestamp re-checks soon (no busy-loop on stale)."""
    monkeypatch.setattr(config, "CACHE_REPOLL_OFFSET", 62)
    monkeypatch.setattr(config, "CACHE_RETRY_SECONDS", 3)
    api_poller._next_due.clear()
    api_poller._last_cts.clear()
    now = int(time.time())
    cts = now - 30   # we just polled and saw a snapshot stamped ~30s ago (typical serving lag)

    api_poller._schedule_next(7, cts)
    assert api_poller._last_cts[7] == cts
    assert api_poller._next_due[7] == cts + 62          # land just after the next snapshot appears

    # Same cache_timestamp again (snapshot not refreshed yet) -> short retry, don't recompute.
    before = time.time()
    api_poller._schedule_next(7, cts)
    assert before + 2 <= api_poller._next_due[7] <= time.time() + 3.5

    # Failed fetch (cts None) -> short retry too.
    before = time.time()
    api_poller._schedule_next(9, None)
    assert before + 2 <= api_poller._next_due[9] <= time.time() + 3.5
    api_poller._next_due.clear()
    api_poller._last_cts.clear()


def test_incomplete_fetch_does_not_fire_phantom_sales(tmp_path, monkeypatch):
    """The TCT-midnight storm: a multi-page gear item whose 2nd page 504s yields a TRUNCATED
    snapshot. The uids on the failed page must NOT be treated as sold. With complete=False the
    vanish pass is skipped entirely; a later complete poll resumes normal detection."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "partial.db"))
    monkeypatch.setattr(config, "GEAR_VANISH_CONFIRM_POLLS", 1)

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=4106545, name="Vglost")
        await db.store_gear_listing(listing_id="L1", item_id=399, item_name="ArmaLite M-15A4",
                                    seller_id=4106545, price=21_200_000, armoury_uid="19527062233")
        bot = FakeBot()
        # Track it from a complete poll.
        await api_poller.reconcile_gear(bot, 399, {"19527062233": {"price": 21_200_000, "stats": None}})
        assert "19527062233" in await db.get_tracked_uids_for_item(399)
        # Next poll's 2nd page errored -> empty/truncated snapshot, complete=False. NOT a sale.
        await api_poller.reconcile_gear(bot, 399, {}, complete=False)
        await api_poller.drain_sale_tasks()
        assert bot.alerts == []                                     # no phantom sale
        assert "19527062233" in await db.get_tracked_uids_for_item(399)   # still tracked, misses untouched
        # A later COMPLETE poll that's genuinely empty still detects the sale.
        await api_poller.reconcile_gear(bot, 399, {}, complete=True)
        await api_poller.drain_sale_tasks()
        assert len(bot.alerts) == 1
        await db.close_db()

    asyncio.run(scenario())


def test_poll_writes_are_batched_into_one_transaction(tmp_path, monkeypatch):
    """Every commit is an fsync on the single aiosqlite worker thread, so a commit-per-listing
    reconcile stalls every other item's poll and the alert path behind it. One poll of an item must
    cost a constant number of commits, not one per listing."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "batch.db"))
    listing_count = 50

    async def scenario():
        await db.init_db()

        # A full page of gear listings, none of them attributable (all land in unmatched_gear —
        # the write-heaviest path, one row per listing).
        listings = [
            {"price": 1_000_000 + i,
             "item_details": {"uid": str(90_000 + i), "stats": {"damage": 60.0, "accuracy": 50.0}}}
            for i in range(listing_count)
        ]

        async def fake_fetch(client, item_id, bonus, max_pages):
            return ({"name": "ArmaLite M-15A4", "type": "Primary"}, listings, True, 1234567890)

        monkeypatch.setattr(api_poller, "_fetch_pages", fake_fetch)

        commits = []
        real_commit = db._db.commit

        async def counting_commit():
            commits.append(1)
            await real_commit()

        try:
            db._db.commit = counting_commit
            await api_poller.poll_item(None, None, 399, True)
            db._db.commit = real_commit
            # The whole poll — watchlist bookkeeping + 50 unmatched rows + the prune — is one commit.
            assert len(commits) == 1, f"{listing_count} listings cost {len(commits)} commits"
            # ...and the rows are there, i.e. we batched the writes rather than dropping them.
            assert await db.unmatched_count() == listing_count
        finally:
            db._db.commit = real_commit
            await db.close_db()   # a leaked connection thread would hang the whole suite

    asyncio.run(scenario())


def test_batch_commits_once_and_survives_an_error(tmp_path, monkeypatch):
    """db.batch() nests (only the outermost commits) and still commits on the way out of an
    exception — matching the pre-batching behaviour where each helper committed as it went."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "batchctx.db"))

    async def scenario():
        await db.init_db()
        commits = []
        real_commit = db._db.commit

        async def counting_commit():
            commits.append(1)
            await real_commit()

        try:
            db._db.commit = counting_commit

            async with db.batch():
                await db.mark_anon_uid(uid="a1")
                async with db.batch():                 # nested -> still no commit
                    await db.mark_anon_uid(uid="a2")
                assert commits == []
            assert len(commits) == 1

            try:
                async with db.batch():
                    await db.mark_anon_uid(uid="a3")
                    raise RuntimeError("mid-reconcile failure")
            except RuntimeError:
                pass
            assert len(commits) == 2
            db._db.commit = real_commit
            assert await db.get_anon_uid_set() == {"a1", "a2", "a3"}   # nothing rolled back
        finally:
            db._db.commit = real_commit
            await db.close_db()

    asyncio.run(scenario())


def test_gear_uid_vanish_is_a_sale(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "g.db"))
    monkeypatch.setattr(config, "GEAR_VANISH_CONFIRM_POLLS", 1)

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        # item-matcher captured: seller Vglost selling ArmaLite M-15A4 (399) at 21,200,000.
        await db.upsert_seller_seen(user_id=4106545, name="Vglost")
        await db.store_gear_listing(
            listing_id="L1", item_id=399, item_name="ArmaLite M-15A4",
            seller_id=4106545, price=21_200_000, armoury_uid="19527062233",  # captured WITH its uid
        )
        bot = FakeBot()

        # poll 1: the API shows this uid (exact match to the capture) -> we track it.
        await api_poller.reconcile_gear(bot, 399, {"19527062233": {"price": 21_200_000, "stats": {"damage": 68.66}}})
        tracked = await db.get_tracked_uids_for_item(399)
        assert "19527062233" in tracked and tracked["19527062233"]["seller_id"] == 4106545
        assert bot.alerts == []

        # poll 2: still present -> re-confirms a real listing (not a stale match).
        await api_poller.reconcile_gear(bot, 399, {"19527062233": {"price": 21_200_000, "stats": {"damage": 68.66}}})
        # poll 3: the uid is gone -> sold -> alert.
        await api_poller.reconcile_gear(bot, 399, {})
        await api_poller.drain_sale_tasks()   # sale handling runs as a background task now
        assert len(bot.alerts) == 1
        a = bot.alerts[0]
        assert a["seller_name"] == "Vglost"
        assert a["value_change"] == 21_200_000
        assert await db.get_tracked_uids_for_item(399) == {}
        await db.close_db()

    asyncio.run(scenario())


def test_unmapped_uid_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "g2.db"))

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        bot = FakeBot()
        # No captured seller for this item/price => uid never tracked => vanish is a no-op.
        await api_poller.reconcile_gear(bot, 399, {"111": {"price": 99_999_999, "stats": None}})
        assert await db.get_tracked_uids_for_item(399) == {}
        await api_poller.reconcile_gear(bot, 399, {})
        assert bot.alerts == []
        await db.close_db()

    asyncio.run(scenario())


def test_price_collision_is_not_attributed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "g3.db"))

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=2250591, name="MonChoon")
        await db.store_gear_listing(
            listing_id="L1", item_id=399, item_name="ArmaLite M-15A4",
            seller_id=2250591, price=30_000_000, armoury_uid="arm-1",
        )
        bot = FakeBot()
        # 3 live listings at the round price 30M with no uid match -> ambiguous -> none tracked.
        await api_poller.reconcile_gear(bot, 399, {
            "111": {"price": 30_000_000, "stats": None},
            "222": {"price": 30_000_000, "stats": None},
            "333": {"price": 30_000_000, "stats": None},
        })
        assert await db.get_tracked_uids_for_item(399) == {}
        await api_poller.reconcile_gear(bot, 399, {"222": {"price": 30_000_000, "stats": None}})
        assert bot.alerts == []          # nothing was attributed, so nothing fires
        await db.close_db()

    asyncio.run(scenario())


def test_unmatched_and_anon_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "u.db"))

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        # uid 111 has a captured seller; uid 333 is anonymous; uid 222 is unknown.
        await db.upsert_seller_seen(user_id=42, name="Seller")
        await db.store_gear_listing(
            listing_id="L", item_id=399, item_name="ArmaLite M-15A4",
            seller_id=42, price=100, armoury_uid="111",
        )
        await db.mark_anon_uid(uid="333", item_name="ArmaLite M-15A4", stat=68.0, accuracy=57.0, price=300)
        bot = FakeBot()
        current = {
            "111": {"price": 100, "stats": {"damage": 68.6, "accuracy": 57.7}},  # seller -> tracked
            "222": {"price": 200, "stats": {"damage": 69.0, "accuracy": 58.0}},  # no seller -> unmatched
            "333": {"price": 300, "stats": {"damage": 68.0, "accuracy": 57.0}},  # anon -> ignored
        }
        await api_poller.reconcile_gear(bot, 399, current, "ArmaLite M-15A4")

        assert "111" in await db.get_tracked_uids_for_item(399)
        assert await db.unmatched_count() == 1
        uncached = await db.get_uncached_gear()
        assert {(u["item_name"], u["price"]) for u in uncached} == {("ArmaLite M-15A4", 200)}

        # the item-matcher then captures uid 222's seller -> it leaves the unmatched list.
        await db.remove_unmatched_gear("222")
        assert await db.unmatched_count() == 0
        await db.close_db()

    asyncio.run(scenario())


def test_exact_uid_match_wins_despite_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "g4.db"))
    monkeypatch.setattr(config, "GEAR_VANISH_CONFIRM_POLLS", 1)

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=2250591, name="MonChoon")
        # Captured WITH the real uid ("222") -> exact join even though the price collides.
        await db.store_gear_listing(
            listing_id="L1", item_id=399, item_name="ArmaLite M-15A4",
            seller_id=2250591, price=30_000_000, armoury_uid="222",
        )
        bot = FakeBot()
        await api_poller.reconcile_gear(bot, 399, {
            "111": {"price": 30_000_000, "stats": None},
            "222": {"price": 30_000_000, "stats": None},
        })
        assert set((await db.get_tracked_uids_for_item(399)).keys()) == {"222"}
        # 222 still present (re-seen) so its later vanish counts as a real sale.
        await api_poller.reconcile_gear(bot, 399, {
            "111": {"price": 30_000_000, "stats": None},
            "222": {"price": 30_000_000, "stats": None},
        })
        await api_poller.reconcile_gear(bot, 399, {"111": {"price": 30_000_000, "stats": None}})
        await api_poller.drain_sale_tasks()   # sale handling runs as a background task now
        assert len(bot.alerts) == 1 and bot.alerts[0]["seller_name"] == "MonChoon"
        await db.close_db()

    asyncio.run(scenario())


def test_recapture_backfills_armoury_uid(tmp_path, monkeypatch):
    """A listing first stored WITHOUT a uid (old capture, surviving restarts) must get its uid
    backfilled on re-capture, or the poller re-adds it as unmatched forever (the persisting
    weapons bug). Verifies the ON CONFLICT now updates armoury_uid + is_gear."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "r.db"))

    async def scenario():
        await db.init_db()
        await db.upsert_seller_seen(user_id=3631262, name="TheCulturedOne")
        # Old-style capture: same listing id, no armouryID -> not joinable.
        await db.store_gear_listing(
            listing_id="L1", item_id=399, item_name="ArmaLite M-15A4",
            seller_id=3631262, price=200_000_000, armoury_uid=None,
        )
        assert await db.get_gear_uid_map(399) == {}
        # Re-capture WITH the uid -> must backfill so reconcile can join it.
        await db.store_gear_listing(
            listing_id="L1", item_id=399, item_name="ArmaLite M-15A4",
            seller_id=3631262, price=200_000_000, armoury_uid="19568008914",
        )
        umap = await db.get_gear_uid_map(399)
        assert "19568008914" in umap and umap["19568008914"]["seller_id"] == 3631262

        # And reconcile now tracks it (removes from unmatched) instead of re-adding it.
        await db.upsert_unmatched_gear(uid="19568008914", item_id=399, item_name="ArmaLite M-15A4",
                                       price=200_000_000, stat=74.57, accuracy=60.54)
        await api_poller.reconcile_gear(None, 399, {"19568008914": {"price": 200_000_000, "stats": None}})
        assert await db.get_uncached_gear() == []
        assert "19568008914" in await db.get_tracked_uids_for_item(399)
        await db.close_db()

    asyncio.run(scenario())


def test_reprice_does_not_false_sale(tmp_path, monkeypatch):
    """A delist→relist reprice: the uid is briefly absent (one poll), then reappears at a NEW
    price. With confirm=2 that gap must NOT fire a sale; the tracked entry just migrates to the
    new price and keeps the seller."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "rp.db"))
    monkeypatch.setattr(config, "GEAR_VANISH_CONFIRM_POLLS", 2)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=2250591, name="MonChoon")
        await db.store_gear_listing(
            listing_id="L1", item_id=399, item_name="ArmaLite M-15A4",
            seller_id=2250591, price=200_000_000, armoury_uid="222",
        )
        bot = FakeBot()
        # Poll 1: tracked at 200M.
        await api_poller.reconcile_gear(bot, 399, {"222": {"price": 200_000_000, "stats": None}})
        assert (await db.get_tracked_uids_for_item(399))["222"]["price"] == 200_000_000
        # Poll 2: uid gone (mid-reprice). confirm=2 -> only 1 miss, no sale yet.
        await api_poller.reconcile_gear(bot, 399, {})
        assert bot.alerts == []
        assert "222" in await db.get_tracked_uids_for_item(399)
        # Poll 3: reappears at a NEW price -> migrate, reset misses, still no sale.
        await api_poller.reconcile_gear(bot, 399, {"222": {"price": 180_000_000, "stats": None}})
        assert bot.alerts == []
        t = await db.get_tracked_uids_for_item(399)
        assert t["222"]["price"] == 180_000_000 and t["222"]["seller_id"] == 2250591 and t["222"]["misses"] == 0
        await db.close_db()

    asyncio.run(scenario())


def test_gear_delist_suppressed_when_seller_just_acted(tmp_path, monkeypatch):
    """A vanished gear uid where the seller acted seconds ago = a delist, not a buy. It must NOT
    alert and the phantom sale must be removed from the window."""
    import time as _t
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "del.db"))
    monkeypatch.setattr(config, "GEAR_VANISH_CONFIRM_POLLS", 1)
    monkeypatch.setattr(config, "DELIST_GUARD_SECONDS", 180)

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=2250591, name="MonChoon")
        await db.update_seller_enrichment(2250591, {"last_action_ts": int(_t.time())})  # acted now
        await db.store_gear_listing(listing_id="L1", item_id=399, item_name="ArmaLite M-15A4",
                                    seller_id=2250591, price=200_000_000, armoury_uid="222")
        bot = FakeBot()
        # Seen in 2 polls (passes the stale-match guard) so we reach the delist guard.
        await api_poller.reconcile_gear(bot, 399, {"222": {"price": 200_000_000, "stats": None}})
        await api_poller.reconcile_gear(bot, 399, {"222": {"price": 200_000_000, "stats": None}})
        await api_poller.reconcile_gear(bot, 399, {})   # uid gone
        await api_poller.drain_sale_tasks()
        assert bot.alerts == []                                            # suppressed as delist
        assert (await db.seller_window(2250591, 9_000_000))["total"] == 0  # phantom sale dropped
        await db.close_db()

    asyncio.run(scenario())


def test_gear_stale_match_dropped(tmp_path, monkeypatch):
    """A uid attributed a seller but seen in only ONE poll, then gone, is a stale match
    (e.g. a just-marked backlog whose listing was already removed) — no sale, nothing
    recorded.
    """
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "stale.db"))
    monkeypatch.setattr(config, "GEAR_VANISH_CONFIRM_POLLS", 1)

    # Force the behavior this test expects, independent of the default.
    monkeypatch.setattr(config, "GEAR_REQUIRE_RESEEN", True)

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=2250591, name="MonChoon")
        await db.store_gear_listing(
            listing_id="L1",
            item_id=399,
            item_name="ArmaLite M-15A4",
            seller_id=2250591,
            price=200_000_000,
            armoury_uid="222",
        )

        bot = FakeBot()
        await api_poller.reconcile_gear(
            bot, 399, {"222": {"price": 200_000_000, "stats": None}}
        )  # seen=1
        await api_poller.reconcile_gear(bot, 399, {})  # gone
        await api_poller.drain_sale_tasks()

        assert bot.alerts == []
        assert (await db.seller_window(2250591, 9_000_000))["total"] == 0
        await db.close_db()

    asyncio.run(scenario())


def test_gear_vanish_drops_seller_pairing(tmp_path, monkeypatch):
    """Lifecycle: once a gear uid leaves the market we drop the seller↔uid pairing (so a relist must
    be re-marked and can't auto re-fire). The exact-uid listing row is removed; the sale persists."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "drop.db"))
    monkeypatch.setattr(config, "GEAR_VANISH_CONFIRM_POLLS", 1)
    monkeypatch.setattr(config, "MUG_CLAIM_ON_ALERT", False)   # keep the sale in the window to prove it survives unlink

    async def fake_enrich(user_id, client, force=False):
        return await db.get_seller(user_id)

    monkeypatch.setattr(detection, "enrich_seller", fake_enrich)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        await db.upsert_seller_seen(user_id=4106545, name="Vglost")
        # Exact-uid capture (armouryID == API uid) so the pairing is droppable by uid.
        await db.store_gear_listing(
            listing_id="L1", item_id=399, item_name="ArmaLite M-15A4",
            seller_id=4106545, price=21_200_000, armoury_uid="19527062233",
        )
        assert "19527062233" in await db.get_gear_uid_map(399)
        bot = FakeBot()
        await api_poller.reconcile_gear(bot, 399, {"19527062233": {"price": 21_200_000, "stats": None}})
        await api_poller.reconcile_gear(bot, 399, {})   # uid gone -> sale + unlink
        await api_poller.drain_sale_tasks()
        assert len(bot.alerts) == 1                       # still alerts on the vanish
        assert await db.get_gear_uid_map(399) == {}       # ...but the pairing is gone
        # Sales history is retained for the mug-value window.
        assert (await db.seller_window(4106545, 9_000_000))["total"] == 21_200_000
        await db.close_db()

    asyncio.run(scenario())


def test_remark_updates_seller_for_uid(tmp_path, monkeypatch):
    """Re-marking a uid with a different seller updates the pairing: get_gear_uid_map returns the
    MOST RECENT capture, so re-marking fixes a stale/wrong seller without a DB wipe."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "remark.db"))

    async def scenario():
        await db.init_db()
        await db.upsert_seller_seen(user_id=111, name="OldOwner")
        await db.upsert_seller_seen(user_id=222, name="NewOwner")
        # First (stale) capture, then a re-mark of the same uid under a new seller/listing.
        await db.store_gear_listing(listing_id="L1", item_id=399, item_name="ArmaLite M-15A4",
                                    seller_id=111, price=200_000_000, armoury_uid="222uid")
        await db.store_gear_listing(listing_id="L2", item_id=399, item_name="ArmaLite M-15A4",
                                    seller_id=222, price=200_000_000, armoury_uid="222uid")
        umap = await db.get_gear_uid_map(399)
        assert umap["222uid"]["seller_id"] == 222     # most-recent capture wins
        await db.close_db()

    asyncio.run(scenario())


def test_mug_protection_reduces_value(tmp_path, monkeypatch):
    """A freshly-mugged seller in hospital has a protected wallet that decays over MUG_PROTECTION_HOURS.
    We keep only the still-muggable fraction (elapsed-since-last-action / window)."""
    import time as _t
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "prot.db"))
    monkeypatch.setattr(config, "MUG_PROTECTION_HOURS", 12)

    async def scenario():
        await db.init_db()
        await db.set_config("min_mug_value", 1)
        now = int(_t.time())
        # Mugged 6h ago (half the 12h window) -> ~50% still muggable.
        half = {"status_state": "Hospital", "status_details": "Mugged by someone",
                "last_action_ts": now - 6 * 3600}
        win, res, pct = await detection._mug_protect_adjust(half, {"total": 100_000_000})
        assert 45 <= pct <= 55 and 45_000_000 <= win["total"] <= 55_000_000

        # Freshly mugged (acted just now) -> almost fully protected, ~0 muggable.
        fresh = {"status_state": "Hospital", "status_details": "Mugged by someone",
                 "last_action_ts": now}
        win2, res2, pct2 = await detection._mug_protect_adjust(fresh, {"total": 100_000_000})
        assert pct2 >= 99 and win2["total"] <= 1_000_000

        # Not mugged (just hospitalised) -> no reduction.
        hosp = {"status_state": "Hospital", "status_details": "In a car accident",
                "last_action_ts": now - 3600}
        win3, res3, pct3 = await detection._mug_protect_adjust(hosp, {"total": 100_000_000})
        assert pct3 == 0 and win3["total"] == 100_000_000

        # Mugged ~2.5h ago but now OUT of hospital (Okay) and still offline -> protection STILL
        # applies; it outlasts the hospital stay. Regression for the $300M Swiss Army Knife $0 mug.
        okay = {"status_state": "Okay", "online_status": "Offline",
                "status_details": "Mugged by someone", "last_action_ts": now - int(2.5 * 3600)}
        win4, res4, pct4 = await detection._mug_protect_adjust(okay, {"total": 300_000_000})
        assert pct4 > 0 and win4["total"] < 300_000_000
        await db.close_db()

    asyncio.run(scenario())
