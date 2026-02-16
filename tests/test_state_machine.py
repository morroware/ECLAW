"""State machine tests — edge cases and full game-flow integration."""

import asyncio

import pytest

from app.game.state_machine import StateMachine, TurnState


# -- Lightweight dummies for the win-bridge unit test -----------------------

class _DummyGPIO:
    def register_win_callback(self, cb):
        self.cb = cb


class _DummyQueue:
    async def peek_next_waiting(self):
        return None


class _DummyWS:
    async def broadcast_state(self, *_args, **_kwargs):
        return None


class _DummyCtrl:
    async def send_to_player(self, *_args, **_kwargs):
        return None


class _DummySettings:
    tries_per_player = 2


@pytest.mark.anyio
async def test_win_bridge_uses_running_loop():
    sm = StateMachine(_DummyGPIO(), _DummyQueue(), _DummyWS(), _DummyCtrl(), _DummySettings())

    handled = asyncio.Event()

    async def fake_handle_win():
        handled.set()

    sm.handle_win = fake_handle_win
    await sm.advance_queue()  # primes the state machine's loop reference

    await asyncio.to_thread(sm._win_bridge)

    await asyncio.wait_for(handled.wait(), timeout=1)


# -- Full-fidelity mocks for game-flow integration tests --------------------

class MockGPIO:
    """Records every GPIO call for assertions."""

    def __init__(self):
        self.log: list[str] = []
        self._locked = False
        self._win_cb = None
        self._active_holds: dict[str, asyncio.Task] = {}

    async def initialize(self):
        self.log.append("init")

    async def pulse(self, name):
        self.log.append(f"pulse:{name}")
        return True

    async def direction_on(self, d):
        self.log.append(f"dir_on:{d}")
        return True

    async def direction_off(self, d):
        self.log.append(f"dir_off:{d}")
        return True

    async def all_directions_off(self):
        self.log.append("all_dirs_off")

    async def drop_on(self):
        self.log.append("drop_on")
        return True

    async def drop_off(self):
        self.log.append("drop_off")
        return True

    async def emergency_stop(self):
        self.log.append("estop")
        self._locked = True

    async def unlock(self):
        self.log.append("unlock")
        self._locked = False

    def register_win_callback(self, cb):
        self._win_cb = cb

    def unregister_win_callback(self):
        self._win_cb = None

    @property
    def is_locked(self):
        return self._locked


class MockQueueManager:
    """In-memory queue manager that mirrors the real DB operations."""

    def __init__(self):
        self._entries: dict[str, dict] = {}
        self._order: list[str] = []

    def add_player(self, entry_id, name="Player"):
        """Helper: pre-load a waiting player."""
        pos = len(self._order) + 1
        self._entries[entry_id] = {
            "id": entry_id,
            "name": name,
            "state": "waiting",
            "position": pos,
            "result": None,
            "tries_used": 0,
        }
        self._order.append(entry_id)

    async def peek_next_waiting(self):
        for eid in self._order:
            e = self._entries.get(eid)
            if e and e["state"] == "waiting":
                return dict(e)
        return None

    async def set_state(self, entry_id, state):
        if entry_id in self._entries:
            self._entries[entry_id]["state"] = state

    async def complete_entry(self, entry_id, result, tries_used):
        if entry_id in self._entries:
            self._entries[entry_id]["state"] = "done"
            self._entries[entry_id]["result"] = result
            self._entries[entry_id]["tries_used"] = tries_used

    async def get_queue_status(self):
        waiting = sum(1 for e in self._entries.values() if e["state"] == "waiting")
        active = next((e for e in self._entries.values() if e["state"] in ("active", "ready")), None)
        return {
            "queue_length": waiting,
            "current_player": active["name"] if active else None,
            "current_player_state": active["state"] if active else None,
        }

    async def list_queue(self):
        return [
            dict(e) for e in self._entries.values()
            if e["state"] in ("waiting", "ready", "active")
        ]

    def get_entry(self, entry_id):
        return self._entries.get(entry_id)


