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

Two prerequisites, both of which fail *silently* under cron if you skip them.

**1. Git must authenticate with no terminal.** `update.sh` fetches on every run, including the
`--if-changed` no-op path. Cron has no TTY, so a passphrase-protected SSH key makes every deploy
fail with nothing to show for it. Use a **read-only deploy key** with no passphrase — your personal
key keeps its passphrase and stays usable for everything else:

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/mug-deploy -C 'mug-bot auto-deploy (read-only)'
cat ~/.ssh/mug-deploy.pub     # add under repo Settings -> Deploy keys, WITHOUT write access
```

```bash
cat >> ~/.ssh/config <<'EOF'

Host github-mugbot
  HostName github.com
  User git
  IdentityFile ~/.ssh/mug-deploy
  IdentitiesOnly yes
EOF
git remote set-url origin github-mugbot:DobrowneyT/torn-mug-bot.git
```

Prove it the way cron will experience it — `BatchMode=yes` forbids every interactive prompt:

```bash
GIT_SSH_COMMAND='ssh -o BatchMode=yes' git fetch origin && echo "OK: works with no terminal"
```

Read-only is deliberate: the server should only ever consume commits, never produce them.

**2. The restart needs passwordless sudo.** `provision.sh` checks this and prints the exact
`/etc/sudoers.d/mug-bot` line if it is missing. (Ubuntu cloud images usually grant it already.)

Then install the cron line — non-interactively, so there is no editor to fight:

```bash
( crontab -l 2>/dev/null; \
  echo '*/5 * * * * /home/ubuntu/torn/torn-mug-bot/deploy/update.sh --if-changed >> /var/log/mug-bot-deploy.log 2>&1' \
) | crontab -
sudo touch /var/log/mug-bot-deploy.log && sudo chown "$USER":"$USER" /var/log/mug-bot-deploy.log
```

That last line matters: the deploy user cannot create a file in `/var/log` itself, so without it the
redirect fails and you lose every deploy log.

Verify cron is actually invoking it — **an empty deploy log is ambiguous**, since `--if-changed`
prints nothing when there is nothing to pull:

```bash
journalctl -u cron --since '-15 min' | grep mug-bot     # proves cron fired
cat /var/log/mug-bot-deploy.log                          # proves it ran without errors
```

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

## Auction house scraper

Completed Torn auctions, pulled into an **ownership ledger**: which uid was won, by whom, when, for
how much. On its own it widens uid coverage; the point of it is #12, where an anonymous item-market
listing whose uid was won at auction can be attributed to the winner.

It runs from cron, not inside the bot, and writes to **its own database file**
(`MUG_AH_DB_PATH`, default `/var/lib/mug-bot/mug_ah.db`). That separation is deliberate: every
statement in the bot process serialises through one SQLite connection that the alert path also
queues on, so a backfill writing tens of thousands of rows into `mug_bot.db` would contend directly
with alerts.

```bash
./.venv/bin/python ah_scrape.py status      # how much have we got, and did the last run work
./.venv/bin/python ah_scrape.py forward     # pull newly-completed auctions (this is the cron job)
./.venv/bin/python ah_scrape.py backfill    # walk history backwards; resumable, run until complete
./.venv/bin/python ah_scrape.py coverage    # how much of the anon backlog the ledger can resolve
./.venv/bin/python ah_scrape.py calibrate   # how ACCURATE that resolution would have been
./.venv/bin/python ah_scrape.py probe       # print raw API rows, store nothing
```

API keys come from the same pool the bot uses (the `api_keys` table, managed with `/keys`), so
there is nothing extra to configure. Each run is capped at `MUG_AH_MAX_PAGES` requests and paces
itself between pages, so it cannot empty the key budget the poller depends on.

Install the cron line alongside the deploy one:

```bash
( crontab -l 2>/dev/null; \
  echo '15,45 * * * * cd /home/ubuntu/torn/torn-mug-bot && ./.venv/bin/python ah_scrape.py forward >> /var/log/mug-bot-ah.log 2>&1' \
) | crontab -
sudo touch /var/log/mug-bot-ah.log && sudo chown "$USER":"$USER" /var/log/mug-bot-ah.log
```

Every 30 minutes, offset off the hour so it never lands on the same tick as the deploy cron. At the
observed rate (~360 auctions/day) a run has about 7 auctions to collect, so it is a single request.

**`forward` runs the match step automatically.** New auctions are worth nothing until they have been
resolved against the un-attributed uids, so the scrape and the match are one operation rather than
two cron lines that can drift apart. `--no-match` skips it; `match` runs it alone.

### Downloading the whole history

Do this once, on the box, inside `screen`/`tmux` so it survives your ssh session:

```bash
cd /home/ubuntu/torn/torn-mug-bot
./.venv/bin/python ah_scrape.py backfill --max-seconds 0     # 0 = run until complete
```

Resumable and idempotent, so it is safe to interrupt and re-run — it continues from the oldest
auction it reached rather than starting over. It logs its way back through time:

```
ah backfill: 25 pages, 2500 new, now at 2026-07-14 09:12:41Z
ah backfill: 50 pages, 5000 new, now at 2026-06-28 22:04:03Z
ah backfill: COMPLETE — 812 page(s), 81143 new; history reaches back to 2024-03-02 11:55:07Z
```

If you would rather not babysit it, leave the default 30-minute budget and put it on a slow cron
until it reports complete — it no-ops from then on:

```bash
( crontab -l 2>/dev/null; \
  echo '17 * * * * cd /home/ubuntu/torn/torn-mug-bot && ./.venv/bin/python ah_scrape.py backfill >> /var/log/mug-bot-ah.log 2>&1' \
) | crontab -
```

**How far back Torn will serve is not documented**, and the scraper discovers it rather than
assuming: it walks backwards until a page comes back empty, records that history is complete, and
`status` prints the span it reached.

Do not read "complete" as proof nothing was skipped — a walk that went wrong reports complete too.
`status` prints a **per-year histogram** for exactly that reason: a year with nothing between two
years with plenty is the only visible signature of a gap, and no other check makes it visible. If
you see one, `backfill --restart` re-walks from the newest end. Inserts are idempotent, so a restart
costs time and nothing else.

Torn also serves some auctions with an epoch-adjacent timestamp — a real auction and a real buyer
with an unusable date. Those are dropped at ingest (`MUG_AH_MIN_TIMESTAMP`): ownership is "most
recent wins", so a win that cannot be placed in time is not evidence, and storing them breaks
`MIN(ended_at)` — which is both the reported span and the floor a resumed backfill starts from. This also means the ledger may not be re-derivable later — if
Torn's window is narrower than what you backfilled, re-running will not get that data back. Deploys
snapshot `mug_ah.db` alongside the main database for that reason.

Sizing, from a live sample: roughly 10 auctions settle every 27 minutes — ~500/day, ~180k/year. At
100 rows per request that is a few thousand requests per year of history, which is minutes of work
and a rounding error against a 14-key pool.

### Modes

`forward` is the routine job. It requests oldest-first from just below the newest auction it holds,
so each committed page permanently advances the watermark and an interrupted run loses nothing. It
deliberately re-requests a few seconds either side of that watermark — Torn documents `from` as
"after this time" without saying whether that is inclusive, and several auctions can share one end
timestamp. Re-fetching is free because a completed auction is immutable, so a re-delivered row is a
no-op insert.

On an empty ledger `forward` takes only the newest page and stops, rather than silently walking all
of history on a routine cron tick. Filling history is `backfill`'s job and is opt-in.

`backfill` walks the other way and bounds its `to` **inclusively** — on the oldest timestamp *on*
the page, not one below it. That is what Torn's own `prev` link does, and it matters: several
auctions settle in the same second, a page boundary can fall in the middle of them, and an exclusive
bound would skip the rest of that second permanently. The cost is one re-requested row per page,
which is a no-op insert.

Unlike `forward`, it is bounded by wall clock rather than page count — the length of a history
download is precisely the number we are trying to measure, so capping pages would mean guessing it.

### The anon-match alert

With `/anonmatch` on (silent by default), a gear sale whose seller was inferred rather than captured
posts with its provenance stated:

> 🕵️ **Anon match** — seller inferred from the auction house: Buyer4242 won this item for
> $9,000,000 3d ago · ~88% likely correct

Three precedence rules govern it, and each one is a way to blame the wrong player:

- **direct beats inferred.** A captured seller overrides an AH inference — *including* when the
  inference got there first. That case is easy to miss: once a uid is tracked, the poller
  short-circuits the whole resolution chain, so the upgrade has to be explicit. Without it the
  first answer wins for the life of the listing, and the first answer is the weaker one.
- **later beats earlier.** Ownership is the most recent observation from any source.
- **`off` means off.** It reproduces today's behaviour exactly — an anonymous uid stays a dead end.

The reconcile tally gains an `auction` bucket alongside `uid` and `anon`, so the inferred path can
be judged on its own hit rate rather than by feel.

### Searching by bonus (`reindex`)

`bonuses` is stored as the raw JSON Torn returns, which is right for fidelity and useless for
searching. `auction_bonuses` normalises it so a query can filter on **bonus id and a value range**
without scanning half a million JSON blobs:

```sql
SELECT auction_id FROM auction_bonuses WHERE bonus_id = 86 AND value BETWEEN 5 AND 9;
```

It is populated as auctions arrive, so a live scrape never leaves it behind. It is **derived** —
`auction_wins.bonuses` stays the source of truth — which means anything wrong with it is fixed by
regenerating, never by migrating:

```bash
./.venv/bin/python ah_scrape.py reindex     # ~6s for 548k auctions
```

`status` reports whether it is in step, because nobody watches a derived table and the alternative
is discovering the drift by running a search and not noticing the answers are wrong.

Two things it is careful about: a bonus with **no value** is stored NULL rather than 0, so it cannot
satisfy a range filter and quietly widen a search; and the same bonus appearing twice on one item is
kept as two rows (the primary key includes an ordinal) rather than collapsing.

### Serving the ledger (the price-checker API)

Off unless `MUG_API_TOKEN` is set — an endpoint publishing 548k rows of named players' purchase
history should be switched on deliberately, never by upgrading. Fail closed.

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # then put it in .env, restart
```

