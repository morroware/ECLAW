"""WebSocket Status Hub — broadcast-only channel for all viewers."""

import asyncio
import json
import logging

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.config import settings

logger = logging.getLogger("ws.status")


class StatusHub:
    def __init__(self):
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> bool:
        """Accept a viewer connection. Returns False if limit reached."""
        if len(self._clients) >= settings.max_status_viewers:
            await ws.accept()
            await ws.close(1013, "Too many viewers")
            return False
        await ws.accept()
        self._clients.add(ws)
        logger.info(f"Status viewer connected ({len(self._clients)} total)")
        return True

    def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)
        logger.info(f"Status viewer disconnected ({len(self._clients)} total)")

    async def broadcast(self, message: dict):
        """Send a message to all connected status viewers concurrently.

        Each send has a per-client timeout so a single slow/stalled
        connection cannot block delivery to the remaining viewers.
        """
        if not self._clients:
            return
        payload = json.dumps(message)
        dead = set()

        async def _send(ws: WebSocket):
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await asyncio.wait_for(ws.send_text(payload), timeout=settings.status_send_timeout_s)
            except Exception:
                dead.add(ws)

        await asyncio.gather(*[_send(ws) for ws in self._clients])
        self._clients -= dead

    async def broadcast_state(self, state, payload: dict):
        await self.broadcast({"type": "state_update", **payload})

    async def broadcast_turn_end(self, entry_id: str, result: str):
        await self.broadcast({"type": "turn_end", "entry_id": entry_id, "result": result})

    async def broadcast_queue_update(self, status: dict, queue_entries: list[dict] | None = None):
        msg = {"type": "queue_update", **status, "viewer_count": self.viewer_count}
        if queue_entries is not None:
            msg["entries"] = queue_entries
        await self.broadcast(msg)

    async def notify_player_ready(self, entry_id: str):
        # Actual notification goes through ControlHandler.
        # This is a no-op on the broadcast hub side.
        pass

    @property
    def viewer_count(self) -> int:
        return len(self._clients)
