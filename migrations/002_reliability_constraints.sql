-- Migration 002: reliability constraints
--
-- 1. CHECK constraints on queue_entries.state and result to prevent
--    invalid enum-like values from entering the database.
-- 2. Partial unique index ensuring at most one row is in an "active-like"
--    state (ready or active) at any time, enforced at the DB level.
--
-- SQLite does not support ALTER TABLE ... ADD CONSTRAINT, so we recreate
-- the table with the new constraints and copy data over.

-- Step 1: Create the new table with CHECK constraints
CREATE TABLE IF NOT EXISTS queue_entries_new (
    id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    ip_address TEXT,
    state TEXT NOT NULL DEFAULT 'waiting'
        CHECK (state IN ('waiting', 'ready', 'active', 'done', 'cancelled')),
    position INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    activated_at TEXT,
    completed_at TEXT,
    result TEXT
        CHECK (result IS NULL OR result IN ('win', 'loss', 'skipped', 'expired', 'admin_skipped', 'cancelled', 'error')),
    tries_used INTEGER NOT NULL DEFAULT 0,
    try_move_end_at TEXT,
    turn_end_at TEXT
);

-- Step 2: Copy data from old table
INSERT OR IGNORE INTO queue_entries_new
    SELECT * FROM queue_entries;

-- Step 3: Drop old table and rename new one
DROP TABLE IF EXISTS queue_entries;
ALTER TABLE queue_entries_new RENAME TO queue_entries;

-- Step 4: Recreate indexes on the new table
CREATE INDEX IF NOT EXISTS idx_queue_state ON queue_entries(state);
CREATE INDEX IF NOT EXISTS idx_queue_position ON queue_entries(position)
    WHERE state IN ('waiting', 'ready', 'active');
CREATE INDEX IF NOT EXISTS idx_events_entry ON game_events(queue_entry_id);
CREATE INDEX IF NOT EXISTS idx_events_time ON game_events(created_at);

-- Step 5: Partial unique index — at most one row may be in 'ready' or 'active'
-- state at any time.  This catches bugs/races that would otherwise produce
-- multiple active-like rows and confuse queue logic.
CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_single_active
    ON queue_entries(state) WHERE state IN ('ready', 'active');

-- Bump schema version
INSERT OR REPLACE INTO schema_version (version) VALUES (2);