| route | |
|---|---|
| `POST /api/auctions/search` | the price-checker query: item, bonuses + value ranges, quality |
| `GET /api/auctions/meta` | row count, coverage span, freshness, max page size |

Both take `Authorization: Bearer <token>` (or an `apikey` header — the userscript already sends
one). Every request is logged to `MUG_API_ACCESS_LOG` with its source IP, including the rejections,
which are the ones worth having:

```
2026-08-03 12:04:11 ip=198.51.100.4 status=200 path=/api/auctions/search ua='Mozilla/5.0…' item='Sai' total=69 took=41ms
2026-08-03 12:07:02 ip=192.0.2.7   status=401 path=/api/auctions/search ua='curl/8.5.0' bad-token took=0ms
```

The token is never written to that log. `X-Forwarded-For` is ignored unless `MUG_API_TRUST_PROXY`
is on, because a client can send that header itself — trusting it by default would mean the log
faithfully records whichever IP the caller chose to claim.

Rate limited per token+IP (`MUG_API_RATE_PER_MIN`, default 60). A shared token leaks eventually;
this bounds what that costs.

The bot opens the ledger **read-only**. The scraper owns that file, and read-only means a bug on
the request path cannot write to it, migrate it, or take its write lock.

Measured over 560k auctions with a realistic spread (250 item names, 16 bonus types):

