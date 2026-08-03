"""Normalisation against a REAL, unedited /market/auctionhouse response.

Captured 2026-08-02 from `https://api.torn.com/v2/market/auctionhouse?limit=10`. Kept verbatim
rather than trimmed to what the parser happens to use: the point of a live fixture is to fail when
Torn changes something we were not paying attention to, and a fixture edited down to the fields we
already handle cannot do that.

It pins several things the OpenAPI spec described only loosely, and which the hand-written fixtures
in test_ah_scraper.py could have got wrong in the same way the parser did.
"""

import asyncio
import json

from server import ah_db, ah_scraper

# --- verbatim, from the live API -------------------------------------------------------------
LIVE = json.loads(r"""
{
  "auctionhouse": [
    {"id": 548761,
     "seller": {"id": 3867338, "name": "HappyPuppy0608"},
     "buyer": {"id": 3705083, "name": "MrFuchs"},
     "timestamp": 1785725391, "price": 67500001, "bids": 6,
     "item": {"id": 657, "uid": 18850799526, "name": "Riot Pants", "type": "Armor",
              "sub_type": null,
              "stats": {"damage": null, "accuracy": null, "armor": 46.57, "quality": 31.37},
              "bonuses": [{"id": 15, "title": "Impregnable",
                           "description": "28% decreased incoming melee damage", "value": 28}],
              "rarity": "yellow"}},
    {"id": 548760,
     "seller": {"id": 3867338, "name": "HappyPuppy0608"},
     "buyer": {"id": 2177652, "name": "PurpleHaze"},
     "timestamp": 1785725382, "price": 75678913, "bids": 7,
     "item": {"id": 656, "uid": 15868612135, "name": "Riot Body", "type": "Armor",
              "sub_type": null,
              "stats": {"damage": null, "accuracy": null, "armor": 46.62, "quality": 32.32},
              "bonuses": [{"id": 15, "title": "Impregnable",
                           "description": "24% decreased incoming melee damage", "value": 24}],
              "rarity": "yellow"}},
    {"id": 547982,
     "seller": {"id": 3372639, "name": "BigMagicBob"},
     "buyer": {"id": 4041998, "name": "Pyrostormz"},
     "timestamp": 1785725304, "price": 67200001, "bids": 6,
     "item": {"id": 253, "uid": 20191028545, "name": "Lorcin 380", "type": "Weapon",
              "sub_type": "Pistol",
              "stats": {"damage": 36.95, "accuracy": 49.37, "armor": null, "quality": 183.23},
              "bonuses": [{"id": 86, "title": "Disarm",
                           "description": "Disables an opponent's weapon upon a hand or arm hit for 7 turns",
                           "value": 7}],
              "rarity": "orange"}},
    {"id": 548355,
     "seller": {"id": 3709844, "name": "Slayers"},
     "buyer": {"id": 3474044, "name": "Saiyaku"},
     "timestamp": 1785724640, "price": 185000001, "bids": 7,
     "item": {"id": 237, "uid": 20253944970, "name": "Kodachi", "type": "Weapon",
              "sub_type": "Slashing",
              "stats": {"damage": 73.69, "accuracy": 62.62, "armor": null, "quality": 183.09},
              "bonuses": [{"id": 48, "title": "Throttle",
                           "description": "75% increased Throat damage", "value": 75}],
              "rarity": "orange"}},
    {"id": 547981,
     "seller": {"id": 4211763, "name": "dariusjackson"},
     "buyer": {"id": 3714674, "name": "Wegerson"},
     "timestamp": 1785724639, "price": 34200001, "bids": 9,
     "item": {"id": 400, "uid": 13896821776, "name": "Guandao", "type": "Weapon",
              "sub_type": "Slashing",
              "stats": {"damage": 70.37, "accuracy": 39.82, "armor": null, "quality": 121.86},
              "bonuses": [{"id": 88, "title": "Quicken",
                           "description": "73% increased passive speed while using this weapon",
                           "value": 73}],
              "rarity": "yellow"}},
    {"id": 547980,
     "seller": {"id": 4211763, "name": "dariusjackson"},
     "buyer": {"id": 4041998, "name": "Pyrostormz"},
     "timestamp": 1785724632, "price": 100000001, "bids": 9,
     "item": {"id": 236, "uid": 15295322565, "name": "Kama", "type": "Weapon",
              "sub_type": "Slashing",
              "stats": {"damage": 47.07, "accuracy": 62.43, "armor": null, "quality": 195.07},
              "bonuses": [{"id": 86, "title": "Disarm",
                           "description": "Disables an opponent's weapon upon a hand or arm hit for 5 turns",
                           "value": 5}],
              "rarity": "orange"}},
    {"id": 547979,
     "seller": {"id": 4211763, "name": "dariusjackson"},
     "buyer": {"id": 3954449, "name": "-Josh-"},
     "timestamp": 1785724626, "price": 33600001, "bids": 9,
     "item": {"id": 1, "uid": 19614253967, "name": "Hammer", "type": "Weapon",
              "sub_type": "Clubbing",
              "stats": {"damage": 22.61, "accuracy": 59.51, "armor": null, "quality": 101.23},
              "bonuses": [{"id": 21, "title": "Plunder",
                           "description": "21% increased money mugged on final hit", "value": 21}],
              "rarity": "yellow"}},
    {"id": 548354,
     "seller": {"id": 3709844, "name": "Slayers"},
     "buyer": {"id": 1967952, "name": "-YouKai-"},
     "timestamp": 1785724624, "price": 153900001, "bids": 6,
     "item": {"id": 238, "uid": 20254148629, "name": "Sai", "type": "Weapon",
              "sub_type": "Piercing",
              "stats": {"damage": 37.31, "accuracy": 60.48, "armor": null, "quality": 167.88},
              "bonuses": [{"id": 42, "title": "Wither",
                           "description": "25% chance to Wither opponent reducing Strength by 25% (x3)",
                           "value": 25},
                          {"id": 101, "title": "Penetrate",
                           "description": "Ignores 31% of armor mitigation", "value": 31}],
              "rarity": "orange"}},
    {"id": 548753,
     "seller": {"id": 3542251, "name": "_Bandit"},
     "buyer": {"id": 3281169, "name": "Skrep"},
     "timestamp": 1785723765, "price": 24210519, "bids": 6,
     "item": {"id": 108, "uid": 17601813334, "name": "9mm Uzi", "type": "Weapon", "sub_type": "SMG",
              "stats": {"damage": 70.81, "accuracy": 47.06, "armor": null, "quality": 98.71},
              "bonuses": [{"id": 55, "title": "Conserve",
                           "description": "25% increased ammo conservation", "value": 25}],
              "rarity": "yellow"}},
    {"id": 548752,
     "seller": {"id": 3542251, "name": "_Bandit"},
     "buyer": {"id": 2177652, "name": "PurpleHaze"},
     "timestamp": 1785723761, "price": 24210519, "bids": 6,
     "item": {"id": 13, "uid": 18916163786, "name": "Raven MP25", "type": "Weapon",
              "sub_type": "Pistol",
              "stats": {"damage": 35.6, "accuracy": 56.39, "armor": null, "quality": 109.92},
              "bonuses": [{"id": 72, "title": "Assassinate",
                           "description": "54% increased damage on the first turn", "value": 54}],
              "rarity": "yellow"}}
  ],
  "_metadata": {
    "links": {
      "prev": "https://api.torn.com/v2/market/auctionhouse?&limit=10&sort=desc&to=1785723761",
      "next": null
    }
  }
}
""")

