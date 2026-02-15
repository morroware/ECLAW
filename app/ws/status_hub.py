"""WebSocket Status Hub — broadcast-only channel for all viewers."""

import json
import logging

from fastapi import WebSocket
from starlette.websockets import WebSocketState

logger = logging.getLogger("ws.status")


class StatusHub:
    def __init__(self):
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.add(ws)
        logger.info(f"Status viewer connected ({len(self._clients)} total)")

    def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)
        logger.info(f"Status viewer disconnected ({len(self._clients)} total)")

    async def broadcast(self, message: dict):
        """Send a message to all connected status viewers."""
        payload = json.dumps(message)
        dead = set()
        for ws in self._clients:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(payload)
            except Exception:
                dead.add(ws)
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
