"""
Mug Bot configuration — thresholds, windows, channel IDs, and Torn URLs.

Defaults here are the boot values; the runtime-tunable ones (mug percentages,
thresholds, windows) are mirrored into the SQLite ``config`` table and can be
edited live via Discord slash commands. Secrets / channel IDs come from the
environment (mug-bot/.env, with ../.secrets/.env as a fallback) — see .env.example.

IMPORTANT: load the .env files (dotenv) *before* importing this module, since the
constants below are evaluated at import time.
"""

import os

# ---- Discord ----
DISCORD_TOKEN = os.environ.get("MUG_BOT_DISCORD_TOKEN")
LOGS_CHANNEL_ID = int(os.environ.get("MUG_LOGS_CHANNEL_ID", "0") or 0)
STATUS_CHANNEL_ID = int(os.environ.get("MUG_STATUS_CHANNEL_ID", "0") or 0)
ITEMS_CHANNEL_ID = int(os.environ.get("MUG_ITEMS_CHANNEL_ID", "0") or 0)
PREVIEW_CHANNEL_ID = int(os.environ.get("MUG_PREVIEW_CHANNEL_ID", "0") or 0)
# Your server's id — set it so slash commands (/mugmin) register instantly in that guild
# (global registration can take up to an hour). Right-click your server icon -> Copy Server ID.
GUILD_ID = int(os.environ.get("MUG_GUILD_ID", "0") or 0)
# Optional role to ping at the top of each outgoing alert. Enable Discord Developer Mode, then
# right-click the role -> Copy Role ID. Leave 0 to not ping. LOGS = mug alerts, ITEMS = deals.
LOGS_ROLE_ID = int(os.environ.get("MUG_LOGS_ROLE_ID", "0") or 0)
ITEMS_ROLE_ID = int(os.environ.get("MUG_ITEMS_ROLE_ID", "0") or 0)

