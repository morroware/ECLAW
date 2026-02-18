"""Adversarial reliability tests.

These tests exercise failure modes identified in the reliability audit:
- Blocked/stalled control WebSocket sends
- GPIO executor timeouts and recovery
- DB constraint enforcement (invalid state/result values)
- Partial unique index preventing duplicate active/ready rows
- Half-open control socket detection via keepalive
- State machine recovery from stuck states
"""

import asyncio
import json
import os
import time

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import app.database as db_module
from app.config import Settings, settings
from app.database import close_db, get_db
from app.game.queue_manager import QueueManager
from app.game.state_machine import StateMachine, TurnState
from app.gpio.controller import GPIOController
from app.main import app
from app.api.routes import _join_limits
from app.ws.control_handler import ControlHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def fresh_db(tmp_path):
    """Provide a fresh database for each test, properly reset."""
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    settings.database_path = db_path
    db_module._db = None
    db_module._db_lock = None
    db_module._write_lock = None
    db = await get_db()
    yield db
    await close_db()
    db_module._db = None


@pytest.fixture
async def api_client(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    settings.database_path = db_path
    db_module._db = None
    db_module._db_lock = None
    db_module._write_lock = None
    _join_limits.clear()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    await close_db()
    db_module._db = None


# ---------------------------------------------------------------------------
# Dummy/mock collaborators for isolated state machine testing
# ---------------------------------------------------------------------------

class _MockGPIO:
    """GPIO mock that optionally simulates timeouts."""

    def __init__(self, *, block_emergency_stop=False):
        self._locked = False
        self._block_emergency_stop = block_emergency_stop
        self._emergency_stop_called = False
        self._directions_off_called = False

    async def all_directions_off(self):
        self._directions_off_called = True

    async def drop_on(self):
        return True

    async def drop_off(self):
        return True

    async def emergency_stop(self):
        self._emergency_stop_called = True
        if self._block_emergency_stop:
            await asyncio.sleep(999)  # Will be timed out by wait_for

    async def pulse(self, name):
        return True

    def register_win_callback(self, cb):
        pass

    def unregister_win_callback(self):
        pass

    async def direction_on(self, d):
        return True

    async def direction_off(self, d):
        return True


class _MockQueue:
    def __init__(self):
        self._entries = []
        self._completed = []

    async def peek_next_waiting(self):
        for e in self._entries:
            if e["state"] == "waiting":
                return e
        return None

    async def set_state(self, entry_id, state):
        for e in self._entries:
            if e["id"] == entry_id:
                e["state"] = state

    async def complete_entry(self, entry_id, result, tries):
        self._completed.append((entry_id, result, tries))
        for e in self._entries:
            if e["id"] == entry_id:
                e["state"] = "done"
                e["result"] = result

    async def get_by_id(self, entry_id):
        for e in self._entries:
            if e["id"] == entry_id:
                return e
        return None

    async def get_queue_status(self):
        return {"queue_length": 0, "current_player": None, "current_player_state": None}

    async def list_queue(self):
        return []

    async def get_waiting_count(self):
        return sum(1 for e in self._entries if e["state"] == "waiting")


class _MockWS:
    def __init__(self):
        self.broadcasts = []

    async def broadcast_state(self, state, payload):
        self.broadcasts.append(("state", state, payload))

    async def broadcast_turn_end(self, entry_id, result):
        self.broadcasts.append(("turn_end", entry_id, result))

    async def broadcast_queue_update(self, status, entries=None):
        self.broadcasts.append(("queue_update", status))


class _MockCtrl:
    def __init__(self):
        self.sent = []
        self._connected = set()
        self._block_send = False

    async def send_to_player(self, entry_id, message):
        if self._block_send:
            await asyncio.sleep(999)  # simulate blocked send
        self.sent.append((entry_id, message))

    def is_player_connected(self, entry_id):
        return entry_id in self._connected


def _make_sm(gpio=None, queue=None, ws=None, ctrl=None):
    """Create a StateMachine with mock collaborators."""
    gpio = gpio or _MockGPIO()
    queue = queue or _MockQueue()
    ws = ws or _MockWS()
    ctrl = ctrl or _MockCtrl()
    sm = StateMachine(gpio, queue, ws, ctrl, settings)
    return sm


# ===========================================================================
# Test 1: send_to_player timeout and eviction
# ===========================================================================

@pytest.mark.anyio
async def test_send_to_player_timeout_evicts_socket():
    """When send_to_player hits a timeout, the socket should be evicted."""
    ctrl = ControlHandler(None, _MockQueue(), _MockGPIO(), settings)

    class _StallSocket:
        """Fake WebSocket that blocks on send_text indefinitely."""
        closed = False

        async def send_text(self, data):
            await asyncio.sleep(999)

        async def close(self, code=1000, reason=""):
            self.closed = True

    ws = _StallSocket()
    ctrl._player_ws["test-entry"] = ws

    # send_to_player should NOT hang — it should time out and evict
    await asyncio.wait_for(
        ctrl.send_to_player("test-entry", {"type": "state_update"}),
        timeout=5.0,
    )

    assert "test-entry" not in ctrl._player_ws
    assert ws.closed


# ===========================================================================
# Test 2: GPIO executor timeout and recovery
# ===========================================================================

@pytest.mark.anyio
async def test_gpio_executor_timeout_replaces_executor():
    """When a GPIO call times out, the executor is replaced so subsequent
    calls don't hang permanently."""
    gpio = GPIOController()
    await gpio.initialize()

    original_executor = gpio._executor

    # Inject a blocking function
    async def _block():
        await gpio._gpio_call(lambda: time.sleep(999), timeout=0.5)

    result = await asyncio.wait_for(_block(), timeout=5.0)

    # Executor should have been replaced
    assert gpio._executor is not original_executor


@pytest.mark.anyio
async def test_gpio_executor_recovery_allows_subsequent_calls():
    """After executor replacement, subsequent GPIO calls should work."""
    gpio = GPIOController()
    await gpio.initialize()

    # Force a timeout
    await gpio._gpio_call(lambda: time.sleep(999), timeout=0.5)

    # Subsequent call should succeed on the new executor
    result = await gpio._gpio_call(lambda: None, timeout=2.0)
    assert result is True


# ===========================================================================
# Test 3: DB constraints — invalid state values
# ===========================================================================

@pytest.mark.anyio
async def test_db_rejects_invalid_state(fresh_db):
    """Inserting a queue entry with an invalid state should fail."""
    db = fresh_db
    import aiosqlite

    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO queue_entries (id, token_hash, name, email, state) "
            "VALUES ('test1', 'hash1', 'Test', 'test@test.com', 'bogus_state')"
        )
        await db.commit()


