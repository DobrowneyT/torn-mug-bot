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
    -- NULL = the auction ENDED UNSOLD. Kept rather than dropped: it transfers no ownership, so it
    -- is useless for #12, but it is real market signal (a reserve nobody met). Every ownership
    -- lookup filters it out — see owner_of_uid and wins_for_uids.
    --
    -- Measured 2026-08-03: these are RARE. At most 293 across 553,907 rows (0.05%), and some of
    -- those are 2013 seed data. Modern Torn evidently does not publish expired auctions, so do not
    -- expect this column to support any analysis on its own.
    buyer_id    INTEGER,
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

-- Bonuses, normalised for searching. DERIVED — auction_wins.bonuses stays the source of truth and
-- this is a rebuildable index over it, so if the shape turns out wrong it can be dropped and
-- regenerated rather than migrated.
--
-- Why it exists: the price-checker's central query is "items like this one", which means bonus id
-- AND a value range. Answering that from the JSON needs json_each() over half a million rows per
-- request, with no index available, and twice over for the two-bonus case.
CREATE TABLE IF NOT EXISTS auction_bonuses (
    auction_id INTEGER NOT NULL,
    bonus_id   INTEGER NOT NULL,
    -- NULL for bonuses that are flags with no magnitude. A range filter must not match those:
    -- "unknown" is not "zero", and treating it as zero would quietly widen every search.
    value      INTEGER,
    ordinal    INTEGER NOT NULL DEFAULT 0,   -- position within the item's bonus array
    PRIMARY KEY (auction_id, bonus_id, ordinal)
);
-- The lookup the search endpoint makes: given a bonus and a value window, which auctions qualify.
CREATE INDEX IF NOT EXISTS idx_auction_bonuses_lookup ON auction_bonuses(bonus_id, value);
CREATE INDEX IF NOT EXISTS idx_auction_bonuses_auction ON auction_bonuses(auction_id);
