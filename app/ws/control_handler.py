"""WebSocket Control Handler — authenticated bidirectional channel for players."""

import asyncio
import json
import logging
import time

from fastapi import WebSocket

from app.database import hash_token

logger = logging.getLogger("ws.control")

VALID_DIRECTIONS = {"north", "south", "east", "west"}


class ControlHandler:
    def __init__(self, state_machine, queue_manager, gpio_controller, settings):
        self.sm = state_machine
        self.queue = queue_manager
        self.gpio = gpio_controller
        self.settings = settings
        self._player_ws: dict[str, WebSocket] = {}  # entry_id -> ws
        self._last_command_time: dict[str, float] = {}  # entry_id -> monotonic
        self._grace_tasks: dict[str, asyncio.Task] = {}  # entry_id -> grace period task
        self._last_activity: dict[str, float] = {}  # entry_id -> monotonic (any msg)
        self._conn_sem = asyncio.Semaphore(settings.max_control_connections)

    async def handle_connection(self, ws: WebSocket):
        """Handle a full control WebSocket lifecycle."""
        if self._conn_sem.locked():
            await ws.accept()
            await ws.close(1013, "Too many connections")
            return

        await self._conn_sem.acquire()
        await ws.accept()
        entry_id = None
        try:
            # First message must be auth
            raw = await asyncio.wait_for(ws.receive_text(), timeout=self.settings.control_auth_timeout_s)
            msg = json.loads(raw)
            if msg.get("type") != "auth" or "token" not in msg:
                await ws.send_text(json.dumps({"type": "error", "message": "Auth required"}))
                await ws.close(1008)
                return

            token_hash = hash_token(msg["token"])
            entry = await self.queue.get_by_token(token_hash)
            if not entry:
                await ws.send_text(json.dumps({"type": "error", "message": "Invalid token"}))
                await ws.close(1008)
                return

            entry_id = entry["id"]

            # Cancel any active grace period for this player (they reconnected)
            grace = self._grace_tasks.pop(entry_id, None)
            if grace:
                grace.cancel()
                logger.info(f"Player {entry_id} reconnected, cancelled grace period")

            # Handle duplicate tabs: close previous connection
            if entry_id in self._player_ws:
                try:
                    await self._player_ws[entry_id].close(1000, "Replaced by new connection")
                except Exception:
                    pass
            self._player_ws[entry_id] = ws

            await ws.send_text(json.dumps({
                "type": "auth_ok",
                "state": entry["state"],
                "position": entry["position"],
            }))

            # If this player is the active player, send the current game state
            # so they can resume after a page refresh (correct try counter,
            # timer, etc.).
            if entry_id == self.sm.active_entry_id:
                payload = self.sm._build_state_payload()
                await ws.send_text(json.dumps({
                    "type": "state_update", **payload
                }))

            logger.info(f"Player {entry_id} connected (state={entry['state']})")

            # Start keepalive ping loop for liveness detection.
            # Records last activity time and proactively closes the
            # socket when no pong (or any message) is received within
            # the liveness threshold.
            self._last_activity[entry_id] = time.monotonic()
            ping_task = asyncio.create_task(
                self._keepalive_ping(entry_id, ws)
            )

            # Main message loop
            try:
                async for raw_msg in ws.iter_text():
                    self._last_activity[entry_id] = time.monotonic()
                    await self._handle_message(entry_id, raw_msg, ws)
            finally:
                ping_task.cancel()

        except asyncio.TimeoutError:
            await ws.close(1008)
        except Exception as e:
            logger.error(f"Control WS error for {entry_id}: {e}")
        finally:
            self._conn_sem.release()
            if entry_id:
                # Only clean up if this WS is still the registered one
                # (a new connection may have already replaced us)
                if self._player_ws.get(entry_id) is ws:
                    self._player_ws.pop(entry_id, None)
                    self._last_command_time.pop(entry_id, None)
                    self._last_activity.pop(entry_id, None)
                    if entry_id == self.sm.active_entry_id:
                        await self.sm.handle_disconnect(entry_id)
                        # Only start the long grace period for truly active
                        # players (MOVING, DROPPING, POST_DROP).  Players in
                        # READY_PROMPT will be handled by the ready timeout
                        # (15s) — giving them a 300s grace period would stall
                        # the queue for 5 minutes when someone navigates away
                        # before confirming ready.
                        from app.game.state_machine import TurnState
                        if self.sm.state in (TurnState.MOVING, TurnState.DROPPING, TurnState.POST_DROP):
                            task = asyncio.create_task(
                                self._disconnect_grace(entry_id, self.settings.queue_grace_period_seconds)
                            )
                            self._grace_tasks[entry_id] = task

    async def _handle_message(self, entry_id: str, raw: str, ws: WebSocket):
        if len(raw) > self.settings.control_max_message_bytes:
            return  # Reject oversized messages
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")

        # Latency ping: respond immediately, bypass rate limit
        if msg_type == "latency_ping":
            await ws.send_text(json.dumps({"type": "latency_pong"}))
            return

        # Rate limit keydown events (held directions fire rapidly).
        # drop_start, drop_end, keyup, and ready_confirm always pass through.
        if msg_type == "keydown":
            now = time.monotonic()
            last = self._last_command_time.get(entry_id, 0)
            min_interval = 1.0 / self.settings.command_rate_limit_hz
            if now - last < min_interval:
                return  # Silently drop
            self._last_command_time[entry_id] = now

        # Non-active player actions
        if entry_id != self.sm.active_entry_id:
            if msg_type == "ready_confirm":
                await self.sm.handle_ready_confirm(entry_id)
            return

        # Control messages (active player only).
        # GPIO operations are wrapped so that a hardware error does not crash
        # the player's WebSocket connection — the turn continues and the
        # periodic checker will recover if the state machine gets stuck.
        if msg_type == "keydown" and msg.get("key") in VALID_DIRECTIONS:
            if self.sm.state.value == "moving":
                try:
                    ok = await self.gpio.direction_on(msg["key"])
                except Exception:
                    logger.exception("GPIO error during direction_on")
                    ok = False
                await ws.send_text(json.dumps({
                    "type": "control_ack", "key": msg["key"], "active": ok
                }))

        elif msg_type == "keyup" and msg.get("key") in VALID_DIRECTIONS:
            if self.sm.state.value == "moving":
                try:
                    await self.gpio.direction_off(msg["key"])
                except Exception:
                    logger.exception("GPIO error during direction_off")

        elif msg_type == "drop_start":
            await self.sm.handle_drop_press(entry_id)

        elif msg_type == "drop_end":
            await self.sm.handle_drop_release(entry_id)

        elif msg_type == "ready_confirm":
            await self.sm.handle_ready_confirm(entry_id)

    async def _disconnect_grace(self, entry_id: str, grace_seconds: int):
        """Wait for reconnection. If not reconnected, end turn."""
        try:
            await asyncio.sleep(grace_seconds)
            if entry_id not in self._player_ws and entry_id == self.sm.active_entry_id:
                logger.info(f"Grace period expired for {entry_id}")
                await self.sm.handle_disconnect_timeout(entry_id)
        except asyncio.CancelledError:
            pass
        finally:
            self._grace_tasks.pop(entry_id, None)

    async def _keepalive_ping(self, entry_id: str, ws: WebSocket):
        """Periodic ping for control channel liveness detection.

        Sends an application-level ping every control_ping_interval_s seconds.
        If no message (including the pong reply) has been received within
        control_liveness_timeout_s, the socket is presumed half-open and
        closed so disconnect handling fires promptly.
        """
        try:
            while True:
                await asyncio.sleep(self.settings.control_ping_interval_s)
                # Check liveness: has any message arrived recently?
                last = self._last_activity.get(entry_id, 0)
                if time.monotonic() - last > self.settings.control_liveness_timeout_s:
                    logger.warning(
                        "Control keepalive: no activity from %s for >%ds, closing",
                        entry_id, self.settings.control_liveness_timeout_s,
                    )
                    try:
                        await ws.close(1001, "Liveness timeout")
                    except Exception:
                        pass
                    return
                # Send application-level ping
                try:
                    await asyncio.wait_for(
                        ws.send_text(json.dumps({"type": "ping"})),
                        timeout=self.settings.control_send_timeout_s,
                    )
                except (asyncio.TimeoutError, Exception):
                    logger.warning("Control keepalive: ping send failed for %s", entry_id)
                    try:
                        await ws.close(1001, "Ping send failed")
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            pass

    async def send_to_player(self, entry_id: str, message: dict):
        """Send a message to a specific player.

        Uses a bounded timeout so a stalled control socket cannot block
        state-machine transitions.  On timeout or error the socket is
        closed and evicted immediately.
        """
        ws = self._player_ws.get(entry_id)
        if ws:
            try:
                await asyncio.wait_for(
                    ws.send_text(json.dumps(message)), timeout=self.settings.control_send_timeout_s
                )
            except (asyncio.TimeoutError, Exception):
                logger.warning("send_to_player: evicting dead socket for %s", entry_id)
                self._player_ws.pop(entry_id, None)
                try:
                    await ws.close(1001, "Send timeout")
                except Exception:
                    pass

    async def send_latency_ping(self, entry_id: str):
        await self.send_to_player(entry_id, {
            "type": "latency_ping", "server_time": time.time()
        })

    def is_player_connected(self, entry_id: str) -> bool:
        return entry_id in self._player_ws