class MockWSHub:
    """Records all broadcasts for assertions."""

    def __init__(self):
        self.messages: list[dict] = []

    async def broadcast_state(self, state, payload):
        self.messages.append({"type": "state_update", **payload})

    async def broadcast_turn_end(self, entry_id, result):
        self.messages.append({"type": "turn_end", "entry_id": entry_id, "result": result})

    async def broadcast_queue_update(self, status, queue_entries=None):
        self.messages.append({"type": "queue_update", **status})


class MockCtrl:
    """Records messages sent to players."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self._connected: set[str] = set()

    def set_connected(self, entry_id):
        self._connected.add(entry_id)

    async def send_to_player(self, entry_id, message):
        self.sent.append((entry_id, message))

    def is_player_connected(self, entry_id):
        return entry_id in self._connected


class MockSettings:
    tries_per_player = 2
    turn_time_seconds = 90
    try_move_seconds = 30
    post_drop_wait_seconds = 8
    ready_prompt_seconds = 15
    coin_each_try = False
    drop_hold_max_ms = 100        # Very short for tests
    command_rate_limit_hz = 25
    direction_conflict_mode = "ignore_new"


# -- Helpers -----------------------------------------------------------------

async def wait_for_state(sm, target_state, timeout=2.0):
    """Poll until the state machine reaches the target state."""
    elapsed = 0.0
    while elapsed < timeout:
        if sm.state == target_state:
            return
        await asyncio.sleep(0.05)
        elapsed += 0.05
    raise AssertionError(f"Timed out waiting for {target_state}, stuck at {sm.state}")


# -- Integration tests -------------------------------------------------------

@pytest.mark.anyio
async def test_full_turn_flow_loss():
    """Simulate a complete turn: join -> ready -> move -> drop -> post_drop -> loss."""
    gpio = MockGPIO()
    qm = MockQueueManager()
    ws = MockWSHub()
    ctrl = MockCtrl()
    s = MockSettings()
    # Use longer hold so intermediate states are observable
    s.drop_hold_max_ms = 500
    s.post_drop_wait_seconds = 0.5

    sm = StateMachine(gpio, qm, ws, ctrl, s)

    # Simulate a player in the queue
    qm.add_player("p1", "Alice")
    ctrl.set_connected("p1")

    # 1. Advance queue → should enter READY_PROMPT
    await sm.advance_queue()
    assert sm.state == TurnState.READY_PROMPT
    assert sm.active_entry_id == "p1"
    assert qm.get_entry("p1")["state"] == "ready"

    # 2. Player confirms ready → MOVING
    await sm.handle_ready_confirm("p1")
    assert sm.state == TurnState.MOVING
    assert sm.current_try == 1
    assert qm.get_entry("p1")["state"] == "active"

    # 3. Player drops → DROPPING
    await sm.handle_drop_press("p1")
    assert sm.state == TurnState.DROPPING
    assert "drop_on" in gpio.log
    assert "all_dirs_off" in gpio.log

    # 4. Drop hold timeout auto-releases → POST_DROP
    await wait_for_state(sm, TurnState.POST_DROP)
    assert "drop_off" in gpio.log

    # 5. Post-drop timeout (no win) → try 2 starts
    await wait_for_state(sm, TurnState.MOVING)
    assert sm.current_try == 2

    # 6. Drop again for try 2
    await sm.handle_drop_press("p1")
    assert sm.state == TurnState.DROPPING

    # 7-8. Let it complete: drop timeout → post-drop timeout → loss → IDLE
    await wait_for_state(sm, TurnState.IDLE)
    assert sm.active_entry_id is None

    # Verify outcome
    entry = qm.get_entry("p1")
    assert entry["state"] == "done"
    assert entry["result"] == "loss"
    assert entry["tries_used"] == 2

    # Verify GPIO was cleaned up
    assert "estop" in gpio.log


@pytest.mark.anyio
async def test_full_turn_flow_win():
    """Simulate a win: the win sensor fires during POST_DROP."""
    gpio = MockGPIO()
    qm = MockQueueManager()
    ws = MockWSHub()
    ctrl = MockCtrl()
    s = MockSettings()
    s.drop_hold_max_ms = 50
    s.post_drop_wait_seconds = 5  # Long enough that win fires first

    sm = StateMachine(gpio, qm, ws, ctrl, s)
    qm.add_player("p1", "Winner")
    ctrl.set_connected("p1")

    await sm.advance_queue()
    await sm.handle_ready_confirm("p1")
    await sm.handle_drop_press("p1")

    # Wait for drop hold to release into POST_DROP
    await asyncio.sleep(0.2)
    assert sm.state == TurnState.POST_DROP

    # Trigger win
    await sm.handle_win()
    assert sm.state == TurnState.IDLE

    entry = qm.get_entry("p1")
    assert entry["result"] == "win"
    assert entry["tries_used"] == 1


@pytest.mark.anyio
async def test_multi_player_queue_cycling():
    """Two players in queue: after player 1 finishes, player 2 auto-starts."""
    gpio = MockGPIO()
    qm = MockQueueManager()
    ws = MockWSHub()
    ctrl = MockCtrl()
    s = MockSettings()
    s.tries_per_player = 1
    s.drop_hold_max_ms = 50
    s.post_drop_wait_seconds = 0.1

    sm = StateMachine(gpio, qm, ws, ctrl, s)

    qm.add_player("p1", "Alice")
    qm.add_player("p2", "Bob")
    ctrl.set_connected("p1")
    ctrl.set_connected("p2")

    # Start player 1
    await sm.advance_queue()
    assert sm.active_entry_id == "p1"
    assert sm.state == TurnState.READY_PROMPT

    # Player 1 plays and finishes
    await sm.handle_ready_confirm("p1")
    await sm.handle_drop_press("p1")
    await asyncio.sleep(0.2)   # drop hold
    await asyncio.sleep(0.2)   # post-drop timeout → loss (1 try)

    # Player 1 is done; player 2 should have auto-advanced
    assert qm.get_entry("p1")["state"] == "done"
    assert qm.get_entry("p1")["result"] == "loss"
    assert sm.active_entry_id == "p2"
    assert sm.state == TurnState.READY_PROMPT

    # Player 2 confirms and plays
    await sm.handle_ready_confirm("p2")
    assert sm.state == TurnState.MOVING
    assert sm.active_entry_id == "p2"


@pytest.mark.anyio
async def test_admin_force_advance():
    """Admin can force-end a turn at any time."""
    gpio = MockGPIO()
    qm = MockQueueManager()
    ws = MockWSHub()
    ctrl = MockCtrl()
    s = MockSettings()

    sm = StateMachine(gpio, qm, ws, ctrl, s)
    qm.add_player("p1", "Slow")
    ctrl.set_connected("p1")

    await sm.advance_queue()
    await sm.handle_ready_confirm("p1")
    assert sm.state == TurnState.MOVING

    # Admin skips
    await sm.force_end_turn("admin_skipped")
    assert sm.state == TurnState.IDLE
    assert qm.get_entry("p1")["result"] == "admin_skipped"


@pytest.mark.anyio
async def test_ready_timeout_skips_player():
    """Player who doesn't confirm ready gets skipped, next player starts."""
    gpio = MockGPIO()
    qm = MockQueueManager()
    ws = MockWSHub()
    ctrl = MockCtrl()
    s = MockSettings()
    s.ready_prompt_seconds = 0.2

    sm = StateMachine(gpio, qm, ws, ctrl, s)
    qm.add_player("p1", "AFK")
    qm.add_player("p2", "Ready")
    ctrl.set_connected("p1")
    ctrl.set_connected("p2")

    await sm.advance_queue()
    assert sm.active_entry_id == "p1"
    assert sm.state == TurnState.READY_PROMPT

    # Wait for p1 to be skipped: need to poll because p2's own ready
    # timeout will also fire at 0.2s, so we must check quickly.
    async def p2_advanced():
        for _ in range(40):
            if sm.active_entry_id == "p2" and sm.state == TurnState.READY_PROMPT:
                return True
            await asyncio.sleep(0.05)
        return False

    assert await p2_advanced(), f"Expected p2 READY_PROMPT, got {sm.state} / {sm.active_entry_id}"
    assert qm.get_entry("p1")["state"] == "skipped"

    # Confirm p2 ready before their timer expires
    await sm.handle_ready_confirm("p2")
    assert sm.state == TurnState.MOVING
    assert sm.active_entry_id == "p2"


