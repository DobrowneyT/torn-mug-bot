# Mug Bot

Self-hosted Torn item-market monitor + Discord alerts. A single Python process runs a
FastAPI ingest server (receives data the userscript forwards from the **page you are
actively viewing**) and a discord.py bot that posts to three channels:

- **logs** — a mug-alert embed per qualifying sale (seller, mug range, TBS, attack link).
- **status-tracker** — 🟢/🔴 per actively-viewed page + cache counts.
- **items** — alerts when something is listed well under market value.

Storage is SQLite on the box. **This folder is fully self-contained** — no imports or config
dependencies on the rest of the repo; copy it to a standalone server and run it on its own. All
secrets/config live in `mug-bot/.env`.

## Compliance (read this)

Torn's updated scripting rules permit scraping/forwarding the WebSocket of a page **you are
actively viewing** (foreground + visible + interacting); they forbid it for pages you are
**not** actively viewing. The hardened `market-tracker` userscript therefore only connects
and forwards while `document.visibilityState === 'visible' && document.hasFocus()`, and
disconnects the instant the tab is hidden or loses focus. Do not run it on background tabs /
many pages. If ever unsure, ask Torn staff (the announcement invites it).

## Layout

```
main.py            entrypoint: uvicorn + discord.py on one asyncio loop
config.py          thresholds, windows, channel ids, Torn URLs
server/            FastAPI ingest (app.py), SQLite (db.py, schema.sql)
bot/               discord_bot.py (live), formatter.py (embeds), state.py, mock_data.py
scratch/           format_preview.py (mock embeds), dumps/ (raw payload captures)
deploy/            provision.sh (once per box), update.sh (every deploy)
systemd/           mug-bot.service.template (provision.sh fills in the real paths)
```

## Setup

