-- Mug Bot SQLite schema. Applied idempotently on startup by server/db.py.

PRAGMA journal_mode = WAL;

-- Phase 0 catch-all: every raw ingest payload, stored untouched, so we can inspect
-- the real WebSocket / gear schema before committing to a parser. Safe to prune later.
CREATE TABLE IF NOT EXISTS raw_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,        -- 'market' | 'status' | 'gear' | 'gear_anon'
    channel     TEXT,                 -- e.g. 'item-market_206' / 'Primary'
    payload     TEXT NOT NULL,        -- raw JSON as received
    received_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_events_source ON raw_events(source, received_at);

-- Sellers seen on the market, enriched on first sight and refreshed as needed.
--   clothing store  <- /v2/user/{id}/job  (job_type_id == 5, rating >= 7 => 75% reduction)
--   status          <- /v2/user/{id}/basic (state + until; drives hospital-accumulation)
--   TBS             <- BSP predictor
CREATE TABLE IF NOT EXISTS sellers (
    user_id             INTEGER PRIMARY KEY,
    name                TEXT,
    -- clothing store (job)
    in_clothing_store   INTEGER,          -- 0 / 1 / NULL(unknown); derived job_type_id==5 && rating>=7
    job_type_id         INTEGER,
    job_rating          INTEGER,
    job_company_name    TEXT,
    clothing_checked_at INTEGER,
    -- status (for hospital accumulation + the embed)
    status_state        TEXT,             -- 'Okay' | 'Hospital' | 'Jail' | 'Traveling' | ...
    status_until        INTEGER,          -- unix when the status ends (e.g. hospital release)
    status_description  TEXT,
    status_checked_at   INTEGER,
    -- battle stats (BSP)
    bsp_total           INTEGER,
    bsp_str             INTEGER,
    bsp_spd             INTEGER,
    bsp_def             INTEGER,
    bsp_dex             INTEGER,
    bsp_fetched_at      INTEGER,
    -- misc profile
    faction             TEXT,
    online_status       TEXT,             -- last seen on a listing: online/offline/idle
    last_action_ts      INTEGER,
    last_alerted_at     INTEGER,          -- mug-alert cooldown / dedupe
    first_seen          INTEGER,
    last_seen           INTEGER
);