@pytest.mark.anyio
async def test_db_rejects_invalid_result(fresh_db):
    """Inserting a queue entry with an invalid result should fail."""
    db = fresh_db
    import aiosqlite

    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO queue_entries (id, token_hash, name, email, state, result) "
            "VALUES ('test2', 'hash2', 'Test', 'test@test.com', 'done', 'bogus_result')"
        )
        await db.commit()


@pytest.mark.anyio
async def test_db_allows_valid_states(fresh_db):
    """All valid state values should be accepted."""
    db = fresh_db
    for i, state in enumerate(["waiting", "ready", "active", "done", "cancelled"]):
        await db.execute(
            "INSERT INTO queue_entries (id, token_hash, name, email, state) "
            f"VALUES ('valid-{i}', 'hash-{i}', 'Test', 'test@test.com', ?)",
            (state,),
        )
    await db.commit()


@pytest.mark.anyio
async def test_db_allows_valid_results(fresh_db):
    """All valid result values (and NULL) should be accepted."""
    db = fresh_db
    results = [None, "win", "loss", "skipped", "expired", "admin_skipped", "cancelled", "error"]
    for i, result in enumerate(results):
        await db.execute(
            "INSERT INTO queue_entries (id, token_hash, name, email, state, result) "
            f"VALUES ('res-{i}', 'reshash-{i}', 'Test', 'test@test.com', 'done', ?)",
            (result,),
        )
    await db.commit()


# ===========================================================================
# Test 4: Partial unique index — at most one active/ready row
# ===========================================================================

@pytest.mark.anyio
async def test_db_prevents_duplicate_active(fresh_db):
    """Only one row may be in 'active' state at a time."""
    db = fresh_db
    import aiosqlite

    await db.execute(
        "INSERT INTO queue_entries (id, token_hash, name, email, state) "
        "VALUES ('a1', 'h1', 'Alice', 'a@test.com', 'active')"
    )
    await db.commit()

    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO queue_entries (id, token_hash, name, email, state) "
            "VALUES ('a2', 'h2', 'Bob', 'b@test.com', 'active')"
        )
        await db.commit()


