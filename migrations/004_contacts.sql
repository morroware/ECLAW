-- Migration 004: Permanent contacts table for CRM/marketing export.
--
-- Stores first_name, last_name, email for every user who joins the queue.
-- Independent of queue_entries — NOT affected by prune_old_entries().
-- Deduplicated by email: re-joins update the name but don't create duplicates.

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR REPLACE INTO schema_version (version) VALUES (4);