-- Current market listings (items + gear), keyed by Torn listingID.
CREATE TABLE IF NOT EXISTS listings (
    listing_id   TEXT PRIMARY KEY,
    item_id      INTEGER,
    item_name    TEXT,
    seller_id    INTEGER,              -- NULL when anonymous / not yet matched
    price        INTEGER,
    available    INTEGER,              -- current quantity remaining
    is_gear      INTEGER NOT NULL DEFAULT 0,
    armoury_uid  TEXT,                 -- gear UID (weapons/armour)
    anonymous    INTEGER NOT NULL DEFAULT 0,
    stats_json   TEXT,                 -- gear stats/bonuses blob
    created_at   INTEGER,
    updated_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_listings_item ON listings(item_id);
CREATE INDEX IF NOT EXISTS idx_listings_seller ON listings(seller_id);

-- Detected sales (a listing's quantity decreased or it disappeared).
CREATE TABLE IF NOT EXISTS sales (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  TEXT,
    item_id     INTEGER,
    item_name   TEXT,
    seller_id   INTEGER,
    qty_sold    INTEGER,
    unit_price  INTEGER,
    total_value INTEGER,
    sold_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_seller ON sales(seller_id, sold_at);

-- Which pages/categories are being actively viewed right now (drives status-tracker).
-- last_ping_at is refreshed by the userscript heartbeat only while a page is actively
-- viewed; staleness => the page is shown red/offline.
CREATE TABLE IF NOT EXISTS monitored_pages (
    page_key     TEXT PRIMARY KEY,     -- item id, item name, or category name
    label        TEXT,
    last_ping_at INTEGER
);

-- API-mode (gear): the poller's live view of each weapon/armour listing, keyed by the API's
-- per-listing uid. A uid present last poll but absent now (for `misses` polls) = a sale, which
-- we attribute to the seller resolved from the item-matcher capture (listings table).
CREATE TABLE IF NOT EXISTS tracked_gear (
    uid        TEXT PRIMARY KEY,
    item_id    INTEGER,
    item_name  TEXT,
    price      INTEGER,
    seller_id  INTEGER,
    stats_json TEXT,
    misses     INTEGER NOT NULL DEFAULT 0,
    seen       INTEGER NOT NULL DEFAULT 1,   -- polls this uid was present (after seller attribution)
    first_seen INTEGER,
    last_seen  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tracked_gear_item ON tracked_gear(item_id);

-- Gear listings the poller has found on the market but we DON'T yet have a seller for. The
-- item-matcher fetches these (/cache/uncached), highlights the matching tiles, and you click
-- through them to capture sellers (matched by uid == armouryID). Drives the 3rd status embed.
CREATE TABLE IF NOT EXISTS unmatched_gear (
    uid       TEXT PRIMARY KEY,
    item_id   INTEGER,
    item_name TEXT,
    price     INTEGER,
    stat      REAL,           -- damage (weapons) or armor (armour)
    accuracy  REAL,
    seen_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_unmatched_gear_item ON unmatched_gear(item_id);

-- uids the item-matcher reported as ANONYMOUS — stashed but ignored (can't mug, so excluded
-- from the unmatched list and never alerted on).
CREATE TABLE IF NOT EXISTS anon_uids (
    uid       TEXT PRIMARY KEY,
    item_name TEXT,
    stat      REAL,
    accuracy  REAL,
    price     INTEGER,
    seen_at   INTEGER
);

-- API-mode (stackable items): the poller's live view keyed by (item_id, price). Items have no
-- uid, so we track each UNIQUE price and watch its quantity. A quantity drop = a confident
-- partial sale by the captured seller; collisions (price shared by 2+ listings) are dropped.
CREATE TABLE IF NOT EXISTS tracked_items (
    item_id     INTEGER NOT NULL,
    price       INTEGER NOT NULL,
    item_name   TEXT,
    seller_id   INTEGER,
    last_amount INTEGER,
    misses      INTEGER NOT NULL DEFAULT 0,
    first_seen  INTEGER,
    last_seen   INTEGER,
    PRIMARY KEY (item_id, price)
);

-- Which items the API poller watches. Managed via /watch (add/remove/enable/disable) and
-- auto-added when the item-matcher captures a seller for a new item. is_gear NULL = unknown
-- (the poller detects it from the API on first poll: gear listings carry item_details.uid).
CREATE TABLE IF NOT EXISTS watchlist (
    item_id       INTEGER PRIMARY KEY,
    label         TEXT,
    is_gear       INTEGER,
    category      TEXT,           -- API item.type, e.g. "Energy Drink" (for the item-matcher link)
    enabled       INTEGER NOT NULL DEFAULT 1,
    added_at      INTEGER,
    last_polled_at INTEGER,
    last_count    INTEGER
);

-- Torn API keys the poller/enrichment rotate over. Managed via /keys; seeded from TORN_API_KEYS
-- on first run. A key that returns a key-level error (incorrect/disabled/inactive/low-access) is
-- auto-disabled and a notice is posted to the status channel.
CREATE TABLE IF NOT EXISTS api_keys (
    key           TEXT PRIMARY KEY,
    user_id       INTEGER,
    name          TEXT,
    level         INTEGER,
    enabled       INTEGER NOT NULL DEFAULT 1,
    added_at      INTEGER,
    last_ok_at    INTEGER,
    last_error    TEXT,
    last_error_at INTEGER
);

-- Runtime-tunable thresholds (seeded from config.py; editable via Discord commands).
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Item-market reference values (one Torn API call for every item), refreshed every ~4h and
-- used by the deal-finder to alert when a watched item is listed far below market.
CREATE TABLE IF NOT EXISTS item_values (
    item_id      INTEGER PRIMARY KEY,
    name         TEXT,
    type         TEXT,            -- Torn item type: Primary/Secondary/Melee/Defensive/Drug/...
    market_value INTEGER,
    fetched_at   INTEGER
);

-- Deals already announced to the items channel, so a restart doesn't re-alert (and re-ping) every
-- standing under-market listing. Kept in step with the in-memory set in deal_finder: a price that
-- leaves the market is removed here too, so it can alert again if it comes back cheap.
CREATE TABLE IF NOT EXISTS alerted_deals (
    item_id    INTEGER NOT NULL,
    price      INTEGER NOT NULL,
    alerted_at INTEGER NOT NULL,
    PRIMARY KEY (item_id, price)
);

-- AH-inferred owners for uids we cannot otherwise attribute (anonymous listings, and gear the
-- item-matcher has not captured yet). Written by the ah_scrape.py `match` cron step, read by the
-- poller.
--
-- PRECOMPUTED ON PURPOSE. The ledger is a separate database file, and reconcile_gear runs per item
-- per poll on the connection the alert path also queues on — so resolving each uid live would put a
-- cross-database read on the hot path. The match step does it out of process, on a cron, and leaves
-- an indexed local lookup behind.
--
-- INFERRED, NOT OBSERVED. `confidence` is the empirically measured probability that the AH buyer is
-- still the seller, given how old the evidence is (see `ah_scrape.py calibrate`). Direct evidence
-- always beats this: a uid the item-matcher captures a seller for must have its row dropped.
CREATE TABLE IF NOT EXISTS anon_matches (
    uid         TEXT PRIMARY KEY,
    buyer_id    INTEGER NOT NULL,
    buyer_name  TEXT,
    auction_id  INTEGER,
    price       INTEGER,          -- what they paid at auction; shown as alert provenance
    won_at      INTEGER NOT NULL,
    age_days    INTEGER,          -- evidence age when matched, so the alert can show it
    confidence  REAL,             -- measured P(correct) for that age; NOT a guess
    matched_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anon_matches_buyer ON anon_matches(buyer_id);
CREATE INDEX IF NOT EXISTS idx_anon_matches_conf  ON anon_matches(confidence);