# ---- Server ----
SERVER_HOST = os.environ.get("MUG_SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("MUG_SERVER_PORT", "3001"))
# Data lives OUTSIDE the checkout so a deploy, a re-clone or `git clean` can never touch it.
DB_PATH = os.environ.get("MUG_DB_PATH") or "/var/lib/mug-bot/mug_bot.db"
# LOCAL DEV ONLY. db._resolve_db_path falls back here when DB_PATH's directory isn't writable, and
# logs a warning saying so — on a server that means the database is sitting in the git working tree,
# which is a data-loss risk, not a convenience. Set MUG_DB_PATH there.
DB_PATH_FALLBACK = os.path.join(os.path.dirname(__file__), "mug_bot.db")

# ---- Torn / enrichment API keys ----
TORN_API_KEYS = [k.strip() for k in (os.environ.get("TORN_API_KEYS", "") or "").split(",") if k.strip()]
BSP_API_KEY = os.environ.get("BSP_API_KEY")
# BSP (lol-manager) is a THIRD PARTY on the alert path, and TBS is display-only — it never affects
# whether a mug qualifies or what it's worth. Enrichment fetches profile/job/TBS concurrently, so a
# slow BSP can't add to the others serially, but it still bounds the alert: the gather waits for the
# slowest. A short timeout caps that. Torn's own calls keep their longer 15s.
BSP_TIMEOUT_SECONDS = float(os.environ.get("MUG_BSP_TIMEOUT", "3"))
TORNSTATS_API_KEY = os.environ.get("TORNSTATS_API_KEY")
# The `comment` attached to every Torn API call (visible to key owners in their API log).
API_COMMENT = os.environ.get("MUG_API_COMMENT", "MonChoon Market Tracker")
# Explicit User-Agent on every Torn request. Torn 403s requests with a default library UA (e.g.
# python-urllib); send a browser-like one so we're never silently blocked.
HTTP_USER_AGENT = os.environ.get("MUG_HTTP_USER_AGENT", "Mozilla/5.0 (compatible; MugBot/1.0)")

# ---- Rate limiting ----
# On a Torn "too many requests" (error 5), cool that key down for this long (the rotation skips
# it; if every key is cooling down, all calls pause until one recovers).
RATE_LIMIT_BACKOFF_SECONDS = int(os.environ.get("MUG_RATE_LIMIT_BACKOFF", "60"))
# If true, a single rate-limit pauses ALL keys for the backoff (vs only the offending key).
RATE_LIMIT_GLOBAL_PAUSE = (
    os.environ.get("MUG_RATE_LIMIT_GLOBAL_PAUSE", "false") or "").strip().lower() in ("1", "true", "yes")
# On a KEY-specific Torn error (rate-limit / bad key), immediately retry the call on the next key in
# the rotation, up to this many keys, before giving up. Torn-SIDE errors (backend/IP) don't retry
# (another key from the same IP won't help). 1 = no retry (old behaviour).
API_MAX_KEY_RETRIES = int(os.environ.get("MUG_API_MAX_KEY_RETRIES", "3"))
# Requests per minute allowed PER KEY before the rotation skips it. Torn's own limit is ~100/min;
# staying under it ourselves means we stop hitting error 5, which parks a key for
# RATE_LIMIT_BACKOFF_SECONDS and shrinks the pool exactly when we're busiest. 0 disables the budget.
KEY_BUDGET_PER_MIN = int(os.environ.get("MUG_KEY_BUDGET_PER_MIN", "100"))

# ---- Raw ingest capture (Phase 0 discovery) ----
# OFF by default. Every ingest POST used to be appended to scratch/dumps/*.jsonl AND inserted into
# the raw_events table, unconditionally and forever, to reverse-engineer the WebSocket schema. That
# work is done — the parser exists — so this now has to be asked for. When on, dumps are written off
# the event loop (a blocking write in an async handler stalls the poller and any in-flight alert).
RAW_CAPTURE = (os.environ.get("MUG_RAW_CAPTURE", "false") or "").strip().lower() in ("1", "true", "yes")
# raw_events rows older than this are pruned at startup and daily. The table shares a database with
# the poller and the alert path, so its bloat is not free.
RAW_RETENTION_DAYS = int(os.environ.get("MUG_RAW_RETENTION_DAYS", "7"))

# ---- Logging ----
# Level for OUR loggers (mug.*). httpx/httpcore are pinned to WARNING regardless: they log the full
# request URL, and the Torn key is a query parameter, so at INFO they write live credentials into
# the journal on every call. DEBUG here raises our verbosity without re-enabling that.
LOG_LEVEL = (os.environ.get("MUG_LOG_LEVEL", "INFO") or "INFO").strip().upper()

# ---- Debugging ----
# Verbose gear reconciliation: log, per poll, how every uid was classified (tracked / matched by
# uid / matched by price / anon / unmatched) + each /gear capture stored. Turn on to diagnose
# "weapons won't leave the uncached list". MUG_DEBUG_GEAR=1.
DEBUG_GEAR = (os.environ.get("MUG_DEBUG_GEAR", "false") or "").strip().lower() in ("1", "true", "yes")

# ---- Detection mode ----
#   api  : poll the item-market API (gear tracked by uid) + item-matcher sellers — the SAFE path.
#   ws   : the market-tracker WebSocket path (Phase 1, stackable items).
#   both : run both sources into the shared mug/alert pipeline.
DETECTION_MODE = (os.environ.get("MUG_DETECTION_MODE", "api") or "api").strip().lower()


def detection_api_enabled() -> bool:
    return DETECTION_MODE in ("api", "both")


def detection_ws_enabled() -> bool:
    return DETECTION_MODE in ("ws", "both")


# ---- API poller (gear + items) ----
# Poll every ~30s to match the API's 30s global cache (polling faster just returns stale data).
# (Used for the calls/min estimate; actual scheduling is adaptive — see CACHE_REPOLL_OFFSET below.)
GEAR_POLL_INTERVAL_SECONDS = int(os.environ.get("MUG_GEAR_POLL_INTERVAL", "30"))

# Adaptive cache-timed scheduling. Each itemmarket response carries `cache_timestamp` (when Torn
# generated that snapshot). Measured behaviour (202 items): the cache regenerates on an EXACT 30s
# cycle, but a snapshot only becomes VISIBLE ~29s (28-31) after its stamp — so the API always serves
# data ~30s stale, and the NEXT distinct snapshot doesn't appear until ~cache_timestamp+60. We
# therefore poll each item at `cache_timestamp + CACHE_REPOLL_OFFSET` (just after the next snapshot
# lands) instead of a blind sweep: freshest possible data, ~1 poll per item per cache cycle, and no
# 504-storm-inducing over-polling. A naive cache_timestamp+31 would re-hit the SAME snapshot.
CACHE_REPOLL_OFFSET = int(os.environ.get("MUG_CACHE_REPOLL_OFFSET", "62"))
# If a poll returns the same cache_timestamp as last time (we polled a touch early, or the snapshot
# was slow to appear), re-check after this many seconds rather than recomputing from the stale stamp
# (which would busy-loop). Also the backoff after a failed/None fetch.
CACHE_RETRY_SECONDS = int(os.environ.get("MUG_CACHE_RETRY_SECONDS", "3"))
# Scheduler granularity: how often the loop wakes to poll whatever items are now due.
POLL_TICK_SECONDS = float(os.environ.get("MUG_POLL_TICK_SECONDS", "1"))
# Consecutive absent polls before a tracked uid is called sold. 0/1 = fire on the FIRST absence
# (fastest; ~30s less latency). >1 absorbs a delist→relist reprice (uid briefly absent) at the cost
# of latency. With confirm at 0 a reprice will fire a (likely-de-list) alert — mute those via
# /delist mode. NOTE: the comparison is `misses < N`, and misses starts at 1, so 0 and 1 both mean
# "fire on the first absence".
GEAR_VANISH_CONFIRM_POLLS = int(os.environ.get("MUG_GEAR_VANISH_CONFIRM_POLLS", "0"))
# Opt-in stale-match guard (default OFF): only treat a vanished uid as a SALE if it was re-confirmed
# present in >1 poll. Off by default — we now drop the seller↔uid pairing the moment the item leaves
# the market (see reconcile_gear), so stale matches can't accumulate; we alert on every vanish and
# you judge delist-vs-sale yourself.
GEAR_REQUIRE_RESEEN = (os.environ.get("MUG_GEAR_REQUIRE_RESEEN", "false") or "").strip().lower() in ("1", "true", "yes")
# Poll items CONCURRENTLY (asyncio) so a big watch list still finishes within the interval instead
# of falling behind (late detection = mugs that already got banked). This many item-polls run at
# once; 0 = auto (= number of active API keys, so each in-flight call lands on a different key).
# Keep it near your key count to respect Torn's ~100 req/min per-key budget.
MAX_CONCURRENT_POLLS = int(os.environ.get("MUG_MAX_CONCURRENT_POLLS", "0"))

# Only fetch GEAR listings with a bonus (the valuable mug targets) — slashes volume. "Any" or "".
GEAR_BONUS_FILTER = os.environ.get("MUG_GEAR_BONUS_FILTER", "Any").strip()
# Gear that should instead be polled with NO bonus filter (capture EVERY listing, not just bonus
# ones) — e.g. melee weapons that sell high without a bonus. Comma-separated seed of gear category
# names (Primary/Secondary/Melee/Defensive) and/or item ids; editable live via /gear allbonus.
GEAR_ALL_BONUS = [t.strip() for t in (os.environ.get("MUG_GEAR_ALL_BONUS", "") or "").split(",") if t.strip()]
# Max itemmarket pages (100 listings each) fetched per item per poll, split by type:
#   GEAR  — paginate fully. A valuable bonus piece can sit on ANY page, so we want every listing
#           (the bonus=Any filter keeps the volume sane). Capped for safety; the poller stops early
#           once a page returns < 100, so it naturally fetches "all pages".
#   ITEM  — just the first page. The cheapest 100 stackables are the only ones that ever sell;
#           deeper pages are absurd prices = wasted API budget + more 504s.
MAX_PAGES_GEAR = int(os.environ.get("MUG_MAX_PAGES_GEAR", "50"))
MAX_PAGES_ITEM = int(os.environ.get("MUG_MAX_PAGES_ITEM", "1"))
# Optional allowlist of item ids to poll (comma-separated). Empty = poll every item we have
# captures for. Use this to "start small" (e.g. a few rifles + specific armour).
WATCH_ITEM_IDS = [int(x) for x in (os.environ.get("MUG_WATCH_ITEM_IDS", "") or "").split(",") if x.strip().isdigit()]

# Items: a quantity drop at a unique price is a confident sale; a unique-priced listing fully
# vanishing is ambiguous (sale vs delist vs reprice). Default: only alert on quantity drops.
ITEM_VANISH_CONFIRM_POLLS = int(os.environ.get("MUG_ITEM_VANISH_CONFIRM_POLLS", "1"))
ITEM_ALERT_ON_REMOVE = (
    os.environ.get("MUG_ITEM_ALERT_ON_REMOVE", "false") or "").strip().lower() in ("1", "true", "yes")

# Delist guard (GEAR), DISABLED by default (0): formerly, if the seller acted within this many
# seconds of a vanish we'd assume a delist and drop it. We now alert on every vanish (you judge
# delist-vs-sale yourself) and drop the seller↔uid pairing on vanish so a relist must be re-marked.
# Set >0 to re-enable the old auto-suppression.
DELIST_GUARD_SECONDS = int(os.environ.get("MUG_DELIST_GUARD_SECONDS", "0"))

# Delist TAG (GEAR): if a gear uid vanished while the seller was online/idle and had acted within
# this many seconds, the sale is flagged a likely de-list — the seller was sitting there fiddling
# with their own listing (a real buy means the BUYER acted, so the seller's last action is usually
# old/offline). 0 disables the tag. Tunable live with /delist. The DEFAULT here seeds the DB config.
DELIST_TAG_SECONDS = int(os.environ.get("MUG_DELIST_TAG_SECONDS", "120"))
# How likely-de-list sales are handled (tunable live with /delist):
#   ping   -> post the alert and ping the mug role (same as a normal sale)
#   silent -> post the alert but DON'T ping the role (visible, no notification)
#   off    -> drop it: no alert, and the phantom sale is removed from the seller's value window
DELIST_MODE = (os.environ.get("MUG_DELIST_MODE", "ping") or "ping").strip().lower()

# Mug protection: when a seller was just mugged (in hospital, "Mugged by …"), their wallet was
# largely taken and the leftover is "protected", decaying linearly to 0 over this many hours. We
# estimate the mug time as their last action (they can't act while hospitalised) and reduce the mug
# value by the still-protected fraction. 0 disables.
MUG_PROTECTION_HOURS = float(os.environ.get("MUG_PROTECTION_HOURS", "12"))

# ---- Mug calculation (runtime-tunable) ----
# Matches the reference "Mr. Blobby" bot: 5%-15% of accumulated sales (the 5% min is also what
# the alert threshold is measured against). e.g. 45,488,286 -> $2,274,414 - $6,823,243.
MUG_PCT_MIN = 0.05
MUG_PCT_MAX = 0.15
CLOTHING_STORE_MULTIPLIER = 0.25     # 7-star Clothing Store => 75% mug reduction
# Alert (from the MUG perspective) when mug_min (= sales * 5% * clothing) >= this. /mugmin tunes it.
MIN_MUG_VALUE = 5_000_000

# Rolling window over which a seller's sales accumulate into "value change".
SALE_WINDOW_SECONDS = 60 * 60 * 6    # 6h

# ---- Mug history decay (avoid re-counting cash that's been banked or already mugged) ----
# After a mug alert posts we assume the cash was taken (the alert => a mug), so those sales are
# "claimed" and never counted again — this stops the window from re-counting the same sales across
# alerts (e.g. a later hospital alert re-listing the Donator Packs from the first alert).
MUG_CLAIM_ON_ALERT = (os.environ.get("MUG_CLAIM_ON_ALERT", "true") or "").strip().lower() in ("1", "true", "yes")
# Banking assumption: a sale's muggable value decays as the seller stays ACTIVE after it (they've
# had time to bank). Full value if they went quiet within GRACE of the sale; zero once they've been
# active ASSUME seconds past it; linear in between. (Uses the seller's last_action time.)
# Tightened 2026-06-28: the old 15m/6h horizon barely decayed anything — a seller active 17min after
# a sale still kept ~99% weight, so stale already-banked sales (e.g. COLDCHAIN's 18-min-old TMP+P90)
# inflated the window over threshold and fired $0 "mugs". 3m full-credit, fully banked by 18m-active
# encodes "if they've been online a while since selling, the cash is in their wallet, not muggable".
MUG_BANK_GRACE_SECONDS = int(os.environ.get("MUG_BANK_GRACE_SECONDS", str(3 * 60)))      # 3m
MUG_BANK_ASSUME_SECONDS = int(os.environ.get("MUG_BANK_ASSUME_SECONDS", str(18 * 60)))   # 18m
# Don't re-alert the same seller more often than this (they keep accumulating meanwhile).
ALERT_COOLDOWN_SECONDS = 60 * 10     # 10m
# Clothing-store (job) + BSP are slow-changing — refresh once a day. (Company job perks only
# apply after 3 days, so a daily refresh is plenty to catch a job change.)
SELLER_ENRICH_TTL_SECONDS = 60 * 60 * 24  # 24h
# Hospital status changes often and is only ever refreshed when a sale is made, capped by this TTL.
SELLER_STATUS_TTL_SECONDS = 120
# How a sale by an IN-HOSPITAL seller is alerted (tunable live with /hosplock):
#   live   -> post immediately with a release countdown (you see all data as it lands)
#   remind -> post immediately AND re-post once near release (the countdown + a final nudge)
#   hold   -> don't post until ~lead before release, then post once (the original behaviour)
HOSP_MODE = (os.environ.get("MUG_HOSP_MODE", "live") or "live").strip().lower()
# Lead window (seconds): for hold/remind, how long before release we post / re-post. We re-fetch the
# seller's live status right before posting and only fire if they're genuinely leaving within this
# window (or already out) — so a re-hospitalised seller is re-held, not posted early. Tunable via
# /hosplock (in minutes). The DEFAULT here seeds the DB config.
HOSPITAL_RELEASE_LEAD_SECONDS = 120

# ---- Items deal-finder (items channel) ----
ITEM_DEAL_PCT = 50                   # alert when listed <= (100 - this)% of market value

# ---- Hospitalize factions ----
# Sellers in these factions are flagged HOSPITALIZE (don't mug — put them in hospital) on the alert.
# Seed list (comma-separated faction ids); editable live via /faction add|remove (config table).
HOSPITALIZE_FACTION_IDS = [
    int(x) for x in (os.environ.get("MUG_HOSPITALIZE_FACTION_IDS", "") or "").split(",") if x.strip().isdigit()]

# ---- Status tracker ----
# A page must heartbeat at least this often (only happens while actively viewed);
# otherwise it is shown red/offline in status-tracker.
STATUS_STALE_SECONDS = 90
# How often the bot refreshes the status-tracker panel + checks hospital releases.
STATUS_REFRESH_SECONDS = 30
# When captures arrive, coalesce status-panel edits and apply them after this quiet period (so
# rapid marking results in one edit shortly after you stop, not a 30s wait).
STATUS_DEBOUNCE_SECONDS = 3
# Hard floor between actual Discord message edits (PATCH). Editing the same status message faster
# than this gets HTTP 429 rate-limited (which is what happened during heavy marking), so the
# debounced refresh never edits more often than this regardless of how fast captures arrive.
STATUS_MIN_EDIT_INTERVAL = int(os.environ.get("MUG_STATUS_MIN_EDIT_INTERVAL", "6"))
# The watch list is split into one Discord message per category. A full refresh edits several
# messages; space them out so we stay under Discord's per-channel edit rate limit (~5/5s).
STATUS_EDIT_SPACING = float(os.environ.get("MUG_STATUS_EDIT_SPACING", "1.2"))

# ---- Torn URLs ----
PROFILE_URL = "https://www.torn.com/profiles.php?XID={user_id}"
ATTACK_URL = "https://www.torn.com/page.php?sid=attack&user2ID={user_id}"
ITEM_MARKET_URL = "https://www.torn.com/page.php?sid=ItemMarket#/market/view=search&itemID={item_id}"

# Tracked drug/consumable set (mirrors the userscript itemMap).
ITEM_MAP = {
    106: "Parachute", 206: "Xanax", 283: "Donator Pack", 329: "Skateboard",
    330: "Boxing Gloves", 331: "Dumbbells", 103: "Firewalk Virus", 366: "Erotic DVD",
    367: "Feathery Hotel Coupon", 370: "Drug Pack", 396: "Business Class Ticket",
    428: "Casino Pass", 532: "Can of Red Cow", 533: "Can of Taurine Elite",
    554: "Can of Rockstar Rudolph", 555: "Can of X-MASS", 818: "Six-Pack of Energy Drink",
    865: "Poison Mistletoe", 1029: "Strippogram Voucher", 1118: "Armor Cache",
    1119: "Melee Cache", 1120: "Small Arms Cache", 1121: "Medium Arms Cache",
    1122: "Heavy Arms Cache",
}
