"""Queue management — CRUD operations for the player queue."""

import json
import secrets
import uuid
from datetime import datetime, timezone

from app.database import get_db, hash_token, log_event
import app.database as _db_mod


class QueueManager:
    async def join(self, name: str, email: str, ip: str) -> dict:
        """Add a user to the queue. Returns {id, token, position}.

        Raises ValueError if the email already has an active queue entry.
        """
        db = await get_db()
        entry_id = str(uuid.uuid4())
        raw_token = secrets.token_urlsafe(32)
        token_h = hash_token(raw_token)

        async with _db_mod._write_lock:
            # Prevent the same player from joining while already in the queue
            async with db.execute(
                "SELECT id FROM queue_entries WHERE email = ? AND state IN ('waiting', 'ready', 'active')",
                (email,),
            ) as cur:
                if await cur.fetchone():
                    raise ValueError("You already have an active queue entry")

            # Atomic position assignment: INSERT with subquery in a single statement.
            # Use all non-terminal states for MAX(position) so positions never collide
            # when the first waiting player advances to ready/active.
            await db.execute(
                """INSERT INTO queue_entries (id, token_hash, name, email, ip_address, state, position)
                   VALUES (?, ?, ?, ?, ?, 'waiting',
                           COALESCE((SELECT MAX(position) FROM queue_entries
                                     WHERE state IN ('waiting', 'ready', 'active')), 0) + 1)""",
                (entry_id, token_h, name, email, ip),
            )
            await db.commit()

        # Read back the assigned position
        async with db.execute(
            "SELECT position FROM queue_entries WHERE id = ?", (entry_id,)
        ) as cur:
            row = await cur.fetchone()
            next_pos = row[0]

        await log_event(entry_id, "join", json.dumps({"name": name, "position": next_pos}))

        return {"id": entry_id, "token": raw_token, "position": next_pos}

    async def leave(self, token_hash: str) -> bool:
        """Cancel a waiting/ready player's queue entry. Returns False if no matching entry."""
        db = await get_db()

        async with _db_mod._write_lock:
            # Find entry first for logging
            async with db.execute(
                "SELECT id FROM queue_entries WHERE token_hash = ? AND state IN ('waiting', 'ready')",
                (token_hash,),
            ) as cur:
                row = await cur.fetchone()
                entry_id = row[0] if row else None

            if not entry_id:
                return False

            await db.execute(
                "UPDATE queue_entries SET state = 'cancelled', completed_at = datetime('now') "
                "WHERE token_hash = ? AND state IN ('waiting', 'ready')",
                (token_hash,),
            )
            await db.commit()

        await log_event(entry_id, "leave")
        return True

    async def peek_next_waiting(self) -> dict | None:
        """Get the next player in the waiting queue."""
        db = await get_db()
        async with db.execute(
            "SELECT * FROM queue_entries WHERE state = 'waiting' ORDER BY position ASC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def set_state(self, entry_id: str, state: str):
        """Update a queue entry's state."""
        db = await get_db()
        activated_at = None
        if state == "active":
            activated_at = datetime.now(timezone.utc).isoformat()
        async with _db_mod._write_lock:
            await db.execute(
                "UPDATE queue_entries SET state = ?, activated_at = COALESCE(?, activated_at) WHERE id = ?",
                (state, activated_at, entry_id),
            )
            await db.commit()

        await log_event(entry_id, f"state_{state}")

    async def complete_entry(self, entry_id: str, result: str, tries_used: int):
        """Mark a queue entry as done with a result."""
        db = await get_db()
        async with _db_mod._write_lock:
            await db.execute(
                "UPDATE queue_entries SET state = 'done', result = ?, tries_used = ?, "
                "completed_at = datetime('now') WHERE id = ?",
                (result, tries_used, entry_id),
            )
            await db.commit()

        await log_event(entry_id, "turn_end", json.dumps({"result": result, "tries": tries_used}))

    async def get_by_token(self, token_hash: str) -> dict | None:
        """Look up a queue entry by token hash."""
        db = await get_db()
        async with db.execute(
            "SELECT * FROM queue_entries WHERE token_hash = ?", (token_hash,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_by_id(self, entry_id: str) -> dict | None:
        """Look up a queue entry by ID."""
        db = await get_db()
        async with db.execute(
            "SELECT * FROM queue_entries WHERE id = ?", (entry_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_queue_status(self) -> dict:
        """Get current queue stats for broadcasting."""
        db = await get_db()
        async with db.execute(
            "SELECT COUNT(*) FROM queue_entries WHERE state = 'waiting'"
        ) as cur:
            waiting = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT name, state FROM queue_entries WHERE state IN ('active', 'ready') LIMIT 1"
        ) as cur:
            active_row = await cur.fetchone()
        return {
            "queue_length": waiting,
            "current_player": dict(active_row)["name"] if active_row else None,
            "current_player_state": dict(active_row)["state"] if active_row else None,
        }

    async def cleanup_stale(self, grace_seconds: int):
        """Called on startup. Expire entries left over from a previous session.

        - 'active' entries older than grace_seconds are expired.
        - 'ready' entries are always expired on restart (their WebSocket is gone
          so they can never confirm readiness).
        Both are transitioned to state='done' with result='expired' so they
        appear correctly in history and are not orphaned.
        """
        db = await get_db()
        async with _db_mod._write_lock:
            await db.execute(
                "UPDATE queue_entries SET state = 'done', result = 'expired', "
                "completed_at = COALESCE(completed_at, datetime('now')) "
                "WHERE state = 'active' AND activated_at IS NOT NULL "
                "AND (julianday('now') - julianday(activated_at)) * 86400 > ?",
                (grace_seconds,),
            )
            await db.execute(
                "UPDATE queue_entries SET state = 'done', result = 'expired', "
                "completed_at = COALESCE(completed_at, datetime('now')) "
                "WHERE state = 'ready'"
            )
            await db.commit()

    async def list_queue(self) -> list[dict]:
        """Return all active queue entries (waiting, ready, active) ordered by position."""
        db = await get_db()
        async with db.execute(
            "SELECT id, name, state, position, created_at "
            "FROM queue_entries WHERE state IN ('waiting', 'ready', 'active') "
            "ORDER BY CASE state "
            "  WHEN 'active' THEN 0 WHEN 'ready' THEN 1 WHEN 'waiting' THEN 2 END, "
            "position ASC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def list_queue_admin(self) -> list[dict]:
        """Return all active queue entries with admin-visible fields.

        Includes email and ip_address which are excluded from the public
        list_queue() for privacy.
        """
        db = await get_db()
        async with db.execute(
            "SELECT id, name, email, ip_address, state, position, created_at "
            "FROM queue_entries WHERE state IN ('waiting', 'ready', 'active') "
            "ORDER BY CASE state "
            "  WHEN 'active' THEN 0 WHEN 'ready' THEN 1 WHEN 'waiting' THEN 2 END, "
            "position ASC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_waiting_rank(self, entry_id: str) -> int:
        """Return the 1-based rank of an entry among active queue entries.

        Rank counts how many entries with state IN ('waiting', 'ready',
        'active') have a position <= this entry's position.  For wait
        estimation, subtract 1 to get the number of people *ahead*.
        """
        db = await get_db()
        async with db.execute(
            "SELECT COUNT(*) FROM queue_entries "
            "WHERE state IN ('waiting', 'ready', 'active') "
            "AND position <= (SELECT position FROM queue_entries WHERE id = ?)",
            (entry_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_recent_results(self, limit: int = 10) -> list[dict]:
        """Return the most recent completed turns for the history feed."""
        db = await get_db()
        async with db.execute(
            "SELECT name, result, tries_used, completed_at "
            "FROM queue_entries WHERE state = 'done' AND result IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_stats(self) -> dict:
        """Return aggregate statistics for the admin dashboard."""
        db = await get_db()
        stats = {}
        async with db.execute(
            "SELECT COUNT(*) FROM queue_entries WHERE state = 'waiting'"
        ) as cur:
            stats["waiting"] = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM queue_entries WHERE state IN ('active', 'ready')"
        ) as cur:
            stats["active"] = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM queue_entries WHERE state = 'done'"
        ) as cur:
            stats["total_completed"] = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM queue_entries WHERE state = 'done' AND result = 'win'"
        ) as cur:
            stats["total_wins"] = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM queue_entries"
        ) as cur:
            stats["total_entries"] = (await cur.fetchone())[0]
        return stats

    async def get_waiting_count(self) -> int:
        """Get count of waiting players."""
        db = await get_db()
        async with db.execute(
            "SELECT COUNT(*) FROM queue_entries WHERE state = 'waiting'"
        ) as cur:
            return (await cur.fetchone())[0]
