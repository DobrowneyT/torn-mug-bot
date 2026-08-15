"""
Hand-crafted fixtures to render the three embed types without a live server (Track A).

The numbers mirror the reference screenshots so the rendered embeds can be compared
side-by-side with what the old bot produced (e.g. the "Mr. Blobby" 5%-15% mug range).
"""

import time
from typing import Dict


def mock_mug_event(now: int = None) -> Dict:
    now = now or int(time.time())
    return {
        "variant": "mug",
        "seller_id": 2250591,
        "seller_name": "Chocobo",
        "item_id": 225,
        "item_name": "Mag 7",
        "item_stats": {"damage": 66.86, "accuracy": 70.0},
        "value_change": 45_488_286,
        "num_sales": 3,
        "mug_min": 2_274_414,
        "mug_max": 6_823_243,
        "tbs": 9_960_000_000,
        # Both sales are AFTER the last action, which is the only shape a real alert can have: any
        # sale the seller has been online since is dropped as banked before the embed is built, so a
        # fixture with old sales under a recent last action depicts an event that cannot occur.
        "last_action_ts": now - 95 * 60,
        "online_status": "Online",        # -> green border
        "status_state": "Okay",
        "detected_ts": now - 20 * 60,
        "faction": "ARKHAM A$YLUM",
        "in_clothing_store": False,
        # The triggering sale IS in the window, so the headline item appears here too — that is
        # what a real event looks like, and it is the reason the separate "Item:" line was dropped.
        # item_id is what lets the list link to the market.
        "sales": [
            {"item_id": 206, "item_name": "Xanax", "qty": 1, "price": 488_286,
             "ts": now - 80 * 60},
            {"item_id": 225, "item_name": "Mag 7", "qty": 1, "price": 45_000_000,
             "ts": now - 20 * 60},
        ],
    }


def mock_hospital_mug_event(now: int = None) -> Dict:
    now = now or int(time.time())
    return {
        "variant": "hospital",
        "seller_id": 1234567,
        "seller_name": "TheCulturedOne",
        "was_mugged": True,
        "money_on_hand": 47_457_000,
        "mug_min": 2_372_850,
        "mug_max": 7_118_550,
        "tbs": 1_970_000,
        "last_action_ts": now - 50 * 60,
        "online_status": "Offline",
        "status_state": "Hospital",       # -> red border
        "detected_ts": now - 16 * 3600,
    }


def mock_status(now: int = None) -> Dict:
    now = now or int(time.time())
    watchlist = [
        {"item_id": 233, "label": "BT MP9", "enabled": True, "is_gear": True, "category": "Secondary", "captured": 4, "tracked": 3, "last_polled_ts": now - 12},
        {"item_id": 399, "label": "ArmaLite M-15A4", "enabled": True, "is_gear": True, "category": "Primary", "captured": 7, "tracked": 5, "last_polled_ts": now - 20},
        {"item_id": 146, "label": "Yasukuni Sword", "enabled": True, "is_gear": True, "category": "Melee", "captured": 3, "tracked": 2, "last_polled_ts": now - 15},
        {"item_id": 680, "label": "EOD Helmet", "enabled": True, "is_gear": True, "category": "Defensive", "captured": 9, "tracked": 9, "last_polled_ts": now - 10},
        {"item_id": 206, "label": "Xanax", "enabled": True, "is_gear": False, "category": "Drug", "captured": 2, "tracked": 1, "last_polled_ts": now - 8},
        {"item_id": 26, "label": "AK-47", "enabled": False, "is_gear": True, "category": "Primary", "captured": 0, "tracked": 0, "last_polled_ts": None},
    ]
    unmatched = [
        {"item_name": "EOD Helmet", "stat": 55.32, "accuracy": 0, "price": 15_500_000_000},
        {"item_name": "BT MP9", "stat": 67.98, "accuracy": 58.72, "price": 43_000_000},
        {"item_name": "ArmaLite M-15A4", "stat": 68.66, "accuracy": 57.76, "price": 21_200_000},
    ]
    unmatched_items = [
        {"item_id": 533, "item_name": "Can of Taurine Elite"},
        {"item_id": 555, "item_name": "Can of X-MASS"},
        {"item_id": 206, "item_name": "Xanax"},
    ]
    return {
        "generated_ts": now,
        "watchlist": watchlist,
        "unmatched": unmatched,
        "unmatched_items": unmatched_items,
        "counts": {"regular": 12, "gear": 34, "unmatched": 187, "unmatched_items": 3, "anon": 9},
        "api_estimate": {"enabled": 3, "calls_per_poll": 5, "calls_per_min": 10},
    }


def mock_deal(now: int = None) -> Dict:
    now = now or int(time.time())
    return {
        "item_name": "Xanax",
        "item_id": 206,
        "category": "Drug",
        "unit_price": 425_000,
        "quantity": 3,
        "total": 1_275_000,
        "market_value": 850_000,
        "discount_pct": 50.0,
        "detected_ts": now,
    }