| query | |
|---|---|
| item only | 4ms |
| item + bonus + value range | 50ms |
| two bonuses | 132ms |

The result **count is capped** at 5,000 (`total_capped: true` says so). An unbounded `COUNT(*)`
was 91ms and grows forever while the page query does not — on a reachable endpoint that is a
scaling trap, not just a slow page.

### Using the ledger for other things

`mug_ah.db` is standalone — no foreign keys into `mug_bot.db`, no application state beyond
`ah_state` and `ah_runs`. Read it with anything. Indexes on `(uid, ended_at)`, `(item_id, ended_at)`,
`(buyer_id, ended_at)` and `ended_at` make per-item price history and per-player buying history both
cheap, and WAL means a reader never blocks the scraper.

```bash
./.venv/bin/python ah_scrape.py schema              # auction_wins, machine-readable
./.venv/bin/python ah_scrape.py schema --all-tables # + the scraper's bookkeeping tables
```

Emits columns, types, indexes, row count and coverage as JSON — plus **per-column semantics**, which
is the part a `PRAGMA` cannot give you. `auction_wins` is the only table an external consumer wants;
the other two are scraper bookkeeping and hold nothing Torn said. Data freshness is `MAX(seen_at)`
on the data table itself, so nothing is lost by ignoring them.

