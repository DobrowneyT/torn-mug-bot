"""
Build discord.Embed objects for the mug bot's three channels:

  - logs   : mug-alert embeds, one per qualifying sale event (build_mug_alert_embed).
             Two variants — a normal "🤑 Mug opportunity!" and a "🚑 Hospital mug
             opportunity!" (money-on-hand + "Was Mugged") — matching the reference
             "Mr. Blobby, TNL Mugger" screenshots.
  - status : the status-tracker page (build_status_embeds) — a cache-counts embed plus
             a tracking embed with a 🟢/🔴 dot per actively-viewed page (mirrors the
             original "Mug Bot Cache/Tracking Status" screenshots).
  - items  : under-market deal alerts (build_deal_embed).

The formatter is pure: it only reads the event dicts the server hands it, so it can be
exercised end-to-end with scratch/format_preview.py + mock_data.py before any real data
flows. Relative times use Discord's native ``<t:unix:R>`` so they stay live in-channel.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional

import discord

import config

COLOR_GREEN = 0x2ECC71       # inactive > 5m — GO NOW, best mug
COLOR_RED = 0xE74C3C         # active < 1m — online, may bank/fight
COLOR_YELLOW = 0xF1C40F      # last action 1–5m ago — borderline
COLOR_BLUE = 0x3498DB        # hospital / overseas — good but WAIT
COLOR_HOSPITALIZE = 0xE67E22  # orange — flagged faction, hospitalize don't mug
COLOR_DEAL = 0x3498DB        # blue — item deal
COLOR_STATUS_OK = 0x2ECC71   # green — tracker has something live
COLOR_STATUS_OFF = 0x95A5A6  # grey — nothing actively tracked

DOT_ONLINE = "🟢"
DOT_IDLE = "🟡"
DOT_OFFLINE = "🔴"


def _alert_color(event: Dict) -> int:
    """Border colour = mug quality:
      🔵 blue   = hospital / overseas — a good target but you must WAIT (or travel) to act.
      🟢 green  = inactive > 5m (or unknown) — GO NOW, the safest/best mug.
      🟡 yellow = last action 1–5m ago — borderline, they were recently around.
      🔴 red    = active < 1m ago — online right now, may bank/fight back.
    """
    if event.get("variant") in ("hospital", "overseas"):
        return COLOR_BLUE
    if event.get("status_state") in ("Hospital", "Traveling", "Abroad"):
        return COLOR_BLUE
    la = event.get("last_action_ts")
    if not la:
        return COLOR_GREEN
    delta = int(datetime.now(timezone.utc).timestamp()) - int(la)
    if delta < 60:
        return COLOR_RED
    if delta <= 5 * 60:
        return COLOR_YELLOW
    return COLOR_GREEN


def _dlen(s: str) -> int:
    """Discord counts message/embed length in UTF-16 code units (so 🔫/📦/🛡️ and other
    non-BMP emoji count as 2, not 1). Budget by THIS, not Python len(), or an embed full of emoji
    rows silently overflows 4096 and Discord truncates it mid-row (dropping the tail)."""
    return len(s.encode("utf-16-le")) // 2


def _status_dot(event: Dict) -> str:
    status = event.get("online_status")
    if status == "Online":
        return DOT_ONLINE
    if status == "Idle":
        return DOT_IDLE
    if status == "Offline":
        return DOT_OFFLINE
    return _online_dot(event.get("last_action_ts"))


# --------------------------------- formatting helpers ---------------------------------

def fmt_money(n: Optional[float]) -> str:
    if n is None:
        return "?"
    return "$" + f"{int(round(n)):,}"


def fmt_compact(n: Optional[float]) -> str:
    """1_970_000 -> '1.97M', 9_960_000_000 -> '9.96B', 14_100 -> '14.1K'."""
    if n is None:
        return "?"
    n = float(n)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.2f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def _relative(ts: Optional[int]) -> str:
    """Discord live relative timestamp, e.g. '<t:123:R>' -> '2 hours ago'."""
    return f"<t:{int(ts)}:R>" if ts else "?"


def _auction_age(match: dict) -> str:
    """" 3 days ago" / " on <date>" for an AH win — always something, never blank.

    Prefers `won_at` (a real timestamp, so Discord renders it live and it stays correct as the
    embed ages) over the stored `age_days`, which froze at the moment the uid was matched.
    """
    won_at = match.get("won_at")
    if won_at:
        return f" {_relative(won_at)}"
    age = match.get("age_days")
    if age is None:
        return ""
    if age < 1:
        return " today"
    return f" {age}d ago" if age < 60 else f" {age // 30}mo ago"


def _online_dot(last_action_ts: Optional[int]) -> str:
    if not last_action_ts:
        return DOT_OFFLINE
    delta = int(datetime.now(timezone.utc).timestamp()) - int(last_action_ts)
    if delta < 5 * 60:
        return DOT_ONLINE
    if delta < 15 * 60:
        return DOT_IDLE
    return DOT_OFFLINE


def _updated_footer(ts: Optional[int]) -> str:
    if not ts:
        return "mug bot"
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return f"Last update: {dt.strftime('%Y-%m-%d %H:%M UTC')}"


# ------------------------------------ mug alert ---------------------------------------

def build_mug_alert_embed(event: Dict) -> discord.Embed:
    """One mug-alert embed for the `logs` channel.

    Expected keys (all optional unless noted):
      variant            "mug" (default) | "hospital"
      seller_id (req)    Torn user id (drives Attack/Profile links)
      seller_name (req)  display name
      value_change       cumulative sale value in the window (the cash-on-hand proxy)
      num_sales          how many sales rolled into value_change
      money_on_hand      actual cash if known (rendered instead of value_change)
      mug_min / mug_max  computed mug range
      tbs                total battle stats (raw int, rendered compact)
      last_action_ts     unix; rendered relative + online dot
      status_dot         override for the online dot
      detected_ts        unix; when the alert fired
      faction            faction name string
      was_mugged         bool -> "(Was Mugged)" tag
      in_clothing_store  bool -> clothing-store tag (mug already reduced upstream)
      sales              [{item_name, qty, price, ts}] optional per-sale breakdown
    """
    variant = event.get("variant", "mug")
    name = event.get("seller_name", "Unknown")
    uid = event.get("seller_id")

    hospitalize = event.get("hospitalize")
    if hospitalize:
        title = "🏥 HOSPITALIZE — do NOT mug! 🏥"
    elif variant == "hospital":
        title = "🚑 Hospital mug opportunity! 🚑"
    elif variant == "overseas":
        title = "✈️ Overseas mug opportunity! ✈️"
    else:
        title = "🤑 Mug opportunity! 🤑"
    # Flagged-faction targets get a loud orange border; otherwise colour by activity.
    color = COLOR_HOSPITALIZE if hospitalize else _alert_color(event)

    lines: List[str] = []

    if event.get("detected_ts"):
        ts = int(event["detected_ts"])
        # <t:..:R> is Discord's live relative time (auto counts up); <t:..:T> is the exact clock
        # time incl. seconds. Both update on the reader's client without us re-editing.
        lines.append(f"Detected: <t:{ts}:R> (<t:{ts}:T>)")

    # The headline item duplicates the Sales list below, which already names it with a relative
    # time — so it is only worth a line when there is no Sales list to carry it (a hospital
    # re-post, where the window has been claimed). Same for the gear stats: they identify WHICH
    # weapon was listed, which matters when matching a listing and not at all when mugging cash.
    item_name = event.get("item_name")
    if item_name and not (event.get("sales") or []):
        stats = event.get("item_stats") or {}
        bits = []
        if stats.get("damage") is not None:
            bits.append(f"Damage: {stats['damage']:g}")
        if stats.get("accuracy") is not None:
            bits.append(f"Accuracy: {stats['accuracy']:g}")
        if stats.get("armor") is not None:
            bits.append(f"Armor: {stats['armor']:g}")
        statstr = f" ({' | '.join(bits)})" if bits else ""
        if event.get("item_id"):
            url = config.ITEM_MARKET_URL.format(item_id=event["item_id"])
            lines.append(f"Item: [{item_name}]({url}){statstr}")
        else:
            lines.append(f"Item: **{item_name}**{statstr}")

    owner = f"**{name}**"
    tags = []
    if event.get("was_mugged"):
        tags.append("Was Mugged")
    if event.get("in_clothing_store"):
        tags.append("🧥 Clothing Store")
    if tags:
        owner += f" ({', '.join(tags)})"
    lines.append(f"Owner: {owner}")

    # Hospital countdown / travel location context.
    state = event.get("status_state")
    until = event.get("status_until")
    if variant == "hospital" or state == "Hospital":
        lines.append(f"🏥 Out of hospital: {_relative(until)}" if until else "🏥 In hospital")
    elif variant == "overseas" or state in ("Traveling", "Abroad"):
        lines.append(f"✈️ {event.get('status_description') or state}")

    if event.get("money_on_hand") is not None:
        lines.append(f"Money on hand: **{fmt_money(event['money_on_hand'])}**")
    else:
        n = event.get("num_sales")
        suffix = f" ({n} sale{'s' if n != 1 else ''})" if n else ""
        # "Expected value" read as the sale price, which sits three lines below at a DIFFERENT
        # number — this one is after the banking drop and mug protection. Naming it for what it
        # estimates removes the apparent contradiction. The Sales list below is filtered to match:
        # it shows only the sales counted here, never one whose cash is already banked.
        lines.append(f"Expected cash on hand: **{fmt_money(event.get('value_change'))}**{suffix}")

    lines.append(
        f"Mug range: **{fmt_money(event.get('mug_min'))} – {fmt_money(event.get('mug_max'))}**"
    )
    if event.get("protection_pct"):
        lines.append(f"🛡️ Mug protection: value reduced ~{event['protection_pct']}% (recently mugged)")

    lines.append(f"Stats: **{fmt_compact(event['tbs'])}**" if event.get("tbs") is not None
                 else "Stats: *unknown*")

    # Always emitted, even when unknown. A line that silently disappears reads as "nothing to
    # report" rather than "we could not find out", and last action is one of the two things that
    # decide whether a target is worth walking to.
    la = event.get("last_action_ts")
    lines.append(f"Last action: {_relative(la)} {_status_dot(event)}" if la
                 else f"Last action: *unknown* {_status_dot(event)}")

    if hospitalize:
        fac = event.get("faction") or f"faction {event.get('faction_id')}"
        lines.append(f"⚠️ **In flagged faction — {fac}. Hospitalize them, don't mug.**")

    dl = event.get("delist_active_secs")
    if dl is not None:
        active = f"{dl}s ago" if dl < 60 else _relative(int(datetime.now(timezone.utc).timestamp()) - dl)
        lines.append(f"⚠️ Likely de-list (seller active {active})")

    # Seller INFERRED from an auction-house win rather than captured by the item-matcher. Stated
    # with its evidence — age, price, and the measured probability it is right — because acting on
    # an inference and acting on an observation are different decisions and must not look alike.
    if event.get("attribution") == "auction":
        m = event.get("anon_match") or {}
        # The buyer IS the owner named above, so repeating the name is clutter — EXCEPT when the
        # two disagree, which is worth seeing: it means the profile and the auction house name
        # different players, and that is a reason to distrust the inference rather than a cosmetic
        # detail. (A rename does it innocently; so does a wrong match.)
        ah_name = m.get("buyer_name")
        who = f"{ah_name} " if ah_name and ah_name != event.get("seller_name") else ""
        price = f" for ${m['price']:,}" if m.get("price") else ""
        # Age is recomputed from the win timestamp rather than read from `age_days`, which is the
        # age at MATCH time. The two drift apart by however long the listing sat on the market, and
        # the number on screen should be how old the evidence is now.
        when = _auction_age(m)
        # Confidence is never omitted. Dropping it when it is low or missing hides exactly the
        # cases where you most need to know how much to trust the line.
        conf = m.get("confidence")
        conf_txt = (f" · ~{conf * 100:.0f}% likely correct" if conf
                    else " · confidence unknown")
        lines.append(f"🕵️ **Anon match** — {who}won it{when}{price}{conf_txt}")

    if event.get("faction"):
        lines.append(f"Faction: {event['faction']}")

    sales = event.get("sales") or []
    if sales:
        lines.append("")
        lines.append("**Sales:**")
        for s in sales[:10]:
            when = f"{_relative(s['ts'])} " if s.get("ts") else ""
            # Linked here so dropping the Item line above costs nothing — that line's one piece of
            # unique content was the jump to the market.
            label = s.get("item_name", "?")
            if s.get("item_id"):
                label = f"[{label}]({config.ITEM_MARKET_URL.format(item_id=s['item_id'])})"
            lines.append(f"• {when}{s.get('qty', 1)} × {label} — {fmt_money(s.get('price'))}")

    if uid:
        attack = config.ATTACK_URL.format(user_id=uid)
        profile = config.PROFILE_URL.format(user_id=uid)
        lines.append("")
        lines.append(f"**[Attack!]({attack})**  |  **[Profile!]({profile})**")

    return discord.Embed(title=title, description="\n".join(lines), color=color)


# ---------------------------------- status tracker ------------------------------------

# Watch list is split into one message per category (each gets its own 6000-char Discord budget,
# so we keep the full verbose format). Order + display titles:
_WATCH_BUCKETS = [
    ("Primary",   "🔫 Primary Weapons"),
    ("Secondary", "🔫 Secondary Weapons"),
    ("Melee",     "🗡️ Melee Weapons"),
    ("Defensive", "🛡️ Armour"),
    ("Items",     "📦 Items"),
    ("Other",     "❔ Uncategorized (pending first poll)"),
]


def _watch_bucket(w: Dict) -> str:
    """Which category list an item belongs in. Gear's category (Primary/Secondary/Melee/Defensive)
    comes from the Torn API and is stored on poll; stackables are always 'Items'; gear we haven't
    polled yet (no category) sits in 'Other' until its first poll fills it in."""
    if w.get("is_gear") is False:
        return "Items"
    cat = w.get("category")
    return cat if cat in ("Primary", "Secondary", "Melee", "Defensive") else "Other"


def _watch_row(w: Dict) -> str:
    # No per-item type emoji (the message is already per-category) and no "polled X ago" (that
    # timestamp changed every poll, forcing a needless re-edit of every category message). Just the
    # enabled mark + name; the footer's "Last update" shows freshness.
    mark = "✅" if w.get("enabled") else "❌"
    return (f"{mark} **{w['label']}** (`{w['item_id']}`) — "
            f"{w.get('captured', 0)} sellers · {w.get('tracked', 0)} tracked")


# One message per category, holding however many embeds are needed. Discord caps a MESSAGE at 6000
# and an EMBED description at 4096 — both in UTF-16 units (_dlen). We budget conservatively (an
# embed's title + footer also count toward the 6000), and truncate the overflow with a /watch list
# pointer rather than overflowing into more messages.
_WATCH_MSG_BUDGET = 5200    # total row text across the message's embeds (leaves room under 6000)
_WATCH_EMBED_BUDGET = 3600  # per-embed description (well under 4096)


def _watch_message(title: str, items: List[Dict], generated_ts) -> List[discord.Embed]:
    """ONE message's embeds for a category: verbose, enabled-first rows budgeted (in Discord UTF-16
    units) under the message cap and split across <4096-unit embeds; overflow truncates to /watch
    list. Never splits or drops a row at an embed boundary."""
    ordered = sorted(items, key=lambda w: (not w.get("enabled"), (w.get("label") or "").lower()))
    enabled = sum(1 for w in items if w.get("enabled"))
    rows = [_watch_row(w) for w in ordered]

    taken, used = [], 0
    for r in rows:
        if used + _dlen(r) + 1 > _WATCH_MSG_BUDGET:
            break
        taken.append(r)
        used += _dlen(r) + 1
    if len(taken) < len(rows):
        note = f"*…and **{len(rows) - len(taken)}** more — `/watch list` for the full set.*"
        while taken and used + _dlen(note) + 1 > _WATCH_MSG_BUDGET:
            used -= _dlen(taken.pop()) + 1
        taken.append(note)

    # Split the chosen rows into embeds each safely under the 4096 description cap.
    pages, cur, eused = [], [], 0
    for r in taken or ["—"]:
        if cur and eused + _dlen(r) + 1 > _WATCH_EMBED_BUDGET:
            pages.append(cur)
            cur, eused = [], 0
        cur.append(r)
        eused += _dlen(r) + 1
    pages.append(cur)

    embeds = []
    for i, page in enumerate(pages):
        head = f"{title} — {enabled}/{len(items)}" + (" (cont.)" if i else "")
        e = discord.Embed(title=head, description="\n".join(page),
                          color=COLOR_STATUS_OK if enabled else COLOR_STATUS_OFF)
        e.set_footer(text=_updated_footer(generated_ts))
        embeds.append(e)
    return embeds


def build_status_groups(status: Dict) -> List:
    """The status panel as ordered (message_key, [embeds]) groups — exactly one message each:
    Cache Status → Primary → Secondary → Melee → Armour → Items → (Uncategorized) → Unmatched."""
    groups = [("status_tracker", [_cache_embed(status)])]
    buckets: Dict[str, List] = {}
    for w in status.get("watchlist", []):
        buckets.setdefault(_watch_bucket(w), []).append(w)
    gen = status.get("generated_ts")
    for cat, title in _WATCH_BUCKETS:
        if buckets.get(cat):
            groups.append((f"watch_{cat}", _watch_message(title, buckets[cat], gen)))
    groups.append(("status_unmatched", [_unmatched_embed(status)]))
    return groups


def build_status_embeds(status: Dict) -> List[discord.Embed]:
    """Flattened embeds (used by the preview/test harness); the live bot uses build_status_groups
    to spread the watch list across separate messages."""
    return [e for _, embeds in build_status_groups(status) for e in embeds]


def _cache_embed(status: Dict) -> discord.Embed:
    counts = status.get("counts", {})
    est = status.get("api_estimate") or {}
    lines = [
        "**Captured sellers:**",
        f"• Item listings: **{counts.get('regular', 0):,}**",
        f"• Gear listings: **{counts.get('gear', 0):,}**",
        f"• Unmatched gear (need a seller): **{counts.get('unmatched', 0):,}**",
        f"• Items needing a view: **{counts.get('unmatched_items', 0):,}**",
        f"• Anonymous (ignored): **{counts.get('anon', 0):,}**",
    ]
    if est:
        lines.append(
            f"\n📡 ~**{est.get('calls_per_min', 0)}** API calls/min "
            f"({est.get('calls_per_poll', 0)}/poll · {est.get('enabled', 0)} enabled)"
        )
    cache = discord.Embed(title="Mug Bot Cache Status", description="\n".join(lines), color=COLOR_DEAL)
    cache.set_footer(text=_updated_footer(status.get("generated_ts")))
    return cache


def _unmatched_embed(status: Dict) -> discord.Embed:
    counts = status.get("counts", {})
    # listings missing a seller. Gear shows per-uid (highlightable); items just list
    # the name (view the item on the market to capture all its sellers).
    total_gear = counts.get("unmatched", 0)
    total_items = counts.get("unmatched_items", 0)
    gear_lines = []
    for u in status.get("unmatched", []):
        bits = []
        if u.get("stat") is not None:
            bits.append(f"{u['stat']:g}")
        if u.get("accuracy"):
            bits.append(f"acc {u['accuracy']:g}")
        statstr = (" " + "/".join(bits)) if bits else ""
        gear_lines.append(f"• **{u.get('item_name') or '?'}**{statstr} — {fmt_money(u.get('price'))}")

    parts = []
    if total_gear:
        body = "\n".join(gear_lines)
        more = total_gear - len(gear_lines)
        if more > 0:
            body += f"\n…and **{more:,}** more."
        parts.append(f"**Gear ({total_gear:,}):**\n{body}")
    items = status.get("unmatched_items", [])
    if items:
        def _item_label(i):
            name = i.get("item_name") or f"Item {i.get('item_id')}"
            miss = i.get("missing")
            return f"{name} ({miss} left)" if isinstance(miss, int) else name
        names = ", ".join(_item_label(i) for i in items[:60])
        parts.append(f"**Items to view & capture ({total_items:,}):** {names}")
    udesc = "\n\n".join(parts) or "*Nothing unmatched — every discovered listing has a seller.*"
    while _dlen(udesc) > 3800:
        udesc = udesc[:int(len(udesc) * 0.95)]
    if udesc != "\n\n".join(parts) and parts:
        udesc += "\n…"

    unmatched_embed = discord.Embed(
        title=f"Unmatched — {total_gear:,} gear · {total_items:,} items need sellers",
        description=udesc,
        color=COLOR_STATUS_OFF if (total_gear + total_items) == 0 else 0xF1C40F,
    )
    unmatched_embed.set_footer(text=_updated_footer(status.get("generated_ts")))
    return unmatched_embed


# ----------------------------------- item deals ---------------------------------------

def build_deal_embed(deal: Dict) -> discord.Embed:
    """One under-market deal embed for the `items` channel.

    Expected:
      item_name (req), item_id, category, market_value, discount_pct, detected_ts
      unit_price   price per item (falls back to listed_price)
      quantity     how many units are in the listing
      total        total cost of the whole listing (falls back to unit_price * quantity)
    """
    name = deal.get("item_name", "?")
    unit = deal.get("unit_price", deal.get("listed_price"))
    qty = deal.get("quantity")
    total = deal.get("total")
    if total is None and unit is not None and qty:
        total = unit * qty

    title = f"🛒 Deal: {name}"
    if deal.get("discount_pct") is not None:
        title += f" — {deal['discount_pct']:.0f}% under market"

    lines = [
        f"Price per item: **{fmt_money(unit)}**",
        f"Quantity: **{qty if qty is not None else '?'}**",
        f"Total cost: **{fmt_money(total)}**",
        f"Market value: {fmt_money(deal.get('market_value'))} each",
    ]
    if deal.get("detected_ts"):
        lines.append(f"Detected: {_relative(deal['detected_ts'])}")
    if deal.get("item_id"):
        url = _deal_market_url(deal)
        lines.append("")
        lines.append(f"**[Buy on Market!]({url})**")

    return discord.Embed(title=title, description="\n".join(lines), color=COLOR_DEAL)


def _deal_market_url(deal: Dict) -> str:
    """Direct item-market link. If we know the category, use the category view (closest thing to
    a direct purchase page); otherwise the item-id search link."""
    item_id = deal.get("item_id")
    category = deal.get("category")
    if category:
        from urllib.parse import quote
        return (
            "https://www.torn.com/page.php?sid=ItemMarket#/market/"
            f"view=category&categoryName={quote(str(category))}&itemID={item_id}"
        )
    return config.ITEM_MARKET_URL.format(item_id=item_id)