@pytest.mark.anyio
async def test_move_timeout_auto_drops():
    """If player doesn't drop within move timeout, auto-drop fires."""
    gpio = MockGPIO()
    qm = MockQueueManager()
    ws = MockWSHub()
    ctrl = MockCtrl()
    s = MockSettings()
    s.try_move_seconds = 0.2
    s.drop_hold_max_ms = 2000  # Long hold so we can catch DROPPING

    sm = StateMachine(gpio, qm, ws, ctrl, s)
    qm.add_player("p1", "Idle")
    ctrl.set_connected("p1")

    await sm.advance_queue()
    await sm.handle_ready_confirm("p1")
    assert sm.state == TurnState.MOVING

    # Wait for move timeout to trigger auto-drop
    await wait_for_state(sm, TurnState.DROPPING)
    assert "drop_on" in gpio.log


@pytest.mark.anyio
async def test_drop_press_rejected_outside_moving():
    """Drop press is ignored if not in MOVING state."""
    gpio = MockGPIO()
    qm = MockQueueManager()
    ws = MockWSHub()
    ctrl = MockCtrl()
    s = MockSettings()

    sm = StateMachine(gpio, qm, ws, ctrl, s)
    qm.add_player("p1", "Player")
    ctrl.set_connected("p1")

    await sm.advance_queue()
    assert sm.state == TurnState.READY_PROMPT

    # Drop during READY_PROMPT should be ignored
    await sm.handle_drop_press("p1")
    assert sm.state == TurnState.READY_PROMPT
    assert "drop_on" not in gpio.log


