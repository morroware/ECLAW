"""WebSocket Control Handler — authenticated bidirectional channel for players."""

import asyncio
import json
import logging
import time

from fastapi import WebSocket

from app.database import hash_token

logger = logging.getLogger("ws.control")

VALID_DIRECTIONS = {"north", "south", "east", "west"}

# Hard cap on total simultaneous control WebSocket connections.
# Each queued/active player opens one.  The limit prevents resource
# exhaustion if many unauthenticated connections arrive before the
# 10-second auth timeout ejects them.
_MAX_CONTROL_CONNECTIONS = 100


class ControlHandler:
    def __init__(self, state_machine, queue_manager, gpio_controller, settings):
        self.sm = state_machine
        self.queue = queue_manager
        self.gpio = gpio_controller
        self.settings = settings
        self._player_ws: dict[str, WebSocket] = {}  # entry_id -> ws
        self._last_command_time: dict[str, float] = {}  # entry_id -> monotonic
        self._grace_tasks: dict[str, asyncio.Task] = {}  # entry_id -> grace period task
        self._total_connections: int = 0

    async def handle_connection(self, ws: WebSocket):
        """Handle a full control WebSocket lifecycle."""
        if self._total_connections >= _MAX_CONTROL_CONNECTIONS:
            await ws.accept()
            await ws.close(1013, "Too many connections")
            return

        await ws.accept()
        self._total_connections += 1
        entry_id = None
        try:
            # First message must be auth
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
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

            # Main message loop
            async for raw_msg in ws.iter_text():
                await self._handle_message(entry_id, raw_msg, ws)

        except asyncio.TimeoutError:
            await ws.close(1008)
        except Exception as e:
            logger.error(f"Control WS error for {entry_id}: {e}")
        finally:
            self._total_connections = max(0, self._total_connections - 1)
            if entry_id:
                # Only clean up if this WS is still the registered one
                # (a new connection may have already replaced us)
                if self._player_ws.get(entry_id) is ws:
                    self._player_ws.pop(entry_id, None)
                    self._last_command_time.pop(entry_id, None)
                    if entry_id == self.sm.active_entry_id:
                        await self.sm.handle_disconnect(entry_id)
                        # Start grace period for active player
                        task = asyncio.create_task(
                            self._disconnect_grace(entry_id, self.settings.queue_grace_period_seconds)
                        )
                        self._grace_tasks[entry_id] = task

    async def _handle_message(self, entry_id: str, raw: str, ws: WebSocket):
        if len(raw) > 1024:
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
        # drop, keyup, and ready_confirm always pass through.
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

        # Control messages (active player only)
        if msg_type == "keydown" and msg.get("key") in VALID_DIRECTIONS:
            if self.sm.state.value == "moving":
                ok = await self.gpio.direction_on(msg["key"])
                await ws.send_text(json.dumps({
                    "type": "control_ack", "key": msg["key"], "active": ok
                }))

        elif msg_type == "keyup" and msg.get("key") in VALID_DIRECTIONS:
            if self.sm.state.value == "moving":
                await self.gpio.direction_off(msg["key"])

        elif msg_type == "drop_start":
            await self.sm.handle_drop_press(entry_id)

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

    async def send_to_player(self, entry_id: str, message: dict):
        """Send a message to a specific player."""
        ws = self._player_ws.get(entry_id)
        if ws:
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                pass

    async def send_latency_ping(self, entry_id: str):
        await self.send_to_player(entry_id, {
            "type": "latency_ping", "server_time": time.time()
        })

    def is_player_connected(self, entry_id: str) -> bool:
        return entry_id in self._player_ws
