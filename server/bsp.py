"""
TBS (total battle stats) lookup via the BSP predictor (lol-manager.com) — the same source
the war room uses. Returns None when unavailable (callers render 'unknown').
"""

import logging
from typing import Dict, Optional

import httpx

import config

log = logging.getLogger("mug.bsp")

BSP_URL = "https://www.lol-manager.com/api/battlestats/{key}/{user_id}/TornForge"


async def get_tbs(client: httpx.AsyncClient, user_id: int) -> Optional[Dict]:
    if not config.BSP_API_KEY:
        log.warning("BSP_API_KEY not set — cannot fetch TBS")
        return None
    url = BSP_URL.format(key=config.BSP_API_KEY, user_id=user_id)
    try:
        resp = await client.get(url, timeout=15, follow_redirects=True)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("BSP fetch failed for %s: %s", user_id, e)
        return None
    # Result 1/6 + TBS present => usable prediction (mirrors war-room handleFetchBspV2).
    if data and data.get("Result") in (1, 6) and data.get("TBS") is not None:
        return {
            "total": data.get("TBS"),
            "str": data.get("Strength"),
            "spd": data.get("Speed"),
            "def": data.get("Defense"),
            "dex": data.get("Dexterity"),
        }
    return None
