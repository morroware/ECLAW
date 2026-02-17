"""State machine edge-case tests."""

import asyncio

import pytest

from app.game.state_machine import StateMachine, TurnState


class _DummyGPIO:
    def __init__(self):
        self.cb = None
        self._locked = False

    async def all_directions_off(self):
        return True

    async def drop_on(self):
        return True

    async def drop_off(self):
        return True

    async def emergency_stop(self):
        return None

    def register_win_callback(self, cb):
        self.cb = cb

    def unregister_win_callback(self):
        self.cb = None


class _DummyQueue:
    async def peek_next_waiting(self):
        return None

    async def set_state(self, *_args, **_kwargs):
        return None

    async def complete_entry(self, *_args, **_kwargs):
        return None

    async def get_queue_status(self):
        return {"queue_length": 0, "current_player": None, "current_player_state": None}

    async def list_queue(self):
        return []


class _DummyWS:
    async def broadcast_state(self, *_args, **_kwargs):
        return None

    async def broadcast_turn_end(self, *_args, **_kwargs):
        return None

    async def broadcast_queue_update(self, *_args, **_kwargs):
        return None


class _DummyCtrl:
    async def send_to_player(self, *_args, **_kwargs):
        return None

    def is_player_connected(self, *_args, **_kwargs):
        return True


class _DummySettings:
    tries_per_player = 2
    ready_prompt_seconds = 5
    try_move_seconds = 5
    drop_hold_max_ms = 200
    post_drop_wait_seconds = 1
    turn_time_seconds = 30
    coin_each_try = False


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


@pytest.mark.anyio
async def test_drop_relay_failure_triggers_recovery():
    class _DropFailGPIO(_DummyGPIO):
        async def drop_on(self):
            return False

    sm = StateMachine(_DropFailGPIO(), _DummyQueue(), _DummyWS(), _DummyCtrl(), _DummySettings())
    sm.state = TurnState.MOVING
    sm.active_entry_id = "entry-1"

    await sm._enter_state(TurnState.DROPPING)

    assert sm.state == TurnState.IDLE
    assert sm.active_entry_id is None


@pytest.mark.anyio
async def test_drop_hold_timeout_recovers_when_drop_release_fails():
    class _DropOffFailGPIO(_DummyGPIO):
        async def drop_off(self):
            return False

    sm = StateMachine(_DropOffFailGPIO(), _DummyQueue(), _DummyWS(), _DummyCtrl(), _DummySettings())
    sm.state = TurnState.DROPPING
    sm.active_entry_id = "entry-2"

    await sm._drop_hold_timeout(0)

    assert sm.state == TurnState.IDLE
    assert sm.active_entry_id is None
