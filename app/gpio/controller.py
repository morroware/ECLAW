"""GPIO Controller — wraps gpiozero with async interface and safety features.

When mock_gpio=True in settings, uses a pure-software mock that logs all
GPIO operations. This allows the full application to run on any machine
without real GPIO hardware.
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from app.config import settings

logger = logging.getLogger("gpio")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpio")

OPPOSING = {
    "north": "south",
    "south": "north",
    "east": "west",
    "west": "east",
}


class MockOutputDevice:
    """Simulates a GPIO output device for PoC/testing."""

    def __init__(self, pin: int, initial_value: bool = False):
        self.pin = pin
        self.value = initial_value
        logger.debug(f"[MOCK] OutputDevice created on pin {pin}")

    def on(self):
        self.value = True
        logger.debug(f"[MOCK] Pin {self.pin} ON")

    def off(self):
        self.value = False
        logger.debug(f"[MOCK] Pin {self.pin} OFF")

    def close(self):
        logger.debug(f"[MOCK] Pin {self.pin} closed")


class MockInputDevice:
    """Simulates a GPIO input device for PoC/testing."""

    def __init__(self, pin: int, pull_up: bool = False, bounce_time: float | None = None):
        self.pin = pin
        self.when_activated = None
        logger.debug(f"[MOCK] InputDevice created on pin {pin}")

    def close(self):
        logger.debug(f"[MOCK] InputDevice pin {self.pin} closed")


class GPIOController:
    def __init__(self):
        self._outputs: dict[str, object] = {}
        self._active_holds: dict[str, asyncio.Task] = {}
        self._last_pulse: dict[str, float] = {}
        self._locked = False
        self._initialized = False
        self._win_input = None

    # -- Lifecycle -----------------------------------------------------------

    async def initialize(self):
        """Call once at server startup."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, self._init_devices)
        self._initialized = True
        logger.info("GPIO controller initialized (mock=%s)", settings.mock_gpio)

    def _init_devices(self):
        """Create all GPIO devices (real or mock)."""
        pin_map = {
            "coin": settings.pin_coin,
            "north": settings.pin_north,
            "south": settings.pin_south,
            "west": settings.pin_west,
            "east": settings.pin_east,
            "drop": settings.pin_drop,
        }

        if settings.mock_gpio:
            OutputDevice = MockOutputDevice
            InputDevice = MockInputDevice
        else:
            from gpiozero import DigitalInputDevice, OutputDevice as RealOutputDevice
            OutputDevice = RealOutputDevice
            InputDevice = DigitalInputDevice

        for name, pin in pin_map.items():
            self._outputs[name] = OutputDevice(pin, initial_value=False)
            self._last_pulse[name] = 0.0

        if settings.mock_gpio:
            self._win_input = MockInputDevice(settings.pin_win, pull_up=False, bounce_time=0.1)
        else:
            self._win_input = InputDevice(settings.pin_win, pull_up=False, bounce_time=0.1)

    async def cleanup(self):
        """Call on server shutdown. Forces all OFF, closes devices."""
        await self.emergency_stop()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, self._close_devices)
        logger.info("GPIO controller cleaned up")

    def _close_devices(self):
        for dev in self._outputs.values():
            dev.off()
            dev.close()
        if self._win_input:
            self._win_input.close()

    # -- Emergency Stop ------------------------------------------------------

    async def emergency_stop(self):
        """Immediately turn all outputs OFF. Cancel all hold tasks."""
        self._locked = True
        for task in self._active_holds.values():
            task.cancel()
        self._active_holds.clear()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, self._all_off)
        logger.warning("EMERGENCY STOP: all outputs OFF")

    def _all_off(self):
        for dev in self._outputs.values():
            dev.off()

    async def unlock(self):
        self._locked = False
        logger.info("GPIO controls unlocked")

    # -- Direction Hold ------------------------------------------------------

    async def direction_on(self, direction: str) -> bool:
        """Start holding a direction. Returns False if rejected."""
        if self._locked or direction not in OPPOSING:
            return False

        opposite = OPPOSING[direction]
        if opposite in self._active_holds:
            if settings.direction_conflict_mode == "ignore_new":
                return False
            else:
                await self.direction_off(opposite)

        if direction in self._active_holds:
            return True

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, self._outputs[direction].on)
        logger.debug(f"Direction ON: {direction}")

        task = asyncio.create_task(
            self._hold_timeout(direction, settings.direction_hold_max_ms / 1000.0)
        )
        self._active_holds[direction] = task
        return True

    async def direction_off(self, direction: str) -> bool:
        """Release a direction."""
        if direction in self._active_holds:
            self._active_holds[direction].cancel()
            del self._active_holds[direction]
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, self._outputs[direction].off)
        logger.debug(f"Direction OFF: {direction}")
        return True

    async def _hold_timeout(self, direction: str, timeout: float):
        """Safety: auto-release after max hold time."""
        try:
            await asyncio.sleep(timeout)
            logger.warning(f"Hold timeout reached for {direction}, forcing OFF")
            await self.direction_off(direction)
        except asyncio.CancelledError:
            pass

    async def all_directions_off(self):
        """Release all directions. Call on turn transitions."""
        for d in list(self._active_holds.keys()):
            await self.direction_off(d)

    # -- Pulse Outputs -------------------------------------------------------

    async def pulse(self, name: str) -> bool:
        """Fire a pulse output (coin or drop). Returns False if rejected."""
        if self._locked or name not in ("coin", "drop"):
            return False

        now = time.monotonic()
        elapsed_ms = (now - self._last_pulse.get(name, 0)) * 1000
        if elapsed_ms < settings.min_inter_pulse_ms:
            logger.debug(f"Pulse {name} rejected: cooldown ({elapsed_ms:.0f}ms)")
            return False

        duration_ms = settings.coin_pulse_ms if name == "coin" else settings.drop_pulse_ms
        self._last_pulse[name] = now

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, self._do_pulse, name, duration_ms)
        logger.info(f"Pulse {name}: {duration_ms}ms")
        return True

    def _do_pulse(self, name: str, duration_ms: int):
        dev = self._outputs[name]
        dev.on()
        time.sleep(duration_ms / 1000.0)
        dev.off()

    # -- Win Input -----------------------------------------------------------

    def register_win_callback(self, callback):
        """Register a callback for win detection."""
        if self._win_input:
            self._win_input.when_activated = callback

    def unregister_win_callback(self):
        if self._win_input:
            self._win_input.when_activated = None

    # -- Status --------------------------------------------------------------

    @property
    def active_directions(self) -> list[str]:
        return list(self._active_holds.keys())

    @property
    def is_locked(self) -> bool:
        return self._locked