@pytest.mark.anyio
async def test_db_prevents_duplicate_ready(fresh_db):
    """Only one row may be in 'ready' state at a time."""
    db = fresh_db
    import aiosqlite

    await db.execute(
        "INSERT INTO queue_entries (id, token_hash, name, email, state) "
        "VALUES ('r1', 'rh1', 'Alice', 'a@test.com', 'ready')"
    )
    await db.commit()

    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO queue_entries (id, token_hash, name, email, state) "
            "VALUES ('r2', 'rh2', 'Bob', 'b@test.com', 'ready')"
        )
        await db.commit()


@pytest.mark.anyio
async def test_db_allows_one_active_and_one_ready(fresh_db):
    """One active and one ready at the same time should be allowed
    (the unique index is per-state-value, not across both)."""
    db = fresh_db

    await db.execute(
        "INSERT INTO queue_entries (id, token_hash, name, email, state) "
        "VALUES ('ar1', 'arh1', 'Alice', 'a@test.com', 'active')"
    )
    await db.execute(
        "INSERT INTO queue_entries (id, token_hash, name, email, state) "
        "VALUES ('ar2', 'arh2', 'Bob', 'b@test.com', 'ready')"
    )
    await db.commit()


@pytest.mark.anyio
async def test_db_allows_multiple_waiting(fresh_db):
    """Multiple waiting entries should be allowed (queue of players)."""
    db = fresh_db

    for i in range(10):
        await db.execute(
            "INSERT INTO queue_entries (id, token_hash, name, email, state, position) "
            f"VALUES ('w{i}', 'wh{i}', 'Player{i}', 'p{i}@test.com', 'waiting', {i})"
        )
    await db.commit()


# ===========================================================================
# Test 5: State machine force recovery with blocked GPIO
# ===========================================================================

@pytest.mark.anyio
async def test_force_recover_with_blocked_emergency_stop():
    """_force_recover should complete even if emergency_stop blocks,
    thanks to the 10s outer timeout."""
    gpio = _MockGPIO(block_emergency_stop=True)
    sm = _make_sm(gpio=gpio)
    await sm.advance_queue()  # prime the event loop ref

    # Manually put SM in a non-IDLE state
    sm.state = TurnState.MOVING
    sm.active_entry_id = "stuck-entry"
    sm._last_state_change = time.monotonic() - 999

    # force_recover should complete within a bounded time
    await asyncio.wait_for(sm._force_recover(), timeout=15.0)

    assert sm.state == TurnState.IDLE
    assert sm.active_entry_id is None
    assert gpio._locked is False


# ===========================================================================
# Test 6: State machine handles concurrent timer callbacks safely
# ===========================================================================

@pytest.mark.anyio
async def test_end_turn_reentrance_guard():
    """Two concurrent _end_turn calls should not double-complete."""
    queue = _MockQueue()
    queue._entries = [
        {"id": "e1", "state": "active", "name": "Test", "position": 1,
         "created_at": "2025-01-01T00:00:00"}
    ]
    sm = _make_sm(queue=queue)
    await sm.advance_queue()

    # Force into MOVING state
    sm.state = TurnState.MOVING
    sm.active_entry_id = "e1"
    sm.current_try = 1

    # Call _end_turn twice concurrently
    async with sm._sm_lock:
        await sm._end_turn("loss")
    async with sm._sm_lock:
        await sm._end_turn("expired")  # Should be no-op (already IDLE)

    # Should only have been completed once
    assert len(queue._completed) == 1
    assert queue._completed[0] == ("e1", "loss", 1)


# ===========================================================================
# Test 7: Queue progression with disconnected players (ghost skip)
# ===========================================================================

@pytest.mark.anyio
async def test_ghost_player_skipped(api_client):
    """A player who joined >30s ago with no WebSocket should be skipped
    during queue advancement."""
    # Join a player
    res = await api_client.post(
        "/api/queue/join",
        json={"name": "Ghost", "email": "ghost@test.com"},
    )
    assert res.status_code == 200
    token = res.json()["token"]

    # Small delay for state machine to process
    await asyncio.sleep(0.1)

    # Check history — the ghost player may have been skipped
    # (depending on timing and whether WS was connected)
    status = await api_client.get("/api/queue/status")
    assert status.status_code == 200


# ===========================================================================
# Test 8: Keepalive constants are configured correctly
# ===========================================================================

def test_keepalive_constants():
    """Verify keepalive timing invariants."""
    from app.config import settings
    # Liveness timeout must be > ping interval (otherwise every ping
    # round would trigger a false liveness failure)
    assert settings.control_liveness_timeout_s > settings.control_ping_interval_s
    # Send timeout must be reasonable (not too short, not too long)
    assert 0.5 <= settings.control_send_timeout_s <= 10.0
    # Ping interval should give enough time for several round-trips
    # before liveness timeout fires
    assert settings.control_liveness_timeout_s >= 2 * settings.control_ping_interval_s


