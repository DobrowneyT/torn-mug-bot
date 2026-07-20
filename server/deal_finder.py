"""
Under-market deal alerts for the `items` channel.

One cheap Torn API call (`torn?selections=items`) gives every item's market value; we cache it
(refreshed every ~4h) and, each time the poller fetches a watched+enabled item's listings, flag
any listing priced at/under (100 - ITEM_DEAL_PCT)% of market value. Each distinct cheap price is
alerted once (until it leaves the market), so a standing deal doesn't spam the channel.
"""

import logging
import time

import config
from server import db, torn

log = logging.getLogger("mug.deals")

# How long the cached market values stay fresh before a re-fetch.
VALUES_REFRESH_SECONDS = 4 * 60 * 60

# item_id -> set of listing prices already alerted; pruned to what's still on the market so a
# price that vanishes and reappears can alert again.
_alerted: dict = {}


async def maybe_refresh_values(client) -> bool:
    """Refresh the whole-catalogue market values if they're missing or older than the TTL.
    Returns True if a refresh actually happened."""
    age = await db.item_values_age()
    if age is not None and age < VALUES_REFRESH_SECONDS:
        return False
    values = await torn.get_all_item_values(client)
    if not values:
        return False
    await db.upsert_item_values(values)
    log.info("refreshed market values for %d items", len(values))
    return True


async def check_item_deals(bot, item_id, listings, item_name=None, category=None) -> None:
    """listings: [{price, amount}] from this poll. Alert on each listing at/under the deal
    threshold of the item's market value. Dedup per (item, price) until that price is gone."""
    if bot is None or not config.ITEMS_CHANNEL_ID:
        return
    market_value = await db.get_item_value(item_id)
    if not market_value:
        return

    pct = float(await db.get_config("item_deal_pct", config.ITEM_DEAL_PCT))
    threshold = market_value * (1.0 - pct / 100.0)

    seen_prices = set()
    for e in listings:
        price = e.get("price")
        if price is None:
            continue
        seen_prices.add(price)
        if price > threshold:
            continue
        already = _alerted.setdefault(item_id, set())
        if price in already:
            continue
        already.add(price)

        amount = e.get("amount")
        discount = (market_value - price) / market_value * 100.0
        log.info("deal: item=%s price=%s qty=%s (%.0f%% under %s)",
                 item_id, price, amount, discount, market_value)
        await bot.post_deal({
            "item_name": item_name or f"Item {item_id}",
            "item_id": item_id,
            "category": category,
            "unit_price": price,
            "quantity": amount,
            "total": price * amount if amount else price,
            "market_value": market_value,
            "discount_pct": discount,
            "detected_ts": int(time.time()),
        })

    # Forget prices that are no longer listed so they can re-alert if they come back cheap.
    if item_id in _alerted:
        _alerted[item_id] &= seen_prices
        if not _alerted[item_id]:
            del _alerted[item_id]