ROWS = LIVE["auctionhouse"]


def test_every_live_row_normalises():
    """No row in a real page may be silently dropped — a dropped row is a lost ownership record."""
    out = [ah_scraper.normalize(r) for r in ROWS]
    assert all(o is not None for o in out), "some live rows were dropped by normalize"
    assert len(out) == 10


def test_armor_row_maps_exactly():
    """Armour: `sub_type` is null and damage/accuracy are null while `armor` carries the stat.
    The hand-written fixtures all use weapons, so this is the shape they could not have caught."""
    row = ah_scraper.normalize(ROWS[0])
    assert row == {
        "auction_id": 548761, "uid": "18850799526", "item_id": 657, "item_name": "Riot Pants",
        "item_type": "Armor", "sub_type": None,
        "damage": None, "accuracy": None, "armor": 46.57, "quality": 31.37,
        "rarity": "yellow", "bonuses": row["bonuses"],
        "seller_id": 3867338, "seller_name": "HappyPuppy0608",
        "buyer_id": 3705083, "buyer_name": "MrFuchs",
        "price": 67500001, "bids": 6, "ended_at": 1785725391,
    }
    assert json.loads(row["bonuses"])[0]["title"] == "Impregnable"


def test_weapon_row_maps_exactly():
    row = ah_scraper.normalize(ROWS[2])
    assert row["item_type"] == "Weapon" and row["sub_type"] == "Pistol"
    assert row["damage"] == 36.95 and row["accuracy"] == 49.37
    assert row["armor"] is None and row["quality"] == 183.23
    assert row["rarity"] == "orange" and row["uid"] == "20191028545"