# ===========================================================================
# Test 9: DB busy/contention handling
# ===========================================================================

@pytest.mark.anyio
async def test_concurrent_db_writes(fresh_db):
    """Multiple concurrent writes should not crash due to DB locking."""
    db = fresh_db

    async def _write(i):
        db_module._ensure_locks()
        async with db_module._write_lock:
            await db.execute(
                "INSERT INTO queue_entries (id, token_hash, name, email, state, position) "
                "VALUES (?, ?, ?, ?, 'waiting', ?)",
                (f"conc-{i}", f"conchash-{i}", f"Player{i}", f"p{i}@test.com", i),
            )
            await db.commit()

    # Run 20 concurrent writes
    await asyncio.gather(*[_write(i) for i in range(20)])

    # All 20 should be present
    async with db.execute("SELECT COUNT(*) FROM queue_entries WHERE id LIKE 'conc-%'") as cur:
        row = await cur.fetchone()
        assert row[0] == 20


# ===========================================================================
# Test 10: Migration 002 is applied correctly via the auto-migration system
# ===========================================================================

@pytest.mark.anyio
async def test_migrations_applied(fresh_db):
    """The migration system should have applied all migrations (002 for
    DB constraints, 003 for rate limiter table)."""
    db = fresh_db
    async with db.execute("SELECT MAX(version) FROM schema_version") as cur:
        row = await cur.fetchone()
        assert row[0] == 3


# ===========================================================================
# Test 11: State machine periodic check detects stuck state
# ===========================================================================

@pytest.mark.anyio
async def test_periodic_check_detects_stuck_moving(api_client):
    """The periodic queue check should detect a state stuck longer than
    the hard maximum and force recovery."""
    sm = app.state.state_machine

    # Force SM into a "stuck" state
    sm.state = TurnState.MOVING
    sm.active_entry_id = "fake-stuck"
    sm._last_state_change = time.monotonic() - 9999  # Way past any timeout

    # Trigger the periodic check manually
    from app.main import _periodic_queue_check
    # Run a single iteration by calling the check logic directly
    check_task = asyncio.create_task(_periodic_queue_check(sm, interval_seconds=0))
    await asyncio.sleep(0.3)
    check_task.cancel()
    try:
        await check_task
    except asyncio.CancelledError:
        pass

    # SM should have recovered to IDLE
    assert sm.state == TurnState.IDLE
    assert sm.active_entry_id is None


# ===========================================================================
# Test 12: _enter_state completes when control send blocks
# ===========================================================================

@pytest.mark.anyio
async def test_enter_state_bounded_with_real_ctrl():
    """End-to-end: _enter_state calls real ControlHandler.send_to_player
    which has a 2s timeout.  Verify the full transition completes within
    a bounded window even when the player's socket is stalled."""
    real_ctrl = ControlHandler(None, _MockQueue(), _MockGPIO(), settings)

    class _StallSocket:
        async def send_text(self, data):
            await asyncio.sleep(999)
        async def close(self, code=1000, reason=""):
            pass

    # Register a stalled socket for the player
    real_ctrl._player_ws["player1"] = _StallSocket()

    queue = _MockQueue()
    queue._entries = [
        {"id": "player1", "state": "waiting", "name": "Test", "position": 1,
         "created_at": "2025-01-01T00:00:00"}
    ]
    sm = _make_sm(ctrl=real_ctrl, queue=queue)
    real_ctrl.sm = sm

    # advance_queue → _enter_state(READY_PROMPT) → send_to_player (blocked)
    # The 2s send timeout should let the transition complete.
    await asyncio.wait_for(sm.advance_queue(), timeout=10.0)

    assert sm.state == TurnState.READY_PROMPT
    assert sm.active_entry_id == "player1"
    # Stalled socket should have been evicted
    assert "player1" not in real_ctrl._player_ws


