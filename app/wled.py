"""WLED integration — optional LED control via the WLED JSON API.

Sends HTTP requests to a WLED device when game events occur.
All calls are non-blocking and failures are logged but never interrupt gameplay.
When ``wled_enabled`` is False (the default) or no IP is configured, every
public method is a silent no-op.

Transient events (win, loss, drop, expire, grab) automatically revert to the
idle preset after ``wled_result_display_seconds``. Persistent events (idle,
start_turn) stay until the next event.

Requests are serialized through a single sender worker so WLED updates are
applied in order. This avoids races between overlapping game events.

WLED JSON API reference: https://kno.wled.ge/interfaces/json-api/
"""

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("wled")

# Events that are "temporary" — shown for a configurable duration, then
# the strip auto-reverts to the idle preset.
_TRANSIENT_EVENTS = frozenset({"win", "loss", "drop", "expire", "grab"})


class WLEDClient:
    """Async client for controlling a WLED device over its JSON API."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._revert_task: asyncio.Task | None = None
        self._sender_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[tuple[dict[str, Any], asyncio.Future[bool] | None] | None] = asyncio.Queue()

    # -- Lifecycle -----------------------------------------------------------

    async def start(self):
        """Create the shared HTTP client and sender worker."""
        if self._client is None:
            timeout = httpx.Timeout(
                connect=settings.wled_connect_timeout_s,
                read=settings.wled_read_timeout_s,
                write=settings.wled_write_timeout_s,
                pool=settings.wled_pool_timeout_s,
            )
            self._client = httpx.AsyncClient(timeout=timeout)
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(self._sender_loop())

    async def close(self):
        self._cancel_revert()
        await self._stop_sender()
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _stop_sender(self):
        if self._sender_task is None:
            return
        if not self._sender_task.done():
            try:
                self._queue.put_nowait(None)
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._sender_task, timeout=1.0)
            except Exception:
                self._sender_task.cancel()
        self._sender_task = None

    # -- Helpers -------------------------------------------------------------

    @property
    def _enabled(self) -> bool:
        return bool(settings.wled_enabled and settings.wled_device_ip)

    @property
    def _base_url(self) -> str:
        ip = settings.wled_device_ip.strip()
        if not ip.startswith("http"):
            ip = f"http://{ip}"
        return ip.rstrip("/")

    def _cancel_revert(self):
        if self._revert_task and not self._revert_task.done():
            self._revert_task.cancel()
        self._revert_task = None

    async def _sender_loop(self):
        while True:
            item = await self._queue.get()
            if item is None:
                break
            payload, fut = item
            ok = await self._post_json(payload)
            if fut is not None and not fut.done():
                fut.set_result(ok)

    async def _queue_post(self, payload: dict[str, Any], wait: bool = False) -> bool:
        if not self._enabled:
            return False
        if self._sender_task is None or self._sender_task.done():
            logger.warning("WLED sender not running, skipping request")
            return False

        if not wait:
            self._queue.put_nowait((payload, None))
            return True

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._queue.put_nowait((payload, fut))
        return await fut

    async def _post_json(self, payload: dict[str, Any]) -> bool:
        if not self._enabled:
            return False
        if self._client is None:
            logger.warning("WLED client not started, skipping request")
            return False

        url = f"{self._base_url}/json/state"
        retries = max(0, settings.wled_http_retries)
        backoff = max(0.0, settings.wled_retry_backoff_seconds)

        for attempt in range(retries + 1):
            try:
                resp = await self._client.post(url, json=payload)
                resp.raise_for_status()
                logger.debug("WLED request OK: %s", payload)
                return True
            except httpx.TimeoutException:
                logger.warning("WLED request timed out (%s), attempt %d/%d", url, attempt + 1, retries + 1)
            except httpx.HTTPStatusError as exc:
                logger.warning("WLED HTTP %s: %s", exc.response.status_code, url)
                return False
            except Exception:
                logger.exception("WLED request failed (%s)", url)

            if attempt < retries and backoff > 0:
                await asyncio.sleep(backoff)

        return False

    # -- Public API used by the state machine --------------------------------

    async def on_event(self, event: str):
        if not self._enabled:
            return

        self._cancel_revert()
        preset = self._preset_for_event(event)
        if preset <= 0:
            logger.debug("WLED: no preset configured for event '%s', skipping", event)
            return

        if event in _TRANSIENT_EVENTS:
            idle_preset = self._preset_for_event("idle")
            delay = max(0.0, settings.wled_result_display_seconds)
            logger.info(
                "WLED: triggering preset %d for event '%s' (revert to %s in %.1fs)",
                preset,
                event,
                idle_preset if idle_preset > 0 else "none",
                delay,
            )
            self._revert_task = asyncio.create_task(
                self._preset_then_revert(preset, idle_preset, delay, event)
            )
            return

        logger.info("WLED: triggering preset %d for event '%s'", preset, event)
        await self._queue_post({"ps": preset}, wait=False)

    async def _preset_then_revert(self, preset_id: int, idle_preset: int, delay: float, event: str):
        try:
            ok = await self._queue_post({"ps": preset_id}, wait=True)
            if not ok:
                logger.warning("WLED: failed to send preset %d for '%s', skipping revert", preset_id, event)
                return
            if idle_preset <= 0 or delay <= 0:
                return
            await asyncio.sleep(delay)
            logger.info("WLED: reverting to idle preset %d after '%s'", idle_preset, event)
            await self._queue_post({"ps": idle_preset}, wait=False)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("WLED preset-then-revert failed for event '%s'", event)

    def _preset_for_event(self, event: str) -> int:
        event_specific = {
            "win": settings.wled_preset_win,
            "loss": settings.wled_preset_loss,
            "drop": settings.wled_preset_drop,
            "start_turn": settings.wled_preset_start_turn,
            "idle": settings.wled_preset_idle,
            "expire": settings.wled_preset_expire,
            "grab": settings.wled_preset_grab,
        }
        preset = event_specific.get(event, 0)
        if preset > 0:
            return preset

        if event in _TRANSIENT_EVENTS:
            return settings.wled_preset_result
        return 0

    # -- Admin / diagnostic helpers ------------------------------------------

    async def test_connection(self) -> dict[str, Any]:
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
        if preset_id <= 0:
            return False
        return await self._queue_post({"ps": preset_id}, wait=True)

    async def set_on(self, on: bool = True) -> bool:
        return await self._queue_post({"on": on}, wait=True)

    async def set_brightness(self, brightness: int) -> bool:
        return await self._queue_post({"bri": max(0, min(255, brightness))}, wait=True)
