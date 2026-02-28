"""WLED integration — optional LED control via the WLED JSON API.

Sends HTTP requests to a WLED device when game events occur.
All calls are non-blocking and failures are logged but never interrupt gameplay.
When ``wled_enabled`` is False (the default) or no IP is configured, every
public method is a silent no-op.

Transient events (win, loss, drop, expire) automatically revert to the idle
preset after ``wled_result_display_seconds``.  Persistent events (idle,
start_turn) stay until the next event.  A single background task manages the
revert sequence so requests never race each other.

WLED JSON API reference: https://kno.wled.ge/interfaces/json-api/
"""

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("wled")

# Timeout for WLED HTTP calls — intentionally short so a dead device
# never blocks the game loop.
_WLED_TIMEOUT = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)

# Events that are "temporary" — shown for a configurable duration, then
# the strip auto-reverts to the idle preset.
_TRANSIENT_EVENTS = frozenset({"win", "loss", "drop", "expire", "grab"})


class WLEDClient:
    """Async client for controlling a WLED device over its JSON API."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        # Tracks the single background task responsible for sending a
        # transient preset and later reverting to idle.  Only one such
        # task exists at a time — new events cancel the previous one.
        self._revert_task: asyncio.Task | None = None

    # -- Lifecycle -----------------------------------------------------------

    async def start(self):
        """Create the shared HTTP client.  Safe to call even when WLED is
        disabled — the client is lightweight and only used on demand."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_WLED_TIMEOUT)

    async def close(self):
        self._cancel_revert()
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- Helpers -------------------------------------------------------------

    @property
    def _enabled(self) -> bool:
        return bool(settings.wled_enabled and settings.wled_device_ip)

    @property
    def _base_url(self) -> str:
        ip = settings.wled_device_ip.strip()
        # Normalise: add http:// if missing
        if not ip.startswith("http"):
            ip = f"http://{ip}"
        return ip.rstrip("/")

    def _cancel_revert(self):
        """Cancel any pending revert-to-idle task."""
        if self._revert_task and not self._revert_task.done():
            self._revert_task.cancel()
        self._revert_task = None

    async def _post_json(self, payload: dict[str, Any]) -> bool:
        """POST *payload* to the WLED JSON API.  Returns True on success."""
        if not self._enabled:
            return False
        if self._client is None:
            logger.warning("WLED client not started, skipping request")
            return False
        url = f"{self._base_url}/json/state"
        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
            logger.debug("WLED request OK: %s", payload)
            return True
        except httpx.TimeoutException:
            logger.warning("WLED request timed out (%s)", url)
        except httpx.HTTPStatusError as exc:
            logger.warning("WLED HTTP %s: %s", exc.response.status_code, url)
        except Exception:
            logger.exception("WLED request failed (%s)", url)
        return False

    # -- Public API used by the state machine --------------------------------

    async def on_event(self, event: str):
        """Trigger the WLED action mapped to *event*.

        Supported events: ``win``, ``loss``, ``drop``, ``grab``,
        ``start_turn``, ``idle``, ``expire``.

        **Transient events** (win, loss, drop, expire) display their preset
        for ``wled_result_display_seconds`` then automatically revert to
        the idle preset.  The entire sequence runs in a single background
        task so the game loop is never blocked and requests cannot race.

        **Persistent events** (idle, start_turn) fire immediately and stay
        until the next event.
        """
        if not self._enabled:
            return

        # Always cancel any pending revert — the new event takes priority.
        self._cancel_revert()

        preset = self._preset_for_event(event)
        if preset <= 0:
            logger.debug("WLED: no preset configured for event '%s', skipping", event)
            return

        if event in _TRANSIENT_EVENTS:
            idle_preset = self._preset_for_event("idle")
            delay = settings.wled_result_display_seconds
            logger.info(
                "WLED: triggering preset %d for event '%s' (revert to %s in %.1fs)",
                preset, event,
                idle_preset if idle_preset > 0 else "none",
                delay,
            )
            # Single background task: send preset → wait → send idle.
            # Guarantees ordering because it's sequential within the task.
            self._revert_task = asyncio.create_task(
                self._preset_then_revert(preset, idle_preset, delay, event)
            )
        else:
            # Persistent event — fire in background, no revert.
            logger.info("WLED: triggering preset %d for event '%s'", preset, event)
            asyncio.create_task(self._post_json({"ps": preset}))

    async def _preset_then_revert(
        self, preset_id: int, idle_preset: int, delay: float, event: str
    ):
        """Background task: send *preset_id*, wait *delay* seconds, then
        send *idle_preset*.  Cancelled cleanly when a new event arrives."""
        try:
            ok = await self._post_json({"ps": preset_id})
            if not ok:
                logger.warning("WLED: failed to send preset %d for '%s', skipping revert", preset_id, event)
                return
            # If no idle preset configured or delay is zero, just leave it
            if idle_preset <= 0 or delay <= 0:
                return
            await asyncio.sleep(delay)
            logger.info("WLED: reverting to idle preset %d after '%s'", idle_preset, event)
            await self._post_json({"ps": idle_preset})
        except asyncio.CancelledError:
            # A new event arrived and cancelled us — that's expected.
            pass
        except Exception:
            logger.exception("WLED preset-then-revert failed for event '%s'", event)

    def _preset_for_event(self, event: str) -> int:
        """Look up the preset ID for the given event name."""
        mapping = {
            "win": settings.wled_preset_win,
            "loss": settings.wled_preset_loss,
            "drop": settings.wled_preset_drop,
            "start_turn": settings.wled_preset_start_turn,
            "idle": settings.wled_preset_idle,
            "expire": settings.wled_preset_expire,
            "grab": settings.wled_preset_grab,
        }
        return mapping.get(event, 0)

    # -- Admin / diagnostic helpers ------------------------------------------

    async def test_connection(self) -> dict[str, Any]:
        """Attempt to reach the WLED device and return its info.

        Returns a dict with ``ok`` (bool) and either ``info`` or ``error``.
        """
        if not settings.wled_device_ip:
            return {"ok": False, "error": "No WLED device IP configured"}
        if self._client is None:
            return {"ok": False, "error": "WLED client not started"}

        url = f"{self._base_url}/json/info"
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            info = resp.json()
            return {
                "ok": True,
                "info": {
                    "name": info.get("name", ""),
                    "version": info.get("ver", ""),
                    "led_count": info.get("leds", {}).get("count", 0),
                    "ip": settings.wled_device_ip,
                },
            }
        except httpx.TimeoutException:
            return {"ok": False, "error": "Connection timed out"}
        except httpx.HTTPStatusError as exc:
            return {"ok": False, "error": f"HTTP {exc.response.status_code}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def trigger_preset(self, preset_id: int) -> bool:
        """Activate a specific preset — used by the admin test UI."""
        if preset_id <= 0:
            return False
        return await self._post_json({"ps": preset_id})

    async def set_on(self, on: bool = True) -> bool:
        """Turn the WLED strip on or off."""
        return await self._post_json({"on": on})

    async def set_brightness(self, brightness: int) -> bool:
        """Set brightness (0-255)."""
        return await self._post_json({"bri": max(0, min(255, brightness))})
