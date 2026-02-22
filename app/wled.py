"""WLED integration — optional LED control via the WLED JSON API.

Sends fire-and-forget HTTP requests to a WLED device when game events occur.
All calls are non-blocking and failures are logged but never interrupt gameplay.
When ``wled_enabled`` is False (the default) or no IP is configured, every
public method is a silent no-op.

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


class WLEDClient:
    """Async client for controlling a WLED device over its JSON API."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None

    # -- Lifecycle -----------------------------------------------------------

    async def start(self):
        """Create the shared HTTP client.  Safe to call even when WLED is
        disabled — the client is lightweight and only used on demand."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_WLED_TIMEOUT)

    async def close(self):
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

    async def _fire_and_forget(self, payload: dict[str, Any]):
        """Schedule a WLED call that cannot block the caller."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._post_json(payload))
        except RuntimeError:
            logger.warning("No running event loop for WLED fire-and-forget")

    # -- Public API used by the state machine --------------------------------

    async def on_event(self, event: str):
        """Trigger the WLED action mapped to *event*.

        Supported events: ``win``, ``loss``, ``drop``, ``start_turn``,
        ``idle``, ``expire``.

        The admin can configure either a **preset ID** (``wled_preset_<event>``)
        or leave it at 0 to skip that event.  Presets are the recommended
        approach — the admin configures colours/effects/segments in the WLED
        web UI and just references the preset number here.
        """
        if not self._enabled:
            return

        preset = self._preset_for_event(event)
        if preset <= 0:
            logger.debug("WLED: no preset configured for event '%s', skipping", event)
            return

        logger.info("WLED: triggering preset %d for event '%s'", preset, event)
        await self._fire_and_forget({"ps": preset})

    def _preset_for_event(self, event: str) -> int:
        """Look up the preset ID for the given event name."""
        mapping = {
            "win": settings.wled_preset_win,
            "loss": settings.wled_preset_loss,
            "drop": settings.wled_preset_drop,
            "start_turn": settings.wled_preset_start_turn,
            "idle": settings.wled_preset_idle,
            "expire": settings.wled_preset_expire,
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
