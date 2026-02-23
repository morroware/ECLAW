"""State machine edge-case tests."""

import asyncio

import pytest

from app.game.state_machine import StateMachine, TurnState


class _DummyGPIO:
    _locked = False

    def register_win_callback(self, cb):
        self.cb = cb

    def unregister_win_callback(self):
        return None

    async def emergency_stop(self):
        return None

    async def all_directions_off(self):
        return None

    async def drop_on(self):
        return None

    async def drop_off(self):
        return None


class _DummyQueue:
    def __init__(self):
        self.completed = []

    async def peek_next_waiting(self):
        return None

    async def complete_entry(self, entry_id, result, tries):
        self.completed.append((entry_id, result, tries))

    async def get_queue_status(self):
        return {}

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


class _DummySettings:
    tries_per_player = 2
    win_sensor_enabled = True
    emergency_stop_timeout_s = 1.0
    turn_time_seconds = 90


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
async def test_post_drop_without_sensor_ends_as_loss():
    settings = _DummySettings()
    settings.win_sensor_enabled = False
    settings.tries_per_player = 1

    queue = _DummyQueue()
    sm = StateMachine(_DummyGPIO(), queue, _DummyWS(), _DummyCtrl(), settings)
    await sm.advance_queue()  # primes loop for _schedule_advance

    sm.state = TurnState.POST_DROP
    sm.active_entry_id = "entry-1"
    sm.current_try = 1

    await sm._post_drop_timeout(0)

    assert queue.completed == [("entry-1", "loss", 1)]
