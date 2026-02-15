CREATE TABLE IF NOT EXISTS queue_entries (
    id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    ip_address TEXT,
    state TEXT NOT NULL DEFAULT 'waiting',
    position INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    activated_at TEXT,
    completed_at TEXT,
    result TEXT,
    tries_used INTEGER NOT NULL DEFAULT 0,
    try_move_end_at TEXT,
    turn_end_at TEXT
);

CREATE TABLE IF NOT EXISTS game_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_entry_id TEXT REFERENCES queue_entries(id),
    event_type TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);

CREATE INDEX IF NOT EXISTS idx_queue_state ON queue_entries(state);
CREATE INDEX IF NOT EXISTS idx_queue_position ON queue_entries(position)
    WHERE state IN ('waiting', 'ready', 'active');
CREATE INDEX IF NOT EXISTS idx_events_entry ON game_events(queue_entry_id);
CREATE INDEX IF NOT EXISTS idx_events_time ON game_events(created_at);