**Every field Torn returns is stored**, including all four stats and the full `bonuses` array as raw
JSON. Only `_metadata` (pagination links) is discarded.

Four NULLs mean something rather than nothing, and an endpoint built without knowing that will
produce confidently wrong aggregates:

| column | NULL means |
|---|---|
| `buyer_id` | the auction **ended unsold** — this is the "actually sold" filter |
| `uid` | the item has **no** armoury uid — every stackable, ~9% of rows |
| `price` | **0 bids**; nothing cleared |
| `sub_type` / `damage` / `armor` | not applicable to that item class, not missing |

And `ended_at` is **Torn's** timestamp (when the auction ended); `seen_at` is **ours** (when we
recorded it). The gap is scrape lag, not market behaviour.

Rows are dropped in exactly one case: no `id` or no `timestamp`, which is malformed rather than
meaningful. Auctions that **ended unsold** are kept with `buyer_id NULL` — they transfer no
ownership, so every ownership lookup filters them out, but they are real market signal (a reserve
nobody met) and cannot be recovered once they age out of Torn's window.

Undatable rows (`timestamp` before `MUG_AH_MIN_TIMESTAMP`) are also dropped — see above.

If you publish anything derived from this, note that it is named players' purchase history, and that
`buyer_id IS NOT NULL` is the filter for "actually sold".

### Resolving anonymous listings (`match`)

Cross-references every un-attributed uid — anonymous, or on the market with no captured seller —
against the ledger, and writes the resolved owner into the **main** database:

```bash
./.venv/bin/python ah_scrape.py match
```

Precomputed rather than looked up live, because `reconcile_gear` runs per item per poll on the same
connection the alert path queues on. Resolving each uid live would put a cross-database read on the
hot path for no benefit, since AH data only changes when the scrape runs.

Each row carries a `confidence` — the **measured** probability that the AH buyer is still the
seller, given how old the evidence is, taken from `calibrate` rather than chosen. It also carries
the evidence age and the auction price, so an alert can show its own provenance.

Posting is controlled by `/anonmatch mode:silent|ping|off`, **silent by default**:

| mode | behaviour |
|---|---|
| `off` | don't infer at all — an anonymous uid stays the dead end it is today |
| `silent` | post the alert, never ping the role *(default)* |
| `ping` | ping, but only above `min_confidence`; weaker inferences still post, silently |

`ping` is gated on **confidence rather than age**, because the measured curve has no cliff to cut
at — old evidence is weaker, not worthless, so an age cutoff would discard thousands of usable
matches to avoid a minority of wrong ones. The default bar (0.88) is the measured accuracy of
week-old evidence, so out of the box a ping means "won at auction within the last week".

Two rules the match step enforces, both of which are correctness rather than polish:

- **direct evidence beats inferred.** A uid the item-matcher has since captured a real seller for
  has its inferred row *deleted*. Keeping it would go on blaming an old auction winner for a listing
  whose seller we now know.
- **sentinel uids are excluded.** Torn's ledger contains one uid on 49,435 auctions — 9% of every
  row on record. It identifies no item, and matching on it would hand a large slice of the market to
  whoever last "won" it. Anything above `MUG_AH_MAX_UID_REUSE` is skipped, and excluded from the
  turnover statistics it would otherwise dominate.

