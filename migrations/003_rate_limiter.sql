-- Migration 003: SQLite-backed rate limiter
--
-- Replaces the in-memory rate limiter with a durable table so that
-- rate limits survive process restarts and are consistent across
-- potential future multi-worker deployments.

CREATE TABLE IF NOT EXISTS rate_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,           -- e.g. "ip:1.2.3.4" or "email:user@example.com"
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rate_limits_key_ts ON rate_limits(key, ts);

-- Bump schema version
INSERT OR REPLACE INTO schema_version (version) VALUES (3);
