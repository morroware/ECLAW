"""State Machine — core game logic managing turn flow and state transitions."""

import asyncio
import logging
import time
from enum import Enum

logger = logging.getLogger("state_machine")


class TurnState(str, Enum):
    IDLE = "idle"
    READY_PROMPT = "ready_prompt"
    MOVING = "moving"
    DROPPING = "dropping"
    POST_DROP = "post_drop"
    TURN_END = "turn_end"


class StateMachine:
    def __init__(self, gpio_controller, queue_manager, ws_hub, control_handler, settings):
        self.gpio = gpio_controller
        self.queue = queue_manager
        self.ws = ws_hub
        self.ctrl = control_handler
        self.settings = settings

        self.state = TurnState.IDLE
        self.active_entry_id: str | None = None
        self.current_try: int = 0
        self._state_timer: asyncio.Task | None = None
        self._turn_timer: asyncio.Task | None = None
        self._paused = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._advance_lock = asyncio.Lock()

        # SSOT deadline tracking — monotonic timestamps for computing remaining time.
        # These are set when entering timed states and cleared on state exit.
        self._state_deadline: float = 0.0   # deadline for current state timer
        self._turn_deadline: float = 0.0    # deadline for hard turn timeout

    # -- Public Interface ----------------------------------------------------

    async def advance_queue(self):
        """Called when queue changes or a turn ends. Starts next player if any.

        If the next player has no WebSocket connection (they navigated away),
        they are skipped immediately instead of waiting for the full ready
        timeout.  Players who joined very recently (< 30 s) get the normal
        ready-prompt flow because their WebSocket may still be connecting.
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        async with self._advance_lock:
            if self.state != TurnState.IDLE:
                return
            if self._paused:
                return

            while True:
                next_entry = await self.queue.peek_next_waiting()
                if next_entry is None:
                    return

                # Give the player a moment to establish their control WebSocket
                # before deciding whether to skip or prompt.
                if self.ctrl and not self.ctrl.is_player_connected(next_entry["id"]):
                    for _ in range(20):  # wait up to ~2 s
                        await asyncio.sleep(0.1)
                        if self.ctrl.is_player_connected(next_entry["id"]):
                            break

                # If still not connected, check how long they've been in the
                # queue.  Players who joined > 30 s ago and have no WebSocket
                # have almost certainly navigated away — skip them immediately
                # so the queue drains in seconds, not minutes.
                if self.ctrl and not self.ctrl.is_player_connected(next_entry["id"]):
                    from datetime import datetime, timezone
                    created = next_entry.get("created_at", "")
                    age_seconds = 999
                    try:
                        # SQLite stores as ISO without tz — assume UTC
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
                    except Exception:
                        pass

                    if age_seconds > 30:
                        logger.info(
                            "Skipping disconnected player %s (%s, queued %.0fs ago)",
                            next_entry["id"], next_entry["name"], age_seconds,
                        )
                        await self.queue.complete_entry(next_entry["id"], "skipped", 0)

                        # Broadcast the skip to viewers
                        try:
                            await self.ws.broadcast_turn_end(next_entry["id"], "skipped")
                            status = await self.queue.get_queue_status()
                            entries = await self.queue.list_queue()
                            queue_entries = [
                                {"name": e["name"], "state": e["state"], "position": e["position"]}
                                for e in entries
                            ]
                            await self.ws.broadcast_queue_update(status, queue_entries)
                        except Exception:
                            logger.exception("Broadcast failed during ghost-player skip (non-fatal)")

                        continue  # Try the next waiting player

                # Player is connected (or just joined) — normal ready prompt
                self.active_entry_id = next_entry["id"]
                await self.queue.set_state(next_entry["id"], "ready")

                # Broadcast updated queue so viewers see the player as READY
                try:
                    status = await self.queue.get_queue_status()
                    entries = await self.queue.list_queue()
                    queue_entries = [
                        {"name": e["name"], "state": e["state"], "position": e["position"]}
                        for e in entries
                    ]
                    await self.ws.broadcast_queue_update(status, queue_entries)
                except Exception:
                    logger.exception("Broadcast failed during ready advancement (non-fatal)")

                await self._enter_state(TurnState.READY_PROMPT)
                return

    async def handle_ready_confirm(self, entry_id: str):
        """Called when the prompted player confirms they are ready."""
        if self.state != TurnState.READY_PROMPT:
            return
        if entry_id != self.active_entry_id:
            return

        await self.queue.set_state(entry_id, "active")
        self.current_try = 0

        # Start hard turn timer and record its deadline
        self._turn_deadline = time.monotonic() + self.settings.turn_time_seconds
        self._turn_timer = asyncio.create_task(
            self._hard_turn_timeout(self.settings.turn_time_seconds)
        )
        await self._start_try()

    async def handle_drop_press(self, entry_id: str):
        """Called when active player clicks drop. Single-click: activates the
        relay, holds for drop_hold_max_ms, then auto-releases into POST_DROP."""
        if self.state != TurnState.MOVING or entry_id != self.active_entry_id:
            return
        await self._enter_state(TurnState.DROPPING)

    async def handle_win(self):
        """Called from win sensor callback (thread-safe bridged).

        Accepts wins during both DROPPING and POST_DROP.  Some claw machines
        trigger the win sensor while the claw is still retracting (DROPPING).
        """
        if self.state == TurnState.DROPPING:
            logger.info("WIN DETECTED during DROPPING — ending turn early")
            await self._end_turn("win")
        elif self.state == TurnState.POST_DROP:
            logger.info("WIN DETECTED")
            await self._end_turn("win")
        else:
            logger.warning(f"Win trigger ignored: state is {self.state}")

    async def handle_disconnect(self, entry_id: str):
        """Called when active player's WebSocket disconnects."""
        if entry_id != self.active_entry_id:
            return
        await self.gpio.all_directions_off()
        # Drop is now single-click with auto-release timer, so if we're in
        # DROPPING state the _drop_hold_timeout will handle the transition.
        logger.info(f"Active player {entry_id} disconnected, directions OFF")

    async def handle_disconnect_timeout(self, entry_id: str):
        """Called after grace period expires without reconnection."""
        if entry_id != self.active_entry_id:
            return
        await self._end_turn("expired")

    async def force_end_turn(self, result: str = "admin_skipped"):
        """Admin: force end the current turn."""
        if self.active_entry_id:
            await self._end_turn(result)

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    # -- Internal State Transitions ------------------------------------------

    async def _enter_state(self, new_state: TurnState):
        """Transition to a new state. Cancels any existing state timer."""
        if self._state_timer and not self._state_timer.done():
            self._state_timer.cancel()

        old_state = self.state
        self.state = new_state
        self._state_deadline = 0.0  # Reset; set below for timed states
        logger.info(f"State: {old_state} -> {new_state}")

        if new_state == TurnState.READY_PROMPT:
            self._state_deadline = time.monotonic() + self.settings.ready_prompt_seconds
            self._state_timer = asyncio.create_task(
                self._ready_timeout(self.settings.ready_prompt_seconds)
            )

        elif new_state == TurnState.MOVING:
            self._state_deadline = time.monotonic() + self.settings.try_move_seconds
            self._state_timer = asyncio.create_task(
                self._move_timeout(self.settings.try_move_seconds)
            )
            # Persist deadline to DB for SSOT recovery
            await self._write_deadlines()

        elif new_state == TurnState.DROPPING:
            drop_secs = self.settings.drop_hold_max_ms / 1000.0
            self._state_deadline = time.monotonic() + drop_secs
            await self.gpio.all_directions_off()
            self.gpio.register_win_callback(self._win_bridge)
            await self.gpio.drop_on()
            self._state_timer = asyncio.create_task(
                self._drop_hold_timeout(drop_secs)
            )

        elif new_state == TurnState.POST_DROP:
            self._state_deadline = time.monotonic() + self.settings.post_drop_wait_seconds
            self.gpio.register_win_callback(self._win_bridge)
            self._state_timer = asyncio.create_task(
                self._post_drop_timeout(self.settings.post_drop_wait_seconds)
            )

        elif new_state == TurnState.TURN_END:
            pass  # Handled by _end_turn

        # Broadcast state to all viewers (after deadlines are set so payload
        # includes accurate remaining-time values)
        payload = self._build_state_payload()
        await self.ws.broadcast_state(new_state, payload)

        # Also notify the active player via control channel
        if self.active_entry_id and self.ctrl:
            await self.ctrl.send_to_player(self.active_entry_id, {
                "type": "state_update", **payload
            })

        # Send explicit ready_prompt after the state_update so the client
        # has full context (timeout_seconds is included in the payload now)
        if new_state == TurnState.READY_PROMPT and self.ctrl:
            await self.ctrl.send_to_player(self.active_entry_id, {
                "type": "ready_prompt",
                "timeout_seconds": self.settings.ready_prompt_seconds,
            })

    async def _start_try(self):
        """Begin a new try. Optionally pulse coin, then enter MOVING."""
        self.current_try += 1
        logger.info(f"Starting try {self.current_try}/{self.settings.tries_per_player}")

        if self.settings.coin_each_try:
            await self.gpio.pulse("coin")
            await asyncio.sleep(0.5)  # Let machine register credit

        await self._enter_state(TurnState.MOVING)

    async def _end_turn(self, result: str):
        """Clean up and finalize the turn."""
        # Guard against re-entry from concurrent timer callbacks.
        # Two timers (e.g. _hard_turn_timeout and _post_drop_timeout) can
        # both wake and enter _end_turn before either cancels the other.
        # Setting TURN_END immediately blocks all timer state-checks.
        if self.state in (TurnState.IDLE, TurnState.TURN_END):
            return
        self.state = TurnState.TURN_END

        logger.info(f"Turn ending: result={result}, tries={self.current_try}")

        # Cancel timers FIRST, before any await, to prevent the other
        # timer from entering _end_turn during a yield.
        if self._turn_timer and not self._turn_timer.done():
            self._turn_timer.cancel()
        if self._state_timer and not self._state_timer.done():
            self._state_timer.cancel()

        self.gpio.unregister_win_callback()
        await self.gpio.emergency_stop()
        await self.gpio.unlock()

        try:
            if self.active_entry_id:
                await self.queue.complete_entry(
                    self.active_entry_id, result, self.current_try
                )
                await self.ws.broadcast_turn_end(self.active_entry_id, result)

                # Notify the player directly
                if self.ctrl:
                    await self.ctrl.send_to_player(self.active_entry_id, {
                        "type": "turn_end",
                        "result": result,
                        "tries_used": self.current_try,
                    })

            # Broadcast updated queue status with full entry list
            status = await self.queue.get_queue_status()
            entries = await self.queue.list_queue()
            queue_entries = [
                {"name": e["name"], "state": e["state"], "position": e["position"]}
                for e in entries
            ]
            await self.ws.broadcast_queue_update(status, queue_entries)
        except Exception:
            logger.exception("Error during turn-end cleanup (non-fatal)")

        # Always reset to IDLE regardless of cleanup errors above
        self.state = TurnState.IDLE
        self.active_entry_id = None
        self.current_try = 0
        self._state_deadline = 0.0
        self._turn_deadline = 0.0

        # Immediately try to start the next player
        try:
            await self.advance_queue()
        except Exception:
            logger.exception("advance_queue failed after turn end (periodic check will retry)")

    # -- Timers --------------------------------------------------------------

    async def _ready_timeout(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.state == TurnState.READY_PROMPT:
                logger.info("Ready prompt timed out, skipping player")
                await self._end_turn("skipped")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("_ready_timeout crashed, forcing recovery")
            await self._force_recover()

    async def _move_timeout(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.state == TurnState.MOVING:
                logger.info("Move timer expired, auto-dropping")
                self._state_timer = None  # Prevent self-cancellation
                await self._enter_state(TurnState.DROPPING)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("_move_timeout crashed, forcing recovery")
            await self._force_recover()

    async def _drop_hold_timeout(self, seconds: float):
        """Safety: auto-release drop after max hold time."""
        try:
            await asyncio.sleep(seconds)
            if self.state == TurnState.DROPPING:
                logger.info("Drop hold timeout, auto-releasing")
                await self.gpio.drop_off()
                self._state_timer = None  # Prevent self-cancellation
                await self._enter_state(TurnState.POST_DROP)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("_drop_hold_timeout crashed, forcing recovery")
            # Ensure drop relay is off even on error
            try:
                await self.gpio.drop_off()
            except Exception:
                pass
            await self._force_recover()

    async def _post_drop_timeout(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.state == TurnState.POST_DROP:
                self.gpio.unregister_win_callback()
                logger.info("Post-drop timeout, no win")
                self._state_timer = None  # Prevent self-cancellation
                if self.current_try < self.settings.tries_per_player:
                    await self._start_try()
                else:
                    await self._end_turn("loss")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("_post_drop_timeout crashed, forcing recovery")
            await self._force_recover()

    async def _hard_turn_timeout(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.state not in (TurnState.IDLE, TurnState.TURN_END):
                logger.warning("Hard turn timeout reached")
                await self._end_turn("expired")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("_hard_turn_timeout crashed, forcing recovery")
            await self._force_recover()

    async def _force_recover(self):
        """Emergency recovery: force the state machine back to IDLE."""
        try:
            logger.warning("Force recovering state machine to IDLE")
            if self._state_timer and not self._state_timer.done():
                self._state_timer.cancel()
            if self._turn_timer and not self._turn_timer.done():
                self._turn_timer.cancel()
            self.gpio.unregister_win_callback()
            await self.gpio.emergency_stop()
            await self.gpio.unlock()
            if self.active_entry_id:
                await self.queue.complete_entry(
                    self.active_entry_id, "error", self.current_try
                )
            self.state = TurnState.IDLE
            self.active_entry_id = None
            self.current_try = 0
            self._state_deadline = 0.0
            self._turn_deadline = 0.0
            await self.advance_queue()
        except Exception:
            logger.exception("Force recovery also failed!")
            # Last resort: just reset state so periodic check can pick it up
            self.state = TurnState.IDLE
            self.active_entry_id = None
            self.current_try = 0
            self._state_deadline = 0.0
            self._turn_deadline = 0.0

    # -- Helpers -------------------------------------------------------------

    def _win_bridge(self):
        """Called from gpiozero thread. Bridges into async event loop."""
        if self._loop is None or self._loop.is_closed():
            logger.error("Failed to bridge win callback to event loop")
            return

        asyncio.run_coroutine_threadsafe(self.handle_win(), self._loop)

    def _build_state_payload(self) -> dict:
        # Compute seconds remaining for the current state timer.
        # Uses monotonic clock so it's immune to wall-clock adjustments.
        remaining = 0.0
        if self._state_deadline > 0:
            remaining = max(0.0, self._state_deadline - time.monotonic())

        turn_remaining = 0.0
        if self._turn_deadline > 0:
            turn_remaining = max(0.0, self._turn_deadline - time.monotonic())

        return {
            "state": self.state.value,
            "active_entry_id": self.active_entry_id,
            "current_try": self.current_try,
            "max_tries": self.settings.tries_per_player,
            "try_move_seconds": self.settings.try_move_seconds,
            "state_seconds_left": round(remaining, 1),
            "turn_seconds_left": round(turn_remaining, 1),
        }

    async def _write_deadlines(self):
        """Persist deadline timestamps to DB for SSOT recovery."""
        if not self.active_entry_id:
            return
        try:
            from datetime import datetime, timezone, timedelta
            from app.database import get_db
            import app.database as _db_mod
            now = datetime.now(timezone.utc)
            move_end = None
            turn_end = None
            if self._state_deadline > 0:
                secs_left = max(0, self._state_deadline - time.monotonic())
                move_end = (now + timedelta(seconds=secs_left)).isoformat()
            if self._turn_deadline > 0:
                secs_left = max(0, self._turn_deadline - time.monotonic())
                turn_end = (now + timedelta(seconds=secs_left)).isoformat()
            db = await get_db()
            async with _db_mod._write_lock:
                await db.execute(
                    "UPDATE queue_entries SET try_move_end_at = ?, turn_end_at = ? WHERE id = ?",
                    (move_end, turn_end, self.active_entry_id),
                )
                await db.commit()
        except Exception:
            logger.exception("Failed to write deadlines to DB (non-fatal)")
