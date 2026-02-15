"""SQLite database layer with async access and auto-migration."""

import hashlib
import os

import aiosqlite

from app.config import settings

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(os.path.abspath(settings.database_path)), exist_ok=True)
        _db = await aiosqlite.connect(settings.database_path)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await _run_migrations(_db)
    return _db


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


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
    await db.execute(
        "INSERT INTO game_events (queue_entry_id, event_type, detail) VALUES (?, ?, ?)",
        (queue_entry_id, event_type, detail),
    )
    await db.commit()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
