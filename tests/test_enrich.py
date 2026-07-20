"""Seller enrichment: the v2 profile exposes faction_id as a flat scalar on the profile (no
nested "faction" object). Regression guard for the hospitalize-faction filter that was always
seeing faction_id=0 because it read prof["faction"]["faction_id"]."""

import asyncio

import config
from server import bsp, db, enrich, torn


def test_enrich_captures_flat_faction_id(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "e.db"))

    # v2 /user/{id}/profile (inner "profile" object, which torn.get_profile returns).
    async def fake_profile(client, user_id):
        return {
            "id": user_id, "name": "MonChoon_616", "faction_id": 10739,
            "status": {"state": "Hospital", "until": 1782223429,
                       "description": "In hospital", "details": "Shot themselves in the foot"},
            "last_action": {"status": "Online", "timestamp": 1782222409},
        }

    async def none_job(client, user_id):
        return None

    async def none_tbs(client, user_id):
        return None

    monkeypatch.setattr(torn, "get_profile", fake_profile)
    monkeypatch.setattr(torn, "get_job", none_job)
    monkeypatch.setattr(bsp, "get_tbs", none_tbs)

    async def scenario():
        await db.init_db()
        await db.upsert_seller_seen(user_id=2250591, name="MonChoon_616")
        seller = await enrich.enrich_seller(2250591, client=None, force=True)
        assert seller["faction_id"] == 10739
        assert seller["status_state"] == "Hospital"
        assert seller["status_details"] == "Shot themselves in the foot"
        # In the hospitalize list -> the filter matches.
        await db.add_hospitalize_faction(10739)
        assert await detection_is_hospitalize(seller) is True
        await db.close_db()

    asyncio.run(scenario())


async def detection_is_hospitalize(seller):
    from server import detection
    return await detection._is_hospitalize_faction(seller)
