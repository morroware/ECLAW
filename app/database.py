"""SQLite database layer with async access and auto-migration.

Single-worker requirement
~~~~~~~~~~~~~~~~~~~~~~~~~
``_write_lock`` is an ``asyncio.Lock`` that serialises write operations
within a single process.  This is intentional: ECLAW runs as a single
uvicorn worker because GPIO hardware ownership, in-memory state machine
state, and in-memory rate limiting all require a single process.  Do NOT
deploy with multiple workers (gunicorn --workers N or WEB_CONCURRENCY>1).
The startup guard in ``app.main.lifespan`` enforces this at boot.
"""

import asyncio
import hashlib
import logging
import os

import aiosqlite

from app.config import settings

logger = logging.getLogger("database")

_db: aiosqlite.Connection | None = None
_db_lock: asyncio.Lock | None = None
# Single-process write serialisation — see module docstring for rationale.
_write_lock: asyncio.Lock | None = None


def _ensure_locks():
    """Lazily create locks so they bind to the current event loop."""
    global _db_lock, _write_lock
    if _db_lock is None:
        _db_lock = asyncio.Lock()
    if _write_lock is None:
        _write_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    global _db
    _ensure_locks()
    async with _db_lock:
        if _db is None:
            os.makedirs(os.path.dirname(os.path.abspath(settings.database_path)), exist_ok=True)
            _db = await aiosqlite.connect(settings.database_path)
            _db.row_factory = aiosqlite.Row
            await _db.execute("PRAGMA journal_mode=WAL")
            await _db.execute("PRAGMA foreign_keys=ON")
            await _db.execute(f"PRAGMA busy_timeout={settings.db_busy_timeout_ms}")
            await _db.execute("PRAGMA synchronous=NORMAL")
            await _run_migrations(_db)
    return _db


async def close_db():
    global _db, _db_lock, _write_lock
    if _db:
        try:
            await _db.close()
        except Exception:
            logger.exception("Error closing database")
        finally:
            _db = None
            _db_lock = None
            _write_lock = None


async def _run_migrations(db: aiosqlite.Connection):
    """Run any pending SQL migration files."""
    # Check if schema_version table exists
    try:
        async with db.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
            current = row[0] if row and row[0] else 0
    except aiosqlite.OperationalError:
        current = 0

    # Find and apply migrations
    migrations_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "migrations")
    if not os.path.isdir(migrations_dir):
        return

    for fname in sorted(os.listdir(migrations_dir)):
        if not fname.endswith(".sql"):
            continue
        try:
            version = int(fname.split("_")[0])
        except (ValueError, IndexError):
            continue
        if version > current:
            with open(os.path.join(migrations_dir, fname)) as f:
                await db.executescript(f.read())
            await db.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (version,),
            )
            await db.commit()


async def log_event(queue_entry_id: str | None, event_type: str, detail: str | None = None):
    """Log a game event to the database."""
    db = await get_db()
    _ensure_locks()
    async with _write_lock:
        await db.execute(
            "INSERT INTO game_events (queue_entry_id, event_type, detail) VALUES (?, ?, ?)",
            (queue_entry_id, event_type, detail),
        )
        await db.commit()


async def prune_old_entries(retention_hours: int = 48):
    """Delete completed queue entries and game events older than retention_hours.

    Call periodically to prevent unbounded database growth during multi-day demos.
    """
    db = await get_db()
    _ensure_locks()
    async with _write_lock:
        cutoff = f"-{retention_hours} hours"
        # Only delete events belonging to completed/cancelled entries older than
        # the retention window.  Previous code deleted events by created_at alone,
        # which could remove events for entries still in the queue (e.g. a player
        # waiting longer than retention_hours at a multi-day event).
        result_events = await db.execute(
            "DELETE FROM game_events WHERE queue_entry_id IN ("
            "  SELECT id FROM queue_entries"
            "  WHERE state IN ('done', 'cancelled')"
            "  AND completed_at < datetime('now', ?)"
            ")",
            (cutoff,),
        )
        result_entries = await db.execute(
            "DELETE FROM queue_entries WHERE state IN ('done', 'cancelled') "
            "AND completed_at < datetime('now', ?)",
            (cutoff,),
        )
        await db.commit()
        events_deleted = result_events.rowcount
        entries_deleted = result_entries.rowcount
        if events_deleted or entries_deleted:
            logger.info(
                "DB prune: removed %d events, %d completed entries (older than %dh)",
                events_deleted, entries_deleted, retention_hours,
            )


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