@pytest.mark.anyio
async def test_wrong_player_commands_rejected():
    """Only the active player's commands are accepted."""
    gpio = MockGPIO()
    qm = MockQueueManager()
    ws = MockWSHub()
    ctrl = MockCtrl()
    s = MockSettings()

    sm = StateMachine(gpio, qm, ws, ctrl, s)
    qm.add_player("p1", "Active")
    ctrl.set_connected("p1")

    await sm.advance_queue()
    await sm.handle_ready_confirm("p1")
    assert sm.state == TurnState.MOVING

    # Wrong player tries to drop
    await sm.handle_drop_press("p2")
    assert sm.state == TurnState.MOVING  # Unchanged

    # Wrong player tries to confirm ready
    await sm.handle_ready_confirm("p2")
    assert sm.state == TurnState.MOVING  # Unchanged


@pytest.mark.anyio
async def test_pause_prevents_queue_advance():
    """Pausing the state machine prevents the next player from starting."""
    gpio = MockGPIO()
    qm = MockQueueManager()
    ws = MockWSHub()
    ctrl = MockCtrl()
    s = MockSettings()

    sm = StateMachine(gpio, qm, ws, ctrl, s)
    qm.add_player("p1", "Player")
    ctrl.set_connected("p1")

    sm.pause()
    await sm.advance_queue()
    assert sm.state == TurnState.IDLE  # Should NOT advance

    sm.resume()
    await sm.advance_queue()
    assert sm.state == TurnState.READY_PROMPT  # Now it advances


@pytest.mark.anyio
async def test_empty_queue_stays_idle():
    """advance_queue with no waiting players stays in IDLE."""
    gpio = MockGPIO()
    qm = MockQueueManager()
    ws = MockWSHub()
    ctrl = MockCtrl()
    s = MockSettings()

    sm = StateMachine(gpio, qm, ws, ctrl, s)

    await sm.advance_queue()
    assert sm.state == TurnState.IDLE
    assert sm.active_entry_id is None