Local development. For a server, use `deploy/provision.sh` instead — see [Deploy](#deploy).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # requirements.txt + pytest
cp .env.example .env        # fill token + channel ids + Torn/BSP keys (all live in this .env)
pytest -q
```

Locally, leaving `MUG_DB_PATH` blank is fine — the bot falls back to a `mug_bot.db` beside the code.
On a server, always set it (see [Deploy](#deploy)).

## Track A — iterate on the embeds with mock data (no server needed)

Set `MUG_BOT_DISCORD_TOKEN` + `MUG_PREVIEW_CHANNEL_ID` in `.env`, then:

```bash
python scratch/format_preview.py
```

First run posts one message per embed type; reruns edit them in place after each
`bot/formatter.py` tweak.

## Run the server (Phase 0 — discovery)

```bash
python main.py
# POSTs to /market, /status, /gear are dumped to scratch/dumps/*.jsonl AND raw_events,
# so we can inspect the real WebSocket schema before writing the Phase 1 parser.
curl -s localhost:3001/health | python3 -m json.tool
```

Point the hardened `market-tracker.user.js` at `http://<box-ip>:3001`, actively view an
item-market page, and watch `scratch/dumps/market-*.jsonl` fill in.

## Discord bot setup

1. https://discord.com/developers/applications → **New Application** (e.g. "Mug Bot").
2. **Bot** tab → **Reset Token** → copy into `MUG_BOT_DISCORD_TOKEN`. No privileged intents needed.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions **Send Messages**,
   **Embed Links**, **Read Message History**. Open the URL and invite the bot to your server.
4. Discord → Settings → Advanced → enable **Developer Mode**. Then right-click each channel →
   **Copy Channel ID** and the server icon → **Copy Server ID**, and fill `.env`:
   `MUG_LOGS_CHANNEL_ID`, `MUG_STATUS_CHANNEL_ID`, `MUG_ITEMS_CHANNEL_ID`, `MUG_GUILD_ID`
   (+ `MUG_PREVIEW_CHANNEL_ID` for embed previews).
5. Start the bot. `/mugmin` registers automatically (instant when `MUG_GUILD_ID` is set):
   `/mugmin 30m` sets the alert threshold (measured on the **mug** value = 5% of accumulated
   sales, clothing-adjusted); `/mugmin` alone shows the current value.

Mug-alert border colour reflects a fresh status check at alert time: **🟢 online**, **🔴 in
hospital**, **🔵 offline/idle**.

### Slash commands

- `/mugmin [value]` — show/set the alert threshold (mug value, e.g. `5m`, `30m`).
- `/watch list|add|remove|enable|disable <item>` — manage which items the poller watches. `add`
  autocompletes from the full catalog (name or id); the rest autocomplete from your current list.
  Items are also auto-added (enabled) when the item-matcher first captures a seller for them.
- `/keys list|add|remove|enable` — manage the Torn API key pool. `add` validates the key (confirms
  access + records the owner id/name/level); a key that errors at runtime with a *real* key-level
  error is auto-disabled and a notice is posted to the status channel (transient rate-limit/backend
  errors only cool the key down, never disable it). `enable [name|id]` re-enables a disabled key, or
  all of them if left blank — recovery for a key that got knocked out. Keys in `TORN_API_KEYS` are
  seeded into the pool on first start.

The `status-tracker` panel shows captured-seller counts + the watch list (✅ enabled / ⬜ disabled,
sellers captured, listings tracked, last poll).

## WebSocket token

The userscript connects to Torn's Centrifugo WebSocket with a JWT whose payload is
`{"sub":"<your Torn user id>"}` — it's **per account, not per page**, and is reused for every
channel subscription. The committed value belongs to the original author; set `wsToken` in
`market-tracker.user.js` to **your own** token. (Follow-up: pull it live from the page instead
of hardcoding — see the plan.)

## Deploy

Deploys are **pull-based**: the server owns its own checkout and updates itself. Merge a PR to
`main` and the box picks it up within five minutes. Nothing off the box holds credentials to it —
which is why this is a cron pulling from GitHub rather than CI pushing over SSH.

(This replaces the old `.deb` pipeline. That pushed from a laptop, targeted a host that no longer
exists, and could not be automated without giving GitHub Actions SSH access to the server.)

### First time on a new box

```bash
sudo apt install -y python3-venv python3-pip git sqlite3
git clone git@github.com:DobrowneyT/torn-mug-bot.git ~/torn/torn-mug-bot
cd ~/torn/torn-mug-bot
./deploy/provision.sh
```

`provision.sh` is idempotent — re-run it any time. It creates `/var/lib/mug-bot`, builds `.venv`,
seeds `.env` from `.env.example` (never clobbering an existing one), and generates
`/etc/systemd/system/mug-bot.service` from **this box's** real user and paths. It deliberately does
not do two things, and prints instructions for both: installing the deploy cron, and moving an
existing database.

Then fill in `.env` — at minimum the Discord token, channel ids, `TORN_API_KEYS`, and:

```bash
MUG_DB_PATH=/var/lib/mug-bot/mug_bot.db
```

**Set `MUG_DB_PATH` explicitly on a server.** If it is unset and `/var/lib/mug-bot` is not writable,
the bot silently falls back to putting the database *inside the git checkout*, where `git clean` or a
re-clone will destroy it.

```bash
sudo systemctl start mug-bot
journalctl -u mug-bot -f          # confirm it boots and the keys come up
sudo ufw allow 3001/tcp           # inbound for the userscripts' POSTs
```

### Updating

```bash
./deploy/update.sh                # pull + deps + backup + restart + verify
./deploy/update.sh --if-changed   # exit silently unless origin moved (what cron runs)
SKIP_BACKUP=1 ./deploy/update.sh  # skip the pre-restart safety dump
```

It takes a `flock` (a slow run can never be overlapped by the next cron tick), fast-forwards only
(a server checkout must never invent merge commits), reinstalls dependencies **only** when
`requirements.txt` changed, snapshots the database, restarts the service, and then polls `/health`
until it answers — exiting non-zero, naming the `journalctl` command, if it never does.

It **refuses to run on a dirty working tree**. If you have edited files on the server, it stops and
lists them rather than merging over your changes.

### Turning on auto-deploy

Once you have watched a few manual runs, `crontab -e`:

```
*/5 * * * * /home/ubuntu/torn/torn-mug-bot/deploy/update.sh --if-changed >> /var/log/mug-bot-deploy.log 2>&1
```

The restart needs passwordless sudo — `provision.sh` checks this and prints the exact
`/etc/sudoers.d/mug-bot` line if it is missing.

Auto-deploy assumes `main` is protected and CI-gated, so only tested code ever reaches the box.

### Backups

Each deploy writes `mug_bot-<utc>-<sha>.db` next to the database under `backups/`, keeping the ten
most recent. The `<sha>` is the version being *replaced*, so it names what you would roll back to.
A failed backup warns but never blocks a deploy; a missing `sqlite3` binary warns loudly, because
that silently disables the safety net.

To roll back: `git checkout <sha>` in the checkout, `sudo systemctl restart mug-bot`, and restore a
backup over `$MUG_DB_PATH` only if a schema change actually requires it.

### What survives a deploy

`.env` and the database. `.env` is gitignored; the database lives outside the checkout entirely.
`git merge --ff-only` cannot touch either.

**Schema migrations** are additive and automatic (`CREATE TABLE IF NOT EXISTS` plus an
`ALTER TABLE … ADD COLUMN` pass in `db._ensure_columns`). A *breaking* change — a renamed or retyped
column, reshaped data — needs a one-off migration step added to `db.init_db` before it is merged,
since the database persists across deploys.

## Moving to a new server

Everything that matters is two files, so a move is "copy two things":

| What | Where | Holds |
|------|-------|-------|
| `.env` | `<checkout>/.env` | Discord token, channel ids, `TORN_API_KEYS`, BSP/TornStats keys |
| SQLite DB | `$MUG_DB_PATH` (`/var/lib/mug-bot/mug_bot.db`) | sellers, captures, sales, watch list, `config`, `api_keys` |

API keys live in both — `.env` seeds them on boot, the DB keeps each key's owner and enabled state.

**On the old box**, take consistent copies:

```bash
sudo systemctl stop mug-bot
sqlite3 /var/lib/mug-bot/mug_bot.db ".backup '/tmp/mug_bot.db'"   # folds in the WAL; do not just cp
cp ~/torn/torn-mug-bot/.env /tmp/mug-bot.env
sudo systemctl start mug-bot        # the old box can keep running until the new one is live
```

**On the new box**, provision first (which creates the dirs, venv and unit), then drop the data on
top:

```bash
# after the "first time on a new box" steps above:
sudo systemctl stop mug-bot
cp /tmp/mug-bot.env ~/torn/torn-mug-bot/.env
cp /tmp/mug_bot.db  /var/lib/mug-bot/mug_bot.db
rm -f /var/lib/mug-bot/mug_bot.db-wal /var/lib/mug-bot/mug_bot.db-shm   # stale sidecars
sqlite3 /var/lib/mug-bot/mug_bot.db "PRAGMA integrity_check;"          # expect: ok
sudo systemctl start mug-bot
```

Finally, repoint the userscripts at the new address (see `userscripts/`).

> Storage tip (Raspberry Pi): put the database on an **SSD**, not the SD card. The bot writes
> constantly, and SD cards are slow at random writes and wear out.

## Resetting the database (fresh item / seller / gear data)

Wipes all captured history and uid↔seller mappings so the bot relearns owners from scratch, while
**keeping every setting** (the `config` table — your `/mugmin`, `/delist`, `/hosplock`, etc.), the
**API key pool** (`api_keys`), and the **watch list** (`watchlist`). It never touches `.env`.

```bash
sudo systemctl stop mug-bot
sudo sqlite3 /var/lib/mug-bot/mug_bot.db <<'SQL'
DELETE FROM sellers;          -- enrichment (clothing/status/TBS/last_action)
DELETE FROM listings;         -- item-matcher captures (the uid -> seller map)
DELETE FROM sales;            -- the rolling mug-value window history
DELETE FROM tracked_gear;     -- poller's live per-uid gear view
DELETE FROM tracked_items;    -- poller's live per-price stackable view
DELETE FROM unmatched_gear;   -- uids still awaiting a seller
DELETE FROM anon_uids;        -- stashed anonymous uids
DELETE FROM monitored_pages;  -- page heartbeats (status-tracker)
DELETE FROM raw_events;       -- raw ingest payload log
DELETE FROM item_values;      -- market-value reference (auto-refreshes within ~4h)
VACUUM;
SQL
sudo systemctl start mug-bot
```

Sellers, listings and mappings repopulate as the item-matcher recaptures and the poller runs again.

- **Also reset which items are watched** (the bot re-adds each item as its seller is recaptured):
  add `DELETE FROM watchlist;` to the block above.
- **Nuke everything, including settings + keys** (true factory reset): stop the service and
  `sudo rm -f /var/lib/mug-bot/mug_bot.db*` — it's recreated empty on the next start, and the keys
  re-seed from `TORN_API_KEYS` in `.env`.

### Wiping a single item

Forget one item entirely — every capture, seller match, sale and tracked listing for it — as if the
bot had never seen it (handy when one item's data gets into a bad state). Stop the service, set the
id, run the block, start it. Example for **206** (Xanax):

```bash
sudo systemctl stop mug-bot
id=206
sudo sqlite3 /var/lib/mug-bot/mug_bot.db "
DELETE FROM listings       WHERE item_id=$id;   -- item-matcher captures (seller -> uid/price maps)
DELETE FROM sales          WHERE item_id=$id;   -- this item's mug-window history
DELETE FROM tracked_gear   WHERE item_id=$id;   -- poller's live gear uids
DELETE FROM tracked_items  WHERE item_id=$id;   -- poller's live stackable prices
DELETE FROM unmatched_gear WHERE item_id=$id;   -- uids still awaiting a seller
DELETE FROM item_values    WHERE item_id=$id;   -- market-value reference (auto-refreshes anyway)
DELETE FROM watchlist      WHERE item_id=$id;   -- also stop polling it (re-adds on next capture)
"
sudo systemctl start mug-bot
```

- **Sellers are left alone on purpose** — a seller row is shared across every item they've listed,
  so deleting it for one item would wipe their data for the others. Only this item's listings/sales/
  maps go; the seller re-enriches as needed.
- Drop the `watchlist` line if you want to keep polling the item and only clear its captured data.
- `anon_uids` is keyed by uid (no item id column), so it isn't filtered here — harmless leftover
  "ignore" markers that get re-learned. Check what's mapped first with
  `sudo sqlite3 /var/lib/mug-bot/mug_bot.db "SELECT * FROM listings WHERE item_id=$id;"`.
