"""State Machine — core game logic managing turn flow and state transitions."""

import asyncio
import logging
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

    # -- Public Interface ----------------------------------------------------

    async def advance_queue(self):
        """Called when queue changes or a turn ends. Starts next player if any."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        async with self._advance_lock:
            if self.state != TurnState.IDLE:
                return
            if self._paused:
                return

            next_entry = await self.queue.peek_next_waiting()
            if next_entry is None:
                return

            self.active_entry_id = next_entry["id"]
            await self.queue.set_state(next_entry["id"], "ready")
            await self._enter_state(TurnState.READY_PROMPT)

    async def handle_ready_confirm(self, entry_id: str):
        """Called when the prompted player confirms they are ready."""
        if self.state != TurnState.READY_PROMPT:
            return
        if entry_id != self.active_entry_id:
            return

        await self.queue.set_state(entry_id, "active")
        self.current_try = 0

        # Start hard turn timer
        self._turn_timer = asyncio.create_task(
            self._hard_turn_timeout(self.settings.turn_time_seconds)
        )
        await self._start_try()

    async def handle_drop(self, entry_id: str):
        """Called when active player presses drop."""
        if self.state != TurnState.MOVING or entry_id != self.active_entry_id:
            return
        await self._enter_state(TurnState.DROPPING)

    async def handle_win(self):
        """Called from win sensor callback (thread-safe bridged)."""
        if self.state != TurnState.POST_DROP:
            logger.warning(f"Win trigger ignored: state is {self.state}")
            return
        logger.info("WIN DETECTED")
        await self._end_turn("win")

    async def handle_disconnect(self, entry_id: str):
        """Called when active player's WebSocket disconnects."""
        if entry_id != self.active_entry_id:
            return
        await self.gpio.all_directions_off()
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
        logger.info(f"State: {old_state} -> {new_state}")

        # Broadcast state to all viewers
        payload = self._build_state_payload()
        await self.ws.broadcast_state(new_state, payload)

        # Also notify the active player via control channel
        if self.active_entry_id and self.ctrl:
            await self.ctrl.send_to_player(self.active_entry_id, {
                "type": "state_update", **payload
            })

        if new_state == TurnState.READY_PROMPT:
            self._state_timer = asyncio.create_task(
                self._ready_timeout(self.settings.ready_prompt_seconds)
            )
            # Notify the specific player they need to confirm
            if self.ctrl:
                await self.ctrl.send_to_player(self.active_entry_id, {
                    "type": "ready_prompt",
                    "timeout_seconds": self.settings.ready_prompt_seconds,
                })

        elif new_state == TurnState.MOVING:
            self._state_timer = asyncio.create_task(
                self._move_timeout(self.settings.try_move_seconds)
            )

        elif new_state == TurnState.DROPPING:
            await self.gpio.all_directions_off()
            await self.gpio.pulse("drop")
            await self._enter_state(TurnState.POST_DROP)

        elif new_state == TurnState.POST_DROP:
            self.gpio.register_win_callback(self._win_bridge)
            self._state_timer = asyncio.create_task(
                self._post_drop_timeout(self.settings.post_drop_wait_seconds)
            )

        elif new_state == TurnState.TURN_END:
            pass  # Handled by _end_turn

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
        logger.info(f"Turn ending: result={result}, tries={self.current_try}")

        self.gpio.unregister_win_callback()
        await self.gpio.emergency_stop()
        await self.gpio.unlock()

        if self._turn_timer and not self._turn_timer.done():
            self._turn_timer.cancel()
        if self._state_timer and not self._state_timer.done():
            self._state_timer.cancel()

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

        # Broadcast updated queue status
        status = await self.queue.get_queue_status()
        await self.ws.broadcast_queue_update(status)

        self.state = TurnState.IDLE
        self.active_entry_id = None
        self.current_try = 0

        # Immediately try to start the next player
        await self.advance_queue()

    # -- Timers --------------------------------------------------------------

    async def _ready_timeout(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.state == TurnState.READY_PROMPT:
                logger.info("Ready prompt timed out, skipping player")
                entry_id = self.active_entry_id

                # Notify the skipped player before clearing active_entry_id
                if self.ctrl and entry_id:
                    await self.ctrl.send_to_player(entry_id, {
                        "type": "turn_end",
                        "result": "skipped",
                        "tries_used": 0,
                    })

                await self.queue.set_state(entry_id, "skipped")
                self.state = TurnState.IDLE
                self.active_entry_id = None
                await self.ws.broadcast_queue_update(await self.queue.get_queue_status())
                await self.advance_queue()
        except asyncio.CancelledError:
            pass

    async def _move_timeout(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.state == TurnState.MOVING:
                logger.info("Move timer expired, auto-dropping")
                await self._enter_state(TurnState.DROPPING)
        except asyncio.CancelledError:
            pass

    async def _post_drop_timeout(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.state == TurnState.POST_DROP:
                self.gpio.unregister_win_callback()
                logger.info("Post-drop timeout, no win")
                if self.current_try < self.settings.tries_per_player:
                    await self._start_try()
                else:
                    await self._end_turn("loss")
        except asyncio.CancelledError:
            pass

    async def _hard_turn_timeout(self, seconds: int):
        try:
            await asyncio.sleep(seconds)
            if self.state not in (TurnState.IDLE, TurnState.TURN_END):
                logger.warning("Hard turn timeout reached")
                await self._end_turn("expired")
        except asyncio.CancelledError:
            pass

    # -- Helpers -------------------------------------------------------------

    def _win_bridge(self):
        """Called from gpiozero thread. Bridges into async event loop."""
        if self._loop is None or self._loop.is_closed():
            logger.error("Failed to bridge win callback to event loop")
            return

        self._loop.call_soon_threadsafe(asyncio.create_task, self.handle_win())

    def _build_state_payload(self) -> dict:
        return {
            "state": self.state.value,
            "active_entry_id": self.active_entry_id,
            "current_try": self.current_try,
            "max_tries": self.settings.tries_per_player,
            "try_move_seconds": self.settings.try_move_seconds,
        }
