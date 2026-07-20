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
systemd/           mug-bot.service
```

## Setup

```bash
cd mug-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill token + channel ids + Torn/BSP keys (all live in this .env)
```

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

## Deploy (Debian package)

The app ships as a `.deb` that installs to `/home/monchoon/torn/mug-bot` and runs as the
`mug-bot` systemd service (enabled + started by the package's `postinst`).

**One command (test → build → scp → install over SSH), from your dev machine:**

```bash
python packaging/release.py            # default: full deploy (prompts for your sudo password)
python packaging/release.py --env      # also push your local .env (first deploy / .env changed)
python packaging/release.py --no-install   # build + scp only; install it yourself
```

It runs the tests, builds the `.deb`, scps it to `/tmp` on the box, and `apt install --reinstall`s
it over `ssh -t` — which restarts the service via the postinst. The DB and `.env` are preserved;
new schema tables auto-create on restart.

(Lower-level scripts also exist: `packaging/build-deb.sh [ver]` and `packaging/deploy.sh [ver]`.)

### The `.env` on the server

Only the **server** needs `.env` (Discord token + channel ids + Torn/BSP keys). It lives at
`/home/monchoon/torn/mug-bot/.env` and is **never** packaged. Two ways to provision it:

- **Edit in place (simplest):** the first install seeds `.env` from `.env.example`; then
  `sudo nano /home/monchoon/torn/mug-bot/.env`, fill it in, `sudo systemctl restart mug-bot`.
- **Push from your machine:** fill `mug-bot/.env` locally, then `python packaging/release.py --env`
  copies it to the right path, fixes ownership, and restarts the service.

Notes:
- The server needs internet at install time (postinst `pip install`s into the venv).
- Open inbound TCP 3001 on the box: `sudo ufw allow 3001/tcp`.
- **Persistent data:** the SQLite DB lives at `/var/lib/mug-bot/mug_bot.db` (created by the postinst),
  **outside** the install dir, so reinstalls/upgrades never touch your watch list, keys, or captures.
  `.env` (in the install dir) and the DB are never packaged, so both survive too. `apt purge` keeps
  `/var/lib/mug-bot` as well — delete it by hand for a true clean slate.
- **Schema migrations:** additive changes are automatic (`CREATE TABLE IF NOT EXISTS` + an
  `ALTER TABLE … ADD COLUMN` pass in `db._ensure_columns`). A *breaking* change (renamed/retyped
  column, data reshaping) needs a one-off migration step added to `db.init_db` before release —
  note it in the changelog since the DB persists across versions.

## Migrating to a new server (e.g. a Raspberry Pi 5)

Everything that matters lives in exactly two places, so a move is "copy two things":

| What | Where | Holds |
|------|-------|-------|
| `.env` | `/home/monchoon/torn/mug-bot/.env` | Discord token, channel ids, `TORN_API_KEYS`, BSP/TornStats keys |
| SQLite DB | `/var/lib/mug-bot/mug_bot.db` | sellers, captures, sales, watch list, `config` settings, the `api_keys` pool |

(API keys live in **both** — `.env` seeds them on boot, the DB keeps each key's owner + enabled
state. Copying both carries them fully.) The `.deb` is `Architecture: all` and builds its venv on
the target with `pip`, so x86 → ARM (the Pi) just works — it only needs internet at install time.

**1) On the OLD server — take clean, consistent copies:**

```bash
sudo systemctl stop mug-bot                                          # quiesce writes / flush the WAL
sudo sqlite3 /var/lib/mug-bot/mug_bot.db ".backup '/tmp/mug_bot.db'" # consistent single-file snapshot
sudo cp /home/monchoon/torn/mug-bot/.env /tmp/mug-bot.env
sudo chown monchoon:monchoon /tmp/mug_bot.db /tmp/mug-bot.env
# done copying — restart the old box if you want it running until the Pi is live:
sudo systemctl start mug-bot
```

**2) Pull them to your machine** (or scp straight to the Pi if it can reach the old box):

```bash
scp monchoon@<OLD_SERVER>:/tmp/mug_bot.db  ./mug_bot.db
scp monchoon@<OLD_SERVER>:/tmp/mug-bot.env ./mug-bot.env
```

**3) On the NEW Pi — install the app first, then drop the data on top.** Installing first creates
the dirs, venv and systemd service (and a placeholder `.env`); then you overwrite `.env` + the DB.

```bash
# from your dev machine — builds the .deb, scps it, installs over SSH (postinst builds the venv):
python packaging/release.py --server monchoon@<PI_IP>

# push your migrated files to the Pi:
scp ./mug-bot.env monchoon@<PI_IP>:/tmp/mug-bot.env
scp ./mug_bot.db  monchoon@<PI_IP>:/tmp/mug_bot.db
```

```bash
# then on the Pi:
ssh monchoon@<PI_IP>
sudo systemctl stop mug-bot
sudo cp /tmp/mug-bot.env /home/monchoon/torn/mug-bot/.env
sudo cp /tmp/mug_bot.db  /var/lib/mug-bot/mug_bot.db
sudo rm -f /var/lib/mug-bot/mug_bot.db-wal /var/lib/mug-bot/mug_bot.db-shm   # stale WAL sidecars
sudo chown monchoon:monchoon /home/monchoon/torn/mug-bot/.env /var/lib/mug-bot/mug_bot.db
sudo ufw allow 3001/tcp        # inbound port for the userscript POSTs
sudo systemctl start mug-bot
journalctl -u mug-bot -f       # confirm it boots + the keys come up
```

**4) Repoint the userscripts** from the old IP to `http://<PI_IP>:3001` — `SERVER_URL` in
`item-matcher.user.js` and the base URLs in `market-tracker.user.js`.

> Storage tip (Pi): put the DB on an **SSD**, not the SD card. The bot writes constantly; SD cards
> are slow at random writes and wear out. The paths above assume the default — if you relocate the
> DB, set `MUG_DB_PATH` in `.env` to the new file.

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
