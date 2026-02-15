"""State machine edge-case tests."""

import asyncio

import pytest

from app.game.state_machine import StateMachine


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