def test_multiple_bonuses_are_preserved():
    """The Sai carries two. Storing only the first would quietly lose data we cannot re-fetch
    once the auction falls out of Torn's window."""
    bonuses = json.loads(ah_scraper.normalize(ROWS[7])["bonuses"])
    assert [b["title"] for b in bonuses] == ["Wither", "Penetrate"]


def test_uids_exceed_32_bit_range():
    """Live uids are ~2e10, well past INT32. Worth pinning: anything that quietly narrows them
    (a schema change, a cast) corrupts the only identifier #12's join has."""
    uids = [int(ah_scraper.normalize(r)["uid"]) for r in ROWS]
    assert max(uids) > 2**31, "fixture no longer covers the large-uid case"
    assert all(u > 0 for u in uids)


def test_uids_are_unique_across_the_page():
    """Sanity on the premise of the whole feature: a uid identifies one physical item."""
    uids = [ah_scraper.normalize(r)["uid"] for r in ROWS]
    assert len(set(uids)) == len(uids)


def test_torns_own_prev_link_bounds_on_the_oldest_row_inclusively():
    """This is where the backfill's `to` handling comes from, so pin it rather than remember it.

    Torn's `prev` is `...&sort=desc&to=1785723761`, and 1785723761 is the timestamp of the LAST row
    on the page — not one below it. Their own pagination re-requests the boundary row and expects
    the client to dedupe. Stepping to oldest-1 instead would skip any auction sharing that second.
    """
    prev = LIVE["_metadata"]["links"]["prev"]
    oldest = min(r["timestamp"] for r in ROWS)
    assert f"to={oldest}" in prev
    assert f"to={oldest - 1}" not in prev
    # ...and `next` is null at the newest end, so `prev` is the direction the backfill walks.
    assert LIVE["_metadata"]["links"]["next"] is None


def test_a_live_page_stores_and_reads_back(tmp_path):
    async def scenario():
        await ah_db.init_db(str(tmp_path / "live.db"))
        try:
            rows = [ah_scraper.normalize(r) for r in ROWS]
            assert await ah_db.record_auctions(rows) == 10
            assert await ah_db.record_auctions(rows) == 0        # replaying a page is a no-op

            win = await ah_db.owner_of_uid(18850799526)          # integer, as the API sends it
            assert win["buyer_name"] == "MrFuchs" and win["price"] == 67500001

            s = await ah_db.stats()
            assert s["rows"] == 10 and s["with_uid"] == 10
            assert s["oldest"] == 1785723761 and s["newest"] == 1785725391
            # 10 auctions over ~27 minutes — the rate that sizes a full-history backfill
            assert s["buyers"] == 8, "two buyers won twice on this page"
        finally:
            await ah_db.close_db()

    asyncio.run(scenario())
