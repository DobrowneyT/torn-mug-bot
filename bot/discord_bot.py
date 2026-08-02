"""
The live Discord client (distinct from scratch/format_preview.py).

Exposes coroutine methods the server calls when something happens:
  - post_mug_alert(event)  -> new embed in `logs`
  - post_deal(deal)        -> new embed in `items`
  - update_status(status)  -> edit the single status-tracker message in place

Status-tracker is edited in place (message id persisted in bot/state.json) so it stays a
single live panel rather than spamming. Mirrors the message-reuse pattern from
discord/bot.py / discord/scratch/format_preview.py.
"""

import asyncio
import logging
import time
from typing import Dict, Optional

import discord
import httpx
from discord import app_commands
from discord.ext import tasks

import config
from bot import formatter, state
from server import db, detection, item_names
from server import keys as keypool

log = logging.getLogger("mug.bot")

_STATUS_KEY = "status_tracker"


def _parse_amount(text: Optional[str]) -> Optional[int]:
    """Parse '5m' / '30M' / '1.5b' / '5000000' / '$5,000,000' -> int. None if unparseable."""
    text = (text or "").strip().lower().replace(",", "").replace("$", "")
    if not text:
        return None
    mult = 1
    if text[-1] in "kmb":
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[text[-1]]
        text = text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        return None


def _split_items(text: str):
    return [t.strip() for t in (text or "").replace("\n", ",").split(",") if t.strip()]


def _role_ping(role_id: int) -> Optional[str]:
    """Discord role-mention string (`<@&id>`) to put at the top of an alert, or None if unset."""
    return f"<@&{role_id}>" if role_id else None


# Category keywords usable anywhere an item list is accepted (add/remove/enable/disable).
_CATEGORY_KEYWORDS = {
    "all": "ALL", "gear": "GEAR", "weapons": "GEAR",
    "items": "Items", "item": "Items", "consumables": "Items", "consumable": "Items",
    "primary": "Primary", "secondary": "Secondary", "melee": "Melee",
    "armour": "Defensive", "armor": "Defensive", "defensive": "Defensive",
    "defense": "Defensive", "defence": "Defensive",
}
# Gear category keywords (for the "capture all bonuses" override) -> canonical Torn type.
_GEAR_ALLBONUS_CATS = {
    "primary": "Primary", "secondary": "Secondary", "melee": "Melee",
    "armour": "Defensive", "armor": "Defensive", "defensive": "Defensive",
}
# Selectable autocomplete shortcuts (value, label).
_WATCH_SHORTCUTS = [
    ("all", "all (every mapped item)"),
    ("gear", "gear (all weapons + armour)"),
    ("items", "items (all consumables)"),
    ("primary", "primary weapons"),
    ("secondary", "secondary weapons"),
    ("melee", "melee weapons"),
    ("armour", "armour (defensive)"),
]


async def _catalog_for_category(cat: str):
    """(item_id, label) pairs in the mapped catalog for a canonical category. Gear sub-categories
    (Primary/Secondary/Melee/Defensive) are resolved from the Torn `type` stored in item_values
    (populated by the items fetch), intersected with our gear catalog."""
    if cat == "ALL":
        return list(item_names.CATALOG)
    if cat == "GEAR":
        return [(i, item_names.name_for(i)) for i in sorted(item_names.GEAR_IDS)]
    if cat == "Items":
        return [(i, item_names.name_for(i)) for i in sorted(item_names.CONSUMABLE_IDS)]
    ids = await db.item_ids_by_type([cat]) & item_names.GEAR_IDS
    return [(i, item_names.name_for(i)) for i in sorted(ids)]


def _watch_result(verb: str, ok, failed) -> str:
    parts = []
    if ok:
        parts.append(f"✅ {verb}: {', '.join(ok)}.")
    if failed:
        parts.append(f"⚠️ Couldn't resolve: {', '.join(failed)}.")
    return "\n".join(parts) or "Nothing to do."


class MugBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._status_task: Optional[asyncio.Task] = None
        self._status_cleaned = False
        self._last_status_edit = 0.0   # monotonic ts of the last actual Discord edit (throttle)
        self._status_hashes: Dict[str, str] = {}   # per-message content signature; skip edit if unchanged

    def request_status_refresh(self) -> None:
        """Debounced status-panel edit — call on each capture; applies a few seconds after the last
        one, and never more often than STATUS_MIN_EDIT_INTERVAL (Discord PATCH rate limit)."""
        if self._status_task and not self._status_task.done():
            self._status_task.cancel()
        self._status_task = asyncio.create_task(self._debounced_status())

    async def _debounced_status(self) -> None:
        try:
            await asyncio.sleep(config.STATUS_DEBOUNCE_SECONDS)
            # Throttle: don't edit the message sooner than MIN_EDIT_INTERVAL since the last edit
            # (the 30s loop also edits and updates _last_status_edit), so heavy marking can't
            # machine-gun PATCHes into a 429.
            wait = config.STATUS_MIN_EDIT_INTERVAL - (time.monotonic() - self._last_status_edit)
            if wait > 0:
                await asyncio.sleep(wait)
            # Rapid path (during marking): refresh only the lightweight dashboard so we don't
            # machine-gun edits across every category message. The 30s loop does the full refresh.
            await self.update_status(await db.status_snapshot(), full=False)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("debounced status error")

    async def setup_hook(self) -> None:
        self._register_commands()
        try:
            if config.GUILD_ID:
                guild = discord.Object(id=config.GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
            log.info("slash commands synced (%s)", "guild" if config.GUILD_ID else "global")
        except Exception:
            log.exception("slash command sync failed")
        self._refresh_loop.start()

    def _register_commands(self) -> None:
        @self.tree.command(name="mugmin", description="Show or set the minimum mug value to alert on")
        @app_commands.describe(value="e.g. 5m, 30m, 5000000 — omit to show the current value")
        async def mugmin(interaction: discord.Interaction, value: Optional[str] = None):
            current = await db.get_config("min_mug_value", config.MIN_MUG_VALUE)
            if value is None:
                await interaction.response.send_message(
                    f"Current minimum mug value: **${int(current):,}**", ephemeral=True
                )
                return
            parsed = _parse_amount(value)
            if parsed is None or parsed < 0:
                await interaction.response.send_message(
                    f"Couldn't parse `{value}`. Try `5m`, `30m`, or `5000000`.", ephemeral=True
                )
                return
            await db.set_config("min_mug_value", parsed)
            await interaction.response.send_message(
                f"Minimum mug value set to **${parsed:,}**.", ephemeral=True
            )

        # ----------------------------- /watch -----------------------------
        watch = app_commands.Group(name="watch", description="Manage which items the poller watches")

        def _ac_choices(current: str, source):
            # Comma-aware: autocomplete the segment after the last comma and prepend the rest,
            # so picking choices builds up a multi-item list.
            prefix, _, last = current.rpartition(",")
            frag, base = last.strip().lower(), (prefix.strip() + ", " if prefix.strip() else "")
            out = []
            for iid, label in source:
                if frag in label.lower() or frag in str(iid):
                    value = f"{base}{iid}"
                    if len(value) <= 100:
                        out.append(app_commands.Choice(name=f"{label} ({iid})", value=value))
                    if len(out) >= 25:
                        break
            return out

        def _with_shortcuts(choices, current):
            # Offer the category shortcuts (all/gear/items/primary/…) that match what's being typed.
            frag = current.rpartition(",")[2].strip().lower()
            pre = [app_commands.Choice(name=label, value=val)
                   for val, label in _WATCH_SHORTCUTS if not frag or val.startswith(frag)]
            return (pre + choices)[:25]

        async def _ac_catalog(interaction, current: str):
            return _with_shortcuts(_ac_choices(current, item_names.CATALOG), current)

        async def _ac_watched(interaction, current: str):
            choices = _ac_choices(current, [(w["item_id"], w["label"]) for w in await db.watchlist_status()])
            return _with_shortcuts(choices, current)

        @watch.command(
            name="add",
            description="Watch items — names/ids, or a category (all, items, primary, secondary, melee, armour)")
        @app_commands.describe(items="Item name(s)/id(s) or category keyword(s), comma-separated")
        @app_commands.autocomplete(items=_ac_catalog)
        async def watch_add(interaction: discord.Interaction, items: str):
            add_map, failed, pending = {}, [], []
            for token in _split_items(items):
                cat = _CATEGORY_KEYWORDS.get(token.lower())
                if cat:
                    pairs = await _catalog_for_category(cat)
                    if not pairs and cat in ("Primary", "Secondary", "Melee", "Defensive"):
                        pending.append(token)
                    add_map.update(pairs)
                else:
                    iid, label = item_names.resolve(token)
                    if iid is None:
                        failed.append(token)
                    else:
                        add_map[iid] = label
            if add_map:
                await db.watchlist_add_all(
                    [(iid, label, item_names.is_gear_hint(iid)) for iid, label in add_map.items()])
            self.request_status_refresh()
            msg = f"➕ Now watching **{len(add_map)}** item(s)."
            if failed:
                msg += f"\n⚠️ Couldn't resolve: {', '.join(failed)}."
            if pending:
                msg += (f"\n⏳ Category data not loaded yet for {', '.join(pending)} — "
                        "the items fetch runs shortly after start; try again in ~30s.")
            await interaction.response.send_message(msg, ephemeral=True)

        @watch.command(
            name="remove",
            description="Stop watching items — names/ids, or a category (all, items, primary, …)")
        @app_commands.autocomplete(items=_ac_watched)
        async def watch_remove(interaction: discord.Interaction, items: str):
            total, ok, failed = 0, [], []
            for token in _split_items(items):
                cat = _CATEGORY_KEYWORDS.get(token.lower())
                if cat:
                    n = await db.watchlist_remove_by_category(cat)
                    total += n
                    ok.append(f"{token} ({n})")
                else:
                    iid, _ = item_names.resolve(token)
                    if iid is None:
                        failed.append(token)
                    elif await db.watchlist_remove(iid):
                        ok.append(item_names.name_for(iid))
                        total += 1
                    else:
                        failed.append(item_names.name_for(iid))
            self.request_status_refresh()
            await interaction.response.send_message(_watch_result("Removed", ok, failed), ephemeral=True)

        async def _set_many(interaction, items, enabled, verb):
            ok, failed = [], []
            for token in _split_items(items):
                cat = _CATEGORY_KEYWORDS.get(token.lower())
                if cat:
                    n = await db.watchlist_set_enabled_by_category(cat, enabled)
                    ok.append(f"{token} ({n})")
                else:
                    iid, _ = item_names.resolve(token)
                    if iid is None:
                        failed.append(token)
                    elif await db.watchlist_set_enabled(iid, enabled):
                        ok.append(item_names.name_for(iid))
                    else:
                        failed.append(item_names.name_for(iid))
            self.request_status_refresh()
            await interaction.response.send_message(_watch_result(verb, ok, failed), ephemeral=True)

        @watch.command(name="enable", description="Enable polling — names/ids, or a category (all, items, primary, …)")
        @app_commands.autocomplete(items=_ac_watched)
        async def watch_enable(interaction: discord.Interaction, items: str):
            await _set_many(interaction, items, True, "Enabled")

        @watch.command(name="disable", description="Pause polling — names/ids, or a category (all, items, primary, …)")
        @app_commands.autocomplete(items=_ac_watched)
        async def watch_disable(interaction: discord.Interaction, items: str):
            await _set_many(interaction, items, False, "Disabled")

        @watch.command(name="clear", description="Clear the entire 'uncached' (to-view) list — gear + items")
        async def watch_clear(interaction: discord.Interaction):
            res = await db.clear_uncached()
            self.request_status_refresh()
            await interaction.response.send_message(
                f"🧹 Cleared the uncached list — {res['gear']} gear listing(s) removed, "
                f"{res['items']} item count(s) reset. Anything still on the market without a "
                "captured seller reappears on the next poll (so if these come back, they're real, "
                "not stale).",
                ephemeral=True,
            )

        async def _ac_categories(interaction, current: str):
            frag = current.strip().lower()
            return [app_commands.Choice(name=label, value=val)
                    for val, label in _WATCH_SHORTCUTS if not frag or val.startswith(frag)][:25]

        def _in_category(w, cat):
            if cat == "ALL":
                return True
            if cat == "GEAR":
                return w["is_gear"] is True
            if cat == "Items":
                return w["is_gear"] is False
            return w.get("category") == cat   # Primary/Secondary/Melee/Defensive

        @watch.command(name="list", description="Show the watch list — optionally one category")
        @app_commands.describe(category="all (default), gear, items, primary, secondary, melee, armour")
        @app_commands.autocomplete(category=_ac_categories)
        async def watch_list(interaction: discord.Interaction, category: str = "all"):
            cat = _CATEGORY_KEYWORDS.get((category or "all").lower(), "ALL")
            rows = [w for w in await db.watchlist_status() if _in_category(w, cat)]
            if not rows:
                await interaction.response.send_message(
                    f"Nothing watched{'' if cat == 'ALL' else f' in {category}'}.", ephemeral=True)
                return
            enabled = sum(1 for w in rows if w["enabled"])
            header = f"**Watch list{'' if cat == 'ALL' else f' — {category}'}** — {enabled}/{len(rows)}"
            lines = [
                f"{'✅' if w['enabled'] else '❌'} **{w['label']}** "
                f"(`{w['item_id']}`) — {w['captured']}s {w['tracked']}t"
                for w in rows
            ]
            await _send_chunked(interaction, header, lines)

        async def _send_chunked(interaction, header, lines):
            # Discord message content caps at 2000 chars; pack lines into <=2000-char chunks and
            # send the first as the response, the rest as follow-ups (all ephemeral).
            chunks, cur = [], header
            for ln in lines:
                if len(cur) + 1 + len(ln) > 1950:
                    chunks.append(cur)
                    cur = ln
                else:
                    cur += "\n" + ln
            chunks.append(cur)
            await interaction.response.send_message(chunks[0], ephemeral=True)
            for c in chunks[1:]:
                await interaction.followup.send(c, ephemeral=True)

        self.tree.add_command(watch)

        # ----------------------------- /keys ------------------------------
        keys_group = app_commands.Group(name="keys", description="Manage Torn API keys")

        def _mask(k):
            return f"…{k[-4:]}" if k else "?"

        async def _ac_keys(interaction, current: str):
            cur = current.lower()
            out = []
            for r in await db.all_api_keys():
                label = f"{r.get('name') or '?'} ({r.get('user_id') or '?'})"
                if cur in label.lower() or cur in str(r.get("user_id") or ""):
                    out.append(app_commands.Choice(name=label, value=str(r.get("user_id") or r["key"])))
                    if len(out) >= 25:
                        break
            return out

        @keys_group.command(name="add", description="Add a Torn API key (validated on add)")
        @app_commands.describe(key="Your Torn API key")
        async def keys_add(interaction: discord.Interaction, key: str):
            await interaction.response.defer(ephemeral=True)
            async with httpx.AsyncClient() as client:
                res = await keypool.add_key(client, key.strip())
            if res.get("ok"):
                await interaction.followup.send(
                    f"✅ Added key for **{res.get('name')}** (`{res.get('user_id')}`, level {res.get('level')}). "
                    f"{keypool.active_count()} active.", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Rejected: {res.get('reason')}", ephemeral=True)

        @keys_group.command(name="remove", description="Remove an API key (by owner name or id)")
        @app_commands.autocomplete(key=_ac_keys)
        async def keys_remove(interaction: discord.Interaction, key: str):
            n = await keypool.remove_key(key.strip())
            await interaction.response.send_message(
                (f"Removed {n} key(s). " if n else "No matching key. ") + f"{keypool.active_count()} active.",
                ephemeral=True)

        @keys_group.command(name="enable", description="Re-enable disabled key(s) — one by name/id, or all if blank")
        @app_commands.autocomplete(key=_ac_keys)
        async def keys_enable(interaction: discord.Interaction, key: Optional[str] = None):
            n = await keypool.enable_keys(key.strip() if key else None)
            await interaction.response.send_message(
                (f"♻️ Re-enabled {n} key(s). " if n else "No disabled keys matched. ")
                + f"{keypool.active_count()} active.", ephemeral=True)

        @keys_group.command(name="list", description="Show API keys + health")
        async def keys_list(interaction: discord.Interaction):
            rows = await db.all_api_keys()
            if not rows:
                await interaction.response.send_message("No API keys. `/keys add <key>`.", ephemeral=True)
                return
            lines = []
            for r in rows:
                mark = "🟢" if r["enabled"] else "🔴"
                owner = f"{r.get('name') or '?'} ({r.get('user_id') or '?'})"
                err = f" — ⚠️ {r['last_error']}" if r.get("last_error") else ""
                # Remaining request budget makes pool sizing visible: keys sitting near zero mean
                # the watch list is bigger than the pool can serve.
                budget = (f" · {keypool.budget_remaining(r['key'])}/{config.KEY_BUDGET_PER_MIN} left"
                          if r["enabled"] and config.KEY_BUDGET_PER_MIN > 0 else "")
                lines.append(f"{mark} {owner} `{_mask(r['key'])}`{budget}{err}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        self.tree.add_command(keys_group)

        # ----------------------------- /faction ---------------------------
        # Sellers in these factions are flagged HOSPITALIZE (don't mug) on the alert.
        faction_group = app_commands.Group(
            name="faction", description="Factions to hospitalize instead of mug")

        @faction_group.command(name="list", description="Show the hospitalize-faction list")
        async def faction_list(interaction: discord.Interaction):
            ids = await db.get_hospitalize_factions()
            txt = ", ".join(f"`{i}`" for i in ids) if ids else "*(none)*"
            await interaction.response.send_message(f"🏥 Hospitalize factions: {txt}", ephemeral=True)

        @faction_group.command(name="add", description="Add faction id(s) to hospitalize (comma-separated)")
        @app_commands.describe(faction_ids="Faction ID(s), comma-separated")
        async def faction_add(interaction: discord.Interaction, faction_ids: str):
            ids, bad = [], []
            for t in _split_items(faction_ids):
                (ids if t.isdigit() else bad).append(t)
            for i in ids:
                latest = await db.add_hospitalize_faction(int(i))
            latest = await db.get_hospitalize_factions()
            msg = f"🏥 Hospitalize factions now: {', '.join(f'`{i}`' for i in latest) or '*(none)*'}"
            if bad:
                msg += f"\n⚠️ Not numeric: {', '.join(bad)}"
            await interaction.response.send_message(msg, ephemeral=True)

        @faction_group.command(name="remove", description="Remove a faction id from the hospitalize list")
        @app_commands.describe(faction_id="Faction ID to remove")
        async def faction_remove(interaction: discord.Interaction, faction_id: str):
            if not faction_id.strip().isdigit():
                await interaction.response.send_message("Faction id must be numeric.", ephemeral=True)
                return
            latest = await db.remove_hospitalize_faction(int(faction_id.strip()))
            await interaction.response.send_message(
                f"🏥 Hospitalize factions now: {', '.join(f'`{i}`' for i in latest) or '*(none)*'}",
                ephemeral=True)

        self.tree.add_command(faction_group)

        # ----------------------------- /gearbonus -------------------------
        # Gear to poll with NO bonus filter (capture EVERY listing, not just bonus ones).
        gearbonus = app_commands.Group(
            name="gearbonus", description="Gear to capture ALL listings for (no bonus filter)")

        def _canon_allbonus(token: str):
            """(stored-token, display-label) for a category keyword or item name/id, else (None, None)."""
            cat = _GEAR_ALLBONUS_CATS.get(token.lower())
            if cat:
                return cat, cat
            iid, label = item_names.resolve(token)
            return (str(iid), label or str(iid)) if iid is not None else (None, None)

        async def _ac_gearcats(interaction, current: str):
            frag = current.strip().lower()
            cats = [("primary", "primary"), ("secondary", "secondary"),
                    ("melee", "melee"), ("armour", "armour (defensive)")]
            return [app_commands.Choice(name=lbl, value=val)
                    for val, lbl in cats if not frag or val.startswith(frag)][:25]

        @gearbonus.command(name="list", description="Show gear captured with no bonus filter")
        async def gearbonus_list(interaction: discord.Interaction):
            toks = await db.get_gear_all_bonus()
            await interaction.response.send_message(
                "🎯 Capturing ALL listings (no bonus filter) for: "
                + (", ".join(f"`{t}`" for t in toks) or "*(none — all gear uses the bonus filter)*"),
                ephemeral=True)

        @gearbonus.command(
            name="add",
            description="Capture ALL listings for a category (primary/secondary/melee/armour) or item")
        @app_commands.describe(targets="Category keyword(s) and/or item name(s)/id(s), comma-separated")
        @app_commands.autocomplete(targets=_ac_gearcats)
        async def gearbonus_add(interaction: discord.Interaction, targets: str):
            ok, bad = [], []
            for t in _split_items(targets):
                canon, label = _canon_allbonus(t)
                if canon is None:
                    bad.append(t)
                    continue
                await db.add_gear_all_bonus(canon)
                ok.append(label)
            msg = "🎯 Now capturing ALL listings for: " + (", ".join(f"`{x}`" for x in ok) or "(nothing)")
            if bad:
                msg += f"\n⚠️ Unrecognized: {', '.join(bad)}"
            msg += "\n*(category overrides apply on the next poll once the type is known)*"
            await interaction.response.send_message(msg, ephemeral=True)

        @gearbonus.command(name="remove", description="Revert a category/item to bonus-only polling")
        @app_commands.autocomplete(targets=_ac_gearcats)
        async def gearbonus_remove(interaction: discord.Interaction, targets: str):
            ok, bad = [], []
            for t in _split_items(targets):
                canon, label = _canon_allbonus(t)
                if canon is None:
                    bad.append(t)
                    continue
                await db.remove_gear_all_bonus(canon)
                ok.append(label)
            msg = "↩️ Reverted to bonus-only for: " + (", ".join(f"`{x}`" for x in ok) or "(nothing)")
            if bad:
                msg += f"\n⚠️ Unrecognized: {', '.join(bad)}"
            await interaction.response.send_message(msg, ephemeral=True)

        self.tree.add_command(gearbonus)

        # ----------------------------- /delist ----------------------------
        # De-list handling for GEAR vanishes (a seller pulling their own listing looks identical to
        # a sale). One command, all params optional — set the detection buffer and/or the mode.
        @self.tree.command(
            name="delist",
            description="Configure likely-de-list handling (buffer + ping/silent/off)")
        @app_commands.describe(
            seconds="Flag a gear vanish as a likely de-list if the seller was online and acted within "
                    "this many seconds (0 disables the flag)",
            mode="What to do with likely de-lists")
        @app_commands.choices(mode=[
            app_commands.Choice(name="ping — post + ping the mug role (default)", value="ping"),
            app_commands.Choice(name="silent — post but DON'T ping the role", value="silent"),
            app_commands.Choice(name="off — drop it (no alert, removed from the value window)", value="off"),
        ])
        async def delist(interaction: discord.Interaction,
                         seconds: Optional[int] = None,
                         mode: Optional[app_commands.Choice[str]] = None):
            changed = []
            if seconds is not None:
                if seconds < 0:
                    await interaction.response.send_message("Seconds must be ≥ 0.", ephemeral=True)
                    return
                await db.set_config("delist_tag_seconds", seconds)
                changed.append(f"buffer → **{seconds}s**")
            if mode is not None:
                await db.set_config("delist_mode", mode.value)
                changed.append(f"mode → **{mode.value}**")
            cur_secs = await db.get_config("delist_tag_seconds", config.DELIST_TAG_SECONDS)
            cur_mode = await db.get_config("delist_mode", config.DELIST_MODE)
            head = ("✅ " + ", ".join(changed) + "\n") if changed else ""
            await interaction.response.send_message(
                f"{head}🏷️ De-list buffer: **{int(cur_secs)}s** · mode: **{cur_mode}** "
                f"(`ping` = post+ping, `silent` = post no-ping, `off` = drop).",
                ephemeral=True)

        # ----------------------------- /hosplock --------------------------
        # How a sale by an in-hospital seller is alerted. All params optional.
        @self.tree.command(
            name="hosplock",
            description="Configure in-hospital sale alerts (live/remind/hold + lead time)")
        @app_commands.describe(
            mode="When to post a sale by an in-hospital seller",
            lead_minutes="For hold/remind: minutes before release to (re)post")
        @app_commands.choices(mode=[
            app_commands.Choice(name="live — post now with a release countdown (default)", value="live"),
            app_commands.Choice(name="remind — post now AND re-post near release", value="remind"),
            app_commands.Choice(name="hold — wait, post once just before release", value="hold"),
        ])
        async def hosplock(interaction: discord.Interaction,
                           mode: Optional[app_commands.Choice[str]] = None,
                           lead_minutes: Optional[int] = None):
            changed = []
            if mode is not None:
                await db.set_config("hosp_mode", mode.value)
                changed.append(f"mode → **{mode.value}**")
            if lead_minutes is not None:
                if lead_minutes < 0:
                    await interaction.response.send_message("Lead minutes must be ≥ 0.", ephemeral=True)
                    return
                await db.set_config("hosp_lead_seconds", lead_minutes * 60)
                changed.append(f"lead → **{lead_minutes} min**")
            cur_mode = await db.get_config("hosp_mode", config.HOSP_MODE)
            cur_lead = int(await db.get_config("hosp_lead_seconds", config.HOSPITAL_RELEASE_LEAD_SECONDS))
            head = ("✅ " + ", ".join(changed) + "\n") if changed else ""
            await interaction.response.send_message(
                f"{head}🏥 Hospital mode: **{cur_mode}** · lead: **{cur_lead // 60} min** "
                f"(`live` = post now, `remind` = post now + re-post near release, `hold` = post only "
                "near release).",
                ephemeral=True)

        # ----------------------------- /help ------------------------------
        @self.tree.command(name="help", description="List Mug Bot's commands")
        async def help_cmd(interaction: discord.Interaction):
            def _sig(cmd):
                parts = []
                for p in getattr(cmd, "parameters", []):
                    parts.append(f"<{p.name}>" if p.required else f"[{p.name}]")
                return (" " + " ".join(parts)) if parts else ""

            lines = []
            for cmd in sorted(self.tree.get_commands(), key=lambda c: c.name):
                if isinstance(cmd, app_commands.Group):
                    lines.append(f"\n__**/{cmd.name}**__ — {cmd.description}")
                    for sub in sorted(cmd.commands, key=lambda c: c.name):
                        lines.append(f"• `/{cmd.name} {sub.name}{_sig(sub)}` — {sub.description}")
                else:
                    lines.append(f"\n• `/{cmd.name}{_sig(cmd)}` — {cmd.description}")
            lines.append(
                "\n*Tip:* item lists accept names, ids, or a category keyword — "
                "`all`, `gear`, `items`, `primary`, `secondary`, `melee`, `armour` "
                "(comma-separate to combine).")
            lines.append(
                "*De-lists:* `/delist mode:ping|silent|off` — `silent` posts without pinging, `off` "
                "drops likely self-pulls entirely; `/delist seconds:<n>` sets the detection buffer.")
            lines.append(
                "*Hospital:* `/hosplock mode:live|remind|hold` — `live` posts immediately with a "
                "countdown, `remind` also re-posts near release, `hold` waits; `lead_minutes` sets "
                "how early.")
            embed = discord.Embed(
                title="🤖 Mug Bot — Commands",
                description="\n".join(lines)[:4096],
                color=formatter.COLOR_STATUS_OK,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_ready(self) -> None:
        log.info("MugBot connected as %s (id=%s)", self.user, getattr(self.user, "id", "?"))
        # Delete any status/watch messages left over from a previous run (incl. a hard kill that
        # skipped close()), so this run starts with fresh panels. Only once per process.
        if not self._status_cleaned:
            self._status_cleaned = True
            await self._delete_status_messages()

    def _status_keys(self) -> list:
        """All persisted status-panel message keys (dashboard, every watch list, unmatched)."""
        return [k for k in state.all_message_keys() if k.startswith("status_") or k.startswith("watch_")]

    async def _delete_status_messages(self, keep: set = frozenset()) -> None:
        """Delete persisted status/watch messages whose key isn't in `keep`, and forget their ids,
        so the next update_status posts fresh ones. (keep empty => delete them all + reset layout.)"""
        if not keep:
            state.set_status_layout([])
        if not config.STATUS_CHANNEL_ID:
            for key in self._status_keys():
                if key not in keep:
                    state.clear_message_id(key)
            return
        channel = await self._channel(config.STATUS_CHANNEL_ID)
        for key in self._status_keys():
            if key in keep:
                continue
            mid = state.get_message_id(key)
            if mid:
                try:
                    msg = await channel.fetch_message(mid)
                    await msg.delete()
                except discord.NotFound:
                    pass
                except Exception:
                    log.exception("could not delete stale status message %s (%s)", mid, key)
            state.clear_message_id(key)
            self._status_hashes.pop(key, None)

    async def close(self) -> None:
        """On graceful shutdown, delete the live status panels so nothing stale lingers."""
        try:
            await self._delete_status_messages()
        except Exception:
            log.exception("status cleanup on shutdown failed")
        await super().close()

    @tasks.loop(seconds=config.STATUS_REFRESH_SECONDS)
    async def _refresh_loop(self) -> None:
        """Edit the live status panels + post any hospital-release alerts that are due."""
        try:
            await self.update_status(await db.status_snapshot(), full=True)
            await detection.post_hospital_due(self)
            for alert in keypool.drain_alerts():
                await self._post_key_alert(alert)
        except Exception:
            log.exception("refresh loop error")

    @_refresh_loop.before_loop
    async def _before_refresh(self) -> None:
        await self.wait_until_ready()

    async def _channel(self, channel_id: int):
        return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

    async def _post_key_alert(self, alert: Dict) -> None:
        if not config.STATUS_CHANNEL_ID:
            return
        channel = await self._channel(config.STATUS_CHANNEL_ID)
        name = alert.get("name") or "unknown"
        uid = alert.get("user_id") or "?"
        await channel.send(
            f"⚠️ **API key disabled** — {name} (`{uid}`): code {alert.get('code')} "
            f"{alert.get('error')}. **{alert.get('remaining', 0)}** key(s) still active."
        )

    async def post_mug_alert(self, event: Dict) -> None:
        if not config.LOGS_CHANNEL_ID:
            log.warning("post_mug_alert skipped — MUG_LOGS_CHANNEL_ID not set")
            return
        channel = await self._channel(config.LOGS_CHANNEL_ID)
        # silent events (likely de-lists under /delist mode=silent) post the embed but skip the ping.
        silent = bool(event.get("silent"))
        await channel.send(
            content=None if silent else _role_ping(config.LOGS_ROLE_ID),
            embeds=[formatter.build_mug_alert_embed(event)],
            allowed_mentions=discord.AllowedMentions(roles=not silent),
        )

    async def post_deal(self, deal: Dict) -> None:
        if not config.ITEMS_CHANNEL_ID:
            return
        channel = await self._channel(config.ITEMS_CHANNEL_ID)
        await channel.send(
            content=_role_ping(config.ITEMS_ROLE_ID),
            embeds=[formatter.build_deal_embed(deal)],
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

    async def update_status(self, status: Dict, full: bool = True) -> None:
        """Edit the status panels. The watch list is split into one message per category, so a full
        refresh upserts several messages (spaced out for the rate limit) and prunes any category
        that emptied. full=False refreshes only the dashboard (used by the rapid marking path)."""
        if not config.STATUS_CHANNEL_ID:
            return
        channel = await self._channel(config.STATUS_CHANNEL_ID)
        groups = formatter.build_status_groups(status)
        if not full:
            # Rapid path: only the dashboard + unmatched (their counts change while marking); the
            # per-category watch lists wait for the 30s full refresh.
            groups = [g for g in groups if g[0] in (_STATUS_KEY, "status_unmatched")]
        else:
            # If the set/order of panels changed (a category appeared or emptied), Discord would
            # append the new message at the bottom — so wipe & re-post to keep the intended order.
            ordered = [k for k, _ in groups]
            if state.get_status_layout() != ordered:
                await self._delete_status_messages()
                state.set_status_layout(ordered)

        active = set()
        for key, embeds in groups:
            active.add(key)
            edited = await self._upsert_status_message(channel, key, embeds)
            if edited:
                self._last_status_edit = time.monotonic()
                await asyncio.sleep(config.STATUS_EDIT_SPACING)   # space ACTUAL edits for the rate limit

        if full:
            # Delete category messages that no longer have any items.
            await self._delete_status_messages(keep=active)

    @staticmethod
    def _embeds_signature(embeds) -> str:
        # Title + description only (NOT the footer, which carries a per-render timestamp). So a
        # message is re-edited only when its real content changes.
        return "\x1e".join((e.title or "") + "\x1f" + (e.description or "") for e in embeds)

    async def _upsert_status_message(self, channel, key: str, embeds) -> bool:
        """Edit/post the message for `key`; returns True if Discord was actually called. Skips the
        edit when the content is unchanged since last time (avoids needless rate-limited edits)."""
        existing = state.get_message_id(key)
        sig = self._embeds_signature(embeds)
        if existing and self._status_hashes.get(key) == sig:
            return False   # nothing changed — don't touch it
        if existing:
            try:
                msg = await channel.fetch_message(existing)
                await msg.edit(embeds=embeds)
                self._status_hashes[key] = sig
                return True
            except discord.NotFound:
                log.warning("status message %s (%s) gone — sending a new one", existing, key)
        msg = await channel.send(embeds=embeds)
        state.set_message_id(key, msg.id)
        self._status_hashes[key] = sig
        return True