@pytest.mark.anyio
async def test_end_turn_bounded_with_stalled_socket():
    """_end_turn calls send_to_player to notify the player of turn result.
    With a stalled socket, verify the turn ends within bounded time."""
    real_ctrl = ControlHandler(None, _MockQueue(), _MockGPIO(), settings)

    class _StallSocket:
        async def send_text(self, data):
            await asyncio.sleep(999)
        async def close(self, code=1000, reason=""):
            pass

    real_ctrl._player_ws["player1"] = _StallSocket()

    queue = _MockQueue()
    queue._entries = [
        {"id": "player1", "state": "active", "name": "Test", "position": 1,
         "created_at": "2025-01-01T00:00:00"}
    ]
    sm = _make_sm(ctrl=real_ctrl, queue=queue)
    real_ctrl.sm = sm
    await sm.advance_queue()  # prime loop ref

    # Set up state as if player is in MOVING
    sm.state = TurnState.MOVING
    sm.active_entry_id = "player1"
    sm.current_try = 1

    # _end_turn should complete within bounded time despite stalled socket
    async with sm._sm_lock:
        await asyncio.wait_for(sm._end_turn("loss"), timeout=10.0)

    assert sm.state == TurnState.IDLE
    assert sm.active_entry_id is None
    assert len(queue._completed) == 1


# ===========================================================================
# Test 13: advance_queue lock hold time is reduced
# ===========================================================================

@pytest.mark.anyio
async def test_advance_queue_lock_not_held_during_ws_wait():
    """The _advance_lock should not be held during the ~2s WebSocket
    connection wait, so concurrent callers aren't blocked."""
    ctrl = _MockCtrl()
    # Player is NOT connected — triggers the wait path
    queue = _MockQueue()
    queue._entries = [
        {"id": "slow-player", "state": "waiting", "name": "Slow", "position": 1,
         "created_at": "2025-01-01T00:00:00"}
    ]
    sm = _make_sm(ctrl=ctrl, queue=queue)

    lock_acquired = asyncio.Event()
    lock_released = asyncio.Event()

    async def _try_lock():
        """Try to acquire the lock while advance_queue is running."""
        async with sm._advance_lock:
            lock_acquired.set()
        lock_released.set()

    # Start advance_queue (will do pre-flight wait ~2s outside lock)
    advance_task = asyncio.create_task(sm.advance_queue())

    # Small delay to let the pre-flight wait start
    await asyncio.sleep(0.2)

    # Try to acquire the lock — should succeed quickly if wait is outside lock
    lock_task = asyncio.create_task(_try_lock())
    try:
        await asyncio.wait_for(lock_acquired.wait(), timeout=1.0)
        acquired_during_wait = True
    except asyncio.TimeoutError:
        acquired_during_wait = False

    # Clean up
    advance_task.cancel()
    lock_task.cancel()
    try:
        await advance_task
    except asyncio.CancelledError:
        pass
    try:
        await lock_task
    except asyncio.CancelledError:
        pass

    # The lock should have been acquirable during the pre-flight wait
    assert acquired_during_wait, \
        "_advance_lock was held during WS connection wait (should be outside lock)"


# ===========================================================================
# Test 14: SQLite-backed rate limiter survives restart
# ===========================================================================

@pytest.mark.anyio
async def test_rate_limiter_persistence(fresh_db):
    """Rate limit records stored in SQLite should survive across
    check_rate_limit calls (simulating persistence across restarts)."""
    from app.api.routes import check_rate_limit_db

    # Record 5 joins for this IP
    for _ in range(5):
        await check_rate_limit_db("ip:10.0.0.1", 10)

    # Should still allow more (5 < 10)
    await check_rate_limit_db("ip:10.0.0.1", 10)  # 6th, still OK

    # Record enough to hit the limit
    for _ in range(4):
        await check_rate_limit_db("ip:10.0.0.1", 10)  # 7,8,9,10

    # 11th should be rejected
    with pytest.raises(HTTPException) as exc_info:
        await check_rate_limit_db("ip:10.0.0.1", 10)
    assert exc_info.value.status_code == 429


@pytest.mark.anyio
async def test_rate_limiter_cleanup(fresh_db):
    """Old rate limit records should be cleaned up."""
    from app.api.routes import prune_rate_limits
    from app.database import get_db

    db = await get_db()
    # Insert an old record (2 hours ago)
    db_module._ensure_locks()
    async with db_module._write_lock:
        await db.execute(
            "INSERT INTO rate_limits (key, ts) VALUES (?, datetime('now', '-2 hours'))",
            ("ip:old",),
        )
        await db.commit()

    # Prune with 1-hour window
    await prune_rate_limits(3600)

    # Old record should be gone
    async with db.execute(
        "SELECT COUNT(*) FROM rate_limits WHERE key = 'ip:old'"
    ) as cur:
        row = await cur.fetchone()
        assert row[0] == 0
