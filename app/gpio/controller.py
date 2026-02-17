"""GPIO Controller — wraps gpiozero with async interface and safety features.

When mock_gpio=True in settings, uses a pure-software mock that logs all
GPIO operations. This allows the full application to run on any machine
without real GPIO hardware.

Executor auto-recovery
~~~~~~~~~~~~~~~~~~~~~~
All hardware calls are funnelled through a single-threaded
``ThreadPoolExecutor`` to serialise access to the GPIO chip.  If *any*
lgpio call blocks (bus contention, kernel driver hiccup, hardware latch-up)
the executor thread is permanently dead and every subsequent GPIO operation
would hang behind it forever.

``_gpio_call()`` wraps every executor submission with a timeout.  When a
timeout fires the dead executor is replaced with a fresh one so the next
operation runs on a new thread.  The lgpio chip handle stays valid across
threads (it's a process-level file descriptor), so existing ``OutputDevice``
objects continue to work on the replacement executor.
"""

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from app.config import settings

logger = logging.getLogger("gpio")

# Timeouts for executor calls.  These must be generous enough for normal
# operations but short enough that a stuck thread is detected quickly.
_GPIO_OP_TIMEOUT = 2.0      # simple on/off/read
_GPIO_PULSE_TIMEOUT = 5.0   # pulse includes time.sleep() in the thread
_GPIO_INIT_TIMEOUT = 10.0   # device initialisation / teardown

OPPOSING = {
    "north": "south",
    "south": "north",
    "east": "west",
    "west": "east",
}


class MockOutputDevice:
    """Simulates a GPIO output device for PoC/testing."""

    def __init__(self, pin: int, active_high: bool = True, initial_value: bool = False):
        self.pin = pin
        self.value = initial_value
        logger.debug(f"[MOCK] OutputDevice created on pin {pin} (active_high={active_high})")

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
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="gpio"
        )

    # -- Executor helper -----------------------------------------------------

    async def _gpio_call(self, func, *args, timeout: float = _GPIO_OP_TIMEOUT) -> bool:
        """Run a synchronous GPIO function in the executor with a timeout.

        Returns ``True`` on success, ``False`` on timeout or error.

        On timeout the executor thread is presumed dead (stuck in an lgpio
        syscall) and is replaced with a fresh one so subsequent calls are
        not permanently blocked.
        """
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(self._executor, func, *args),
                timeout=timeout,
            )
            return True
        except asyncio.TimeoutError:
            logger.error(
                "GPIO %s timed out after %.1fs — replacing executor",
                getattr(func, '__name__', str(func)), timeout,
            )
            self._replace_executor()
            return False
        except Exception:
            logger.exception(
                "GPIO %s failed", getattr(func, '__name__', str(func)),
            )
            return False

    def _replace_executor(self):
        """Abandon a stuck executor and create a fresh one.

        The old thread may still be blocked inside an lgpio call — there is
        no way to kill it from Python.  ``shutdown(wait=False)`` tells the
        pool to stop accepting work; the stuck thread will be reaped when
        the process exits.
        """
        old = self._executor
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="gpio"
        )
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        logger.warning("GPIO executor replaced — old thread may still be blocked")

    # -- Lifecycle -----------------------------------------------------------

    async def initialize(self):
        """Call once at server startup."""
        if not await self._gpio_call(self._init_devices, timeout=_GPIO_INIT_TIMEOUT):
            logger.error("GPIO initialisation failed — hardware may not work")
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
            os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
            from gpiozero import DigitalInputDevice, OutputDevice as RealOutputDevice
            OutputDevice = RealOutputDevice
            InputDevice = DigitalInputDevice

        active_high = not settings.relay_active_low
        for name, pin in pin_map.items():
            self._outputs[name] = OutputDevice(pin, active_high=active_high, initial_value=False)
            self._last_pulse[name] = 0.0

        if settings.mock_gpio:
            self._win_input = MockInputDevice(settings.pin_win, pull_up=False, bounce_time=0.1)
        else:
            self._win_input = InputDevice(settings.pin_win, pull_up=False, bounce_time=0.1)

    async def cleanup(self):
        """Call on server shutdown. Forces all OFF, closes devices."""
        await self.emergency_stop()
        await self._gpio_call(self._close_devices, timeout=_GPIO_INIT_TIMEOUT)
        logger.info("GPIO controller cleaned up")

    def _close_devices(self):
        for name, dev in self._outputs.items():
            try:
                dev.off()
                dev.close()
            except Exception:
                logger.exception("Failed to close GPIO device %s", name)
        if self._win_input:
            try:
                self._win_input.close()
            except Exception:
                logger.exception("Failed to close win input device")

    # -- Emergency Stop ------------------------------------------------------

    async def emergency_stop(self):
        """Immediately turn all outputs OFF. Cancel all hold tasks.

        This method is designed to NEVER raise — it logs errors internally.
        ``_locked`` is set True at the start and must be cleared by the
        caller (typically ``_end_turn`` sets ``_locked = False`` directly).
        """
        self._locked = True
        for task in self._active_holds.values():
            task.cancel()
        self._active_holds.clear()
        if await self._gpio_call(self._all_off):
            logger.warning("EMERGENCY STOP: all outputs OFF")
        else:
            logger.error("EMERGENCY STOP: _all_off failed (GPIO may be in bad state)")

    def _all_off(self):
        for name, dev in self._outputs.items():
            try:
                dev.off()
            except Exception:
                # Continue turning off remaining devices even if one fails.
                logger.exception("Failed to turn off GPIO device %s", name)

    async def unlock(self):
        self._locked = False
        logger.info("GPIO controls unlocked")

    # -- Direction Hold ------------------------------------------------------

    async def direction_on(self, direction: str) -> bool:
        """Start holding a direction. Returns False if rejected or on error."""
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

        if not await self._gpio_call(self._outputs[direction].on):
            return False
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
        if not await self._gpio_call(self._outputs[direction].off):
            return False
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

    # -- Drop Hold -----------------------------------------------------------

    async def drop_on(self) -> bool:
        """Turn on the drop relay (hold). Returns False if rejected or on error."""
        if self._locked:
            return False
        if not await self._gpio_call(self._outputs["drop"].on):
            return False
        logger.debug("Drop relay ON (hold)")
        return True

    async def drop_off(self) -> bool:
        """Turn off the drop relay."""
        if not await self._gpio_call(self._outputs["drop"].off):
            return False
        logger.debug("Drop relay OFF")
        return True

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

        if not await self._gpio_call(self._do_pulse, name, duration_ms,
                                     timeout=_GPIO_PULSE_TIMEOUT):
            return False
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
