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
    def __init__(self, gpio_controller, queue_manager, ws_hub, control_handler, settings, wled=None):
        self.gpio = gpio_controller
        self.queue = queue_manager
        self.ws = ws_hub
        self.ctrl = control_handler
        self.settings = settings
        self.wled = wled  # Optional WLEDClient — None when WLED is disabled

        self.state = TurnState.IDLE
        self.active_entry_id: str | None = None
        self.current_try: int = 0
        self._state_timer: asyncio.Task | None = None
        self._turn_timer: asyncio.Task | None = None
        self._paused = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._advance_lock = asyncio.Lock()

        # Serialises all state-mutating operations.  The periodic checker,
        # timer callbacks, and WebSocket handlers all go through this lock
        # so that only one mutation runs at a time.
        self._sm_lock = asyncio.Lock()

        # SSOT deadline tracking — monotonic timestamps for computing remaining time.
        # These are set when entering timed states and cleared on state exit.
        self._state_deadline: float = 0.0   # deadline for current state timer
        self._turn_deadline: float = 0.0    # deadline for hard turn timeout

        # Track when the last state transition happened so the periodic
        # checker can detect states that have been stuck for too long.
        self._last_state_change: float = time.monotonic()

        # Prevents concurrent _force_recover calls from piling up.
        self._recovering = False

    # -- Public Interface ----------------------------------------------------

    async def advance_queue(self):
        """Called when queue changes or a turn ends. Starts next player if any.

        If the next player has no WebSocket connection (they navigated away),
        they are skipped immediately instead of waiting for the full ready
        timeout.  Players who joined very recently (< 30 s) get the normal
        ready-prompt flow because their WebSocket may still be connecting.

        The ~2 s WebSocket connection wait runs *outside* ``_advance_lock``
        to minimise lock hold time.  Under burst conditions this prevents
        the lock from being held for seconds while we poll for a connection.
        The candidate is re-validated under the lock before any mutation.
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        # Pre-flight: wait for the likely next candidate's WebSocket
        # connection *outside* the lock so we don't inflate lock hold time.
        # This is best-effort — the candidate is re-validated under the lock.
        candidate = await self.queue.peek_next_waiting()
        if candidate and self.ctrl and not self.ctrl.is_player_connected(candidate["id"]):
            for _ in range(20):  # wait up to ~2 s
                await asyncio.sleep(0.1)
                if self.ctrl.is_player_connected(candidate["id"]):
                    break

        async with self._advance_lock:
            if self.state != TurnState.IDLE:
                return
            if self._paused:
                return

            while True:
                next_entry = await self.queue.peek_next_waiting()
                if next_entry is None:
                    return

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

                    if age_seconds > self.settings.ghost_player_age_s:
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

                # Player is connected (or just joined) — normal ready prompt.
                # Acquire _sm_lock for the mutation section so that
                # force_end_turn() / timer callbacks cannot interleave
                # between setting active_entry_id and _enter_state().
                async with self._sm_lock:
                    # Re-validate: another coroutine may have changed state
                    # while we awaited ghost-player DB operations above.
                    if self.state != TurnState.IDLE:
                        return

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
        async with self._sm_lock:
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
        """Called when active player presses drop. Momentary: relay stays on
        until handle_drop_release() or the safety timeout (drop_hold_max_ms)."""
        async with self._sm_lock:
            if self.state != TurnState.MOVING or entry_id != self.active_entry_id:
                return
            await self._enter_state(TurnState.DROPPING)

    async def handle_drop_release(self, entry_id: str):
        """Called when active player releases drop. Turns relay off and
        transitions to POST_DROP.  If the safety timeout already fired,
        this is a harmless no-op."""
        async with self._sm_lock:
            if self.state != TurnState.DROPPING or entry_id != self.active_entry_id:
                return
            logger.info("Drop released by player")
            # Cancel the safety timeout since the player released manually
            if self._state_timer and not self._state_timer.done():
                self._state_timer.cancel()
            await self.gpio.drop_off()
            self._state_timer = None
            await self._enter_state(TurnState.POST_DROP)

    async def handle_win(self):
        """Called from win sensor callback (thread-safe bridged).

        Accepts wins during both DROPPING and POST_DROP.  Some claw machines
        trigger the win sensor while the claw is still retracting (DROPPING).
        Ignored entirely when win_sensor_enabled is False.
        """
        async with self._sm_lock:
            if not self.settings.win_sensor_enabled:
                logger.debug("Win trigger ignored: win sensor disabled")
                return
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
        # Drop is momentary — if we're in DROPPING state, the safety
        # _drop_hold_timeout will auto-release and transition to POST_DROP.
        logger.info(f"Active player {entry_id} disconnected, directions OFF")

    async def handle_disconnect_timeout(self, entry_id: str):
        """Called after grace period expires without reconnection."""
        async with self._sm_lock:
            if entry_id != self.active_entry_id:
                return
            await self._end_turn("expired")

    async def force_end_turn(self, result: str = "admin_skipped"):
        """Force end the current turn (admin skip, player leave, etc.).

        Handles the edge case where advance_queue() has set active_entry_id
        but hasn't entered READY_PROMPT yet (state still IDLE).  In that
        case _end_turn would bail, so we clean up the entry directly.
        """
        async with self._sm_lock:
            if not self.active_entry_id:
                return

            if self.state in (TurnState.IDLE, TurnState.TURN_END):
                # advance_queue set active_entry_id but the state hasn't
                # transitioned yet (or turn is already ending).  Clean up
                # the DB entry directly and reset.
                entry_id = self.active_entry_id
                logger.info(
                    "force_end_turn: state is %s, cleaning up entry %s directly",
                    self.state, entry_id,
                )
                try:
                    await self.queue.complete_entry(entry_id, result, self.current_try)
                except Exception:
                    logger.exception("force_end_turn: failed to complete entry (non-fatal)")
                self.active_entry_id = None
                self.current_try = 0
                self._state_deadline = 0.0
                self._turn_deadline = 0.0
                self.gpio._locked = False
                # Let advance_queue pick up the next player
                self._schedule_advance()
            else:
                await self._end_turn(result)

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    # -- Internal State Transitions ------------------------------------------

    async def _enter_state(self, new_state: TurnState):
        """Transition to a new state. Cancels any existing state timer.

        MUST be called while holding _sm_lock (or from a timer callback
        that has already checked its state guard).
        """
        if self._state_timer and not self._state_timer.done():
            self._state_timer.cancel()

        old_state = self.state
        self.state = new_state
        self._state_deadline = 0.0  # Reset; set below for timed states
        self._last_state_change = time.monotonic()
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
            if self.settings.win_sensor_enabled:
                self.gpio.register_win_callback(self._win_bridge)
            await self.gpio.drop_on()
            self._state_timer = asyncio.create_task(
                self._drop_hold_timeout(drop_secs)
            )
            await self._wled_event("drop")

        elif new_state == TurnState.POST_DROP:
            # When the win sensor is off there is nothing to wait for —
            # use a short 1-second pause so the UI transition is visible,
            # then advance immediately.
            wait = self.settings.post_drop_wait_seconds if self.settings.win_sensor_enabled else 1
            self._state_deadline = time.monotonic() + wait
            if self.settings.win_sensor_enabled:
                self.gpio.register_win_callback(self._win_bridge)
            self._state_timer = asyncio.create_task(
                self._post_drop_timeout(wait)
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
            for i in range(self.settings.coin_pulses_per_credit):
                await self.gpio.pulse("coin")
                await asyncio.sleep(self.settings.coin_post_pulse_delay_s)  # Let machine register credit

        # Fire WLED start_turn on the first try
        if self.current_try == 1:
            await self._wled_event("start_turn")

        await self._enter_state(TurnState.MOVING)

    async def _end_turn(self, result: str):
        """Clean up and finalize the turn.

        IMPORTANT: This method MUST NOT call advance_queue() directly
        because _end_turn is often invoked from timer callbacks that fire
        while advance_queue() holds _advance_lock (e.g. ready timeout
        during the advance_queue skipping loop).  Calling advance_queue()
        inline would deadlock on _advance_lock.  Instead, we schedule it
        as a fire-and-forget task.
        """
        # Guard against re-entry from concurrent timer callbacks.
        # Two timers (e.g. _hard_turn_timeout and _post_drop_timeout) can
        # both wake and enter _end_turn before either cancels the other.
        # Setting TURN_END immediately blocks all timer state-checks.
        if self.state in (TurnState.IDLE, TurnState.TURN_END):
            return
        prev_state = self.state
        self.state = TurnState.TURN_END
        self._last_state_change = time.monotonic()

        logger.info(f"Turn ending: result={result}, tries={self.current_try}")

        # Cancel timers FIRST, before any await, to prevent the other
        # timer from entering _end_turn during a yield.
        if self._turn_timer and not self._turn_timer.done():
            self._turn_timer.cancel()
        if self._state_timer and not self._state_timer.done():
            self._state_timer.cancel()

        self.gpio.unregister_win_callback()

        # If we're in DROPPING state, explicitly release the drop relay
        # BEFORE the emergency_stop so it gets turned off even if the
        # executor is busy.
        if prev_state == TurnState.DROPPING:
            try:
                await self.gpio.drop_off()
            except Exception:
                logger.exception("Failed to release drop relay before emergency_stop")

        # emergency_stop() handles its own timeouts internally via
        # _gpio_call() and auto-recovers the executor if lgpio blocks.
        # The outer wait_for is pure defense-in-depth (generous 10 s).
        try:
            await asyncio.wait_for(self.gpio.emergency_stop(), timeout=self.settings.emergency_stop_timeout_s)
        except (asyncio.TimeoutError, Exception):
            logger.exception("GPIO emergency_stop outer timeout — continuing cleanup")
        # ALWAYS unlock GPIO.  This is the critical line that prevents
        # _locked from staying True and killing controls for every
        # subsequent player.  We set the flag directly rather than calling
        # unlock() to guarantee it succeeds even if the GPIO controller
        # is in a bad state.
        self.gpio._locked = False

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

        # Fire WLED event based on the turn result
        wled_event = {"win": "win", "loss": "loss", "expired": "expire"}.get(result)
        if wled_event:
            await self._wled_event(wled_event)

        # Always reset to IDLE regardless of cleanup errors above
        self.state = TurnState.IDLE
        self._last_state_change = time.monotonic()
        self.active_entry_id = None
        self.current_try = 0
        self._state_deadline = 0.0
        self._turn_deadline = 0.0

        # Notify WLED of idle state
        await self._wled_event("idle")

        # Schedule advance_queue as a separate task to prevent deadlock.
        # _end_turn is often called from timer callbacks that fire while
        # advance_queue() holds _advance_lock.  A direct call here would
        # deadlock.  The fire-and-forget task will acquire the lock fresh.
        self._schedule_advance()

    def _schedule_advance(self):
        """Schedule advance_queue as a fire-and-forget task.

        Safe to call from anywhere — avoids deadlocks on _advance_lock.
        """
        async def _safe_advance():
            try:
                await self.advance_queue()
            except Exception:
                logger.exception("Scheduled advance_queue failed (periodic check will retry)")

        if self._loop and not self._loop.is_closed():
            self._loop.create_task(_safe_advance())
        else:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_safe_advance())
            except RuntimeError:
                logger.warning("No running event loop for scheduled advance_queue")

    # -- Timers --------------------------------------------------------------

    async def _ready_timeout(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            async with self._sm_lock:
                if self.state == TurnState.READY_PROMPT:
                    logger.info("Ready prompt timed out, skipping player")
                    self._state_timer = None  # Prevent self-cancellation
                    await self._end_turn("skipped")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("_ready_timeout crashed, forcing recovery")
            await self._force_recover()

    async def _move_timeout(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            async with self._sm_lock:
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
            async with self._sm_lock:
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
            async with self._sm_lock:
                if self.state == TurnState.POST_DROP:
                    self.gpio.unregister_win_callback()
                    self._state_timer = None  # Prevent self-cancellation
                    if self.current_try < self.settings.tries_per_player:
                        if self.settings.win_sensor_enabled:
                            logger.info("Post-drop timeout, no win — starting next try")
                        else:
                            logger.info("Win sensor disabled — advancing to next try")
                        await self._start_try()
                    else:
                        if self.settings.win_sensor_enabled:
                            logger.info("Post-drop timeout, no win — ending turn as loss")
                            await self._end_turn("loss")
                        else:
                            logger.info("Win sensor disabled — all tries used, ending turn as loss")
                            await self._end_turn("loss")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("_post_drop_timeout crashed, forcing recovery")
            await self._force_recover()

    async def _hard_turn_timeout(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            async with self._sm_lock:
                if self.state not in (TurnState.IDLE, TurnState.TURN_END):
                    logger.warning("Hard turn timeout reached")
                    self._turn_timer = None  # Prevent self-cancellation
                    await self._end_turn("expired")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("_hard_turn_timeout crashed, forcing recovery")
            await self._force_recover()

    async def _force_recover(self):
        """Emergency recovery: force the state machine back to IDLE.

        Guarded against concurrent calls — only one recovery can run at
        a time.  Uses _sm_lock to prevent races with normal state machine
        operations.
        """
        if self._recovering:
            logger.warning("Force recovery already in progress, skipping")
            return
        self._recovering = True
        try:
            async with self._sm_lock:
                # Re-check: another path may have already recovered us.
                if self.state == TurnState.IDLE and self.active_entry_id is None:
                    logger.info("Force recovery: already IDLE, nothing to do")
                    return

                logger.warning("Force recovering state machine to IDLE")
                if self._state_timer and not self._state_timer.done():
                    self._state_timer.cancel()
                if self._turn_timer and not self._turn_timer.done():
                    self._turn_timer.cancel()
                self.gpio.unregister_win_callback()
                try:
                    await asyncio.wait_for(self.gpio.emergency_stop(), timeout=self.settings.emergency_stop_timeout_s)
                except (asyncio.TimeoutError, Exception):
                    logger.exception("GPIO emergency_stop failed during force recovery")
                # ALWAYS unlock GPIO regardless of what happened above
                self.gpio._locked = False
                if self.active_entry_id:
                    try:
                        await self.queue.complete_entry(
                            self.active_entry_id, "error", self.current_try
                        )
                    except Exception:
                        logger.exception("Failed to complete entry during force recovery (non-fatal)")
                self.state = TurnState.IDLE
                self._last_state_change = time.monotonic()
                self.active_entry_id = None
                self.current_try = 0
                self._state_deadline = 0.0
                self._turn_deadline = 0.0
        except Exception:
            logger.exception("Force recovery also failed!")
            # Last resort: just reset state so periodic check can pick it up
            self.state = TurnState.IDLE
            self._last_state_change = time.monotonic()
            self.active_entry_id = None
            self.current_try = 0
            self._state_deadline = 0.0
            self._turn_deadline = 0.0
            self.gpio._locked = False
        finally:
            self._recovering = False

        # Schedule advance outside the lock to avoid deadlock
        self._schedule_advance()

    # -- WLED helper ---------------------------------------------------------

    async def _wled_event(self, event: str):
        """Fire a WLED event if a client is configured.  Never raises."""
        if self.wled is None:
            return
        try:
            await self.wled.on_event(event)
        except Exception:
            logger.exception("WLED event '%s' failed (non-fatal)", event)

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
            "win_sensor_enabled": self.settings.win_sensor_enabled,
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
