-- Auction house ledger. Applied idempotently by server/ah_db.py.
--
-- This is a SEPARATE SQLite file from mug_bot.db, on purpose. Every statement in the bot process
-- serialises through one aiosqlite connection and the alert path queues on it, so a backfill
-- writing tens of thousands of rows into that file would contend directly with alerts. The scraper
-- is a short-lived cron process with its own connection, so the two never touch.
--
-- The cost of the split is that #12's uid lookup becomes cross-database. That is a read-only point
-- query on an indexed column, so the bot can open a second read-only connection (or ATTACH) when
-- the time comes; it does not justify putting bulk history on the hot connection.

PRAGMA journal_mode = WAL;

-- Completed auctions, one row per Torn auction listing id.
--
-- APPEND-ONLY OBSERVATION LOG, not a current-ownership table. A uid changes hands repeatedly, so
-- "who owns uid X" is always "the most recent observation from any source" — an AH win here, an
-- item-matcher capture in the main DB, or a market vanish. Never read a single row as fact.
CREATE TABLE IF NOT EXISTS auction_wins (
    auction_id  INTEGER PRIMARY KEY,   -- Torn AuctionListingId
    -- TEXT, matching tracked_gear.uid / anon_uids.uid / listings.armoury_uid in the main database,
    -- even though the API returns an integer. SQLite itself would cope either way — TEXT affinity
    -- coerces an inserted integer, and applies to a bound parameter in comparison — so this is not
    -- about the SQL. It is about Python: api_poller normalises every uid with str() and
    -- reconcile_gear classifies them with `uid in tracked` / `in uid_map` / `in anon` against dicts
    -- and sets, where '123' != 123 silently. #12 inserts the AH lookup into exactly that chain, so
    -- the ledger has to speak the same convention as the code that will query it.
    uid         TEXT,
    item_id     INTEGER,
    item_name   TEXT,
    item_type   TEXT,                  -- Weapon / Armor / Drug / ...
    sub_type    TEXT,                  -- Rifle / Pistol / ... (weapons only)
    damage      REAL,                  -- gear stats, for the uid-less fallback match
    accuracy    REAL,
    armor       REAL,
    quality     REAL,
    rarity      TEXT,                  -- yellow / orange / red, else NULL
    bonuses     TEXT,                  -- raw JSON array as returned
    seller_id   INTEGER,
    seller_name TEXT,
    buyer_id    INTEGER NOT NULL,      -- rows without a buyer are dropped at ingest, not stored
    buyer_name  TEXT,
    price       INTEGER,
    bids        INTEGER,
    ended_at    INTEGER NOT NULL,      -- Torn's "timestamp": when the auction ended
    seen_at     INTEGER NOT NULL       -- when WE recorded it (scrape lag is worth being able to see)
);

-- (uid, ended_at) is the #12 lookup: newest win for a uid. The others support the flip-margin
-- analysis (#11 step 3) and "what has this player been buying".
CREATE INDEX IF NOT EXISTS idx_auction_wins_uid   ON auction_wins(uid, ended_at);
CREATE INDEX IF NOT EXISTS idx_auction_wins_item  ON auction_wins(item_id, ended_at);
CREATE INDEX IF NOT EXISTS idx_auction_wins_buyer ON auction_wins(buyer_id, ended_at);
-- MIN/MAX(ended_at) drive both scrape watermarks, so they must not be table scans.
CREATE INDEX IF NOT EXISTS idx_auction_wins_ended ON auction_wins(ended_at);

-- Scraper bookkeeping. Deliberately small: the forward/backfill cursors are derived from
-- MIN/MAX(ended_at) on the table itself rather than stored here, so they cannot drift out of sync
-- with the data they describe. What lives here is only what the table cannot tell us.
CREATE TABLE IF NOT EXISTS ah_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per scrape run. A cron job that quietly stops working is the classic failure mode here —
-- there is no user watching a scraper the way there is one watching alerts stop arriving — so
-- "when did this last succeed, and did it find anything" has to be answerable after the fact.
CREATE TABLE IF NOT EXISTS ah_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mode       TEXT NOT NULL,          -- 'forward' | 'backfill'
    started_at INTEGER NOT NULL,
    ended_at   INTEGER,                -- NULL => the process died mid-run
    pages      INTEGER NOT NULL DEFAULT 0,
    rows_seen  INTEGER NOT NULL DEFAULT 0,
    rows_new   INTEGER NOT NULL DEFAULT 0,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ah_runs_started ON ah_runs(started_at);