### How far can an AH win be trusted? (`calibrate`)

The auction house is a **lossy observer of ownership**. A uid also changes hands by trade, by
bazaar, and by item-market sale, and it sees none of those. So "most recent AH win" is not "current
owner" — it is an estimate that decays, and the decay is what any confidence model has to be built
on.

That decay is measurable retroactively, with no feedback loop and no waiting. For every uid we have
*directly* observed a seller for, ask what the auction house would have claimed at that moment and
whether it matched:

```
  evidence age    checked   agreed   accuracy
  0-1d                 99       91      91.9%
  1-7d                113      107      94.7%
  7-30d                92       83      90.2%
  30-90d              114       86      75.4%
  90-365d              85       33      38.8%
  365d+                97        4       4.1%
  overall             600      404      67.3%
```

(Illustrative shape only — run it for your real numbers.) Read a row as: *if an anon-match alert had
fired on evidence this old, this is how often it would have named the right player.*

The ground truth comes from three places, and the important one is `sales`: gear sales are recorded
with `listing_id = "uid:<uid>"`, which makes that table an accidental but complete historical
uid→seller log. Unlike `listings` and `tracked_gear` it survives the listing vanishing — which is
precisely when a sale happens.

One honest caveat the report prints itself: the sample only covers uids that reached the **item
market**, because that is the only place a seller is observable. A uid won at auction and kept
forever never appears. That is the right bias — we only ever infer for uids we can see listed — but
the numbers describe items that move.

`calibrate` reports **two curves, separately**:

- **buyer** — a completed sale; the buyer held it
- **seller** — an auction that ended *unsold*; the seller kept it

Equally strong at the moment they happen, but differently selected afterwards. Someone whose auction
failed is far more likely than a random owner to relist — including anonymously, which is exactly
the population being resolved — so the seller curve could plausibly be *better*. Or worse, if a
failed auction mostly means the item went somewhere invisible. Sharing one table between them would
hide whichever it is.

**Answered: the no-buyer rows are neither.** `probe --oldest` showed they are auction ids 1-63 on
Torn staff accounts — seed data from when the auction house was built in January 2013. The feed
carries no active auctions at all: nothing has a future timestamp, and the ASC end is 2013 rather
than today. So it really is a completed-events feed.

Genuinely ended-unsold auctions do exist, but are **vanishingly rare** — at most 293 across 553,907
rows (0.05%). Modern Torn does not appear to publish expired auctions.

Which means seller-side inference stays **off, probably permanently**:
`AH_SELLER_CONFIDENCE_BANDS` is `None`, and `None` means unsold rows are excluded from matching
entirely. Not because the idea is wrong — an auction that failed to sell is good evidence the seller
kept the item — but because there is no sample to calibrate it against. The machinery stays in
place; if Torn ever starts publishing expired auctions, filling in the bands from a real `calibrate`
run is all that is needed.

### Is the ledger worth anything? (`coverage`)

```
anon uids        412 of 1,639 resolve (25.1%)   the dead end #12 exists to open up
                   evidence age: median 23d, 88 within a week, oldest 340d
unmatched gear    31 of 96 resolve (32.3%)      on the market, no seller captured yet
tracked gear      74 of 210 resolve (35.2%)     already attributed — a cross-check, not a target
uid turnover:     {'seen_1x': 78412, 'seen_2x': 1204, 'seen_3x': 61}
                  1,265 uid(s) auctioned more than once, median 47.0d apart
```

Reads both databases and makes no API calls. Three things to read off it:

- **the anon line is the ceiling on #12.** An anonymous uid with no AH win behaves exactly as it
  does today, so this is the fraction of the dead-end backlog that could ever produce an alert.
- **the tracked line is a sanity check, not a target.** Those uids already have a captured seller,
  so if the AH-inferred buyer disagrees with the captured seller on many of them, the inference is
  weaker than it looks — a cheap way to catch that before trusting it.
- **turnover sets the staleness cutoff.** If uids typically change hands every few weeks, a
  months-old win is not evidence of current ownership. That number is what a confidence threshold
  should be derived from rather than picked.

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
