#!/usr/bin/env python3
"""
Claw Machine GPIO Watchdog.

Monitors the game server health endpoint. If the server is unresponsive
for WATCHDOG_FAIL_THRESHOLD consecutive checks, forces all GPIO output
pins LOW using lgpio directly.

This process does NOT use gpiozero and does NOT conflict with the game
server's pin ownership during normal operation. It only claims pins
during an emergency.
"""

import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [watchdog] %(message)s",
)
logger = logging.getLogger("watchdog")

# Load config from env or defaults
HEALTH_URL = os.getenv("WATCHDOG_HEALTH_URL", "http://127.0.0.1:8000/api/health")
CHECK_INTERVAL = int(os.getenv("WATCHDOG_CHECK_INTERVAL_S", "2"))
FAIL_THRESHOLD = int(os.getenv("WATCHDOG_FAIL_THRESHOLD", "3"))

# All output pins (BCM numbers) — must match game server config
OUTPUT_PINS = [
    int(os.getenv("PIN_COIN", "17")),
    int(os.getenv("PIN_NORTH", "27")),
    int(os.getenv("PIN_SOUTH", "5")),
    int(os.getenv("PIN_WEST", "6")),
    int(os.getenv("PIN_EAST", "24")),
    int(os.getenv("PIN_DROP", "25")),
]


def force_all_pins_off():
    """Use lgpio directly to force all output pins LOW."""
    try:
        import lgpio
    except ImportError:
        logger.critical("WATCHDOG: lgpio not available — cannot force pins OFF")
        return

    try:
        h = lgpio.gpiochip_open(0)  # gpiochip0 on Pi 5
        for pin in OUTPUT_PINS:
            try:
                lgpio.gpio_claim_output(h, pin, 0)  # Claim and set LOW
            except lgpio.error as e:
                logger.warning(f"Could not claim pin {pin}: {e}")
        lgpio.gpiochip_close(h)
        logger.critical("WATCHDOG: All pins forced OFF")
    except Exception as e:
        logger.critical(f"WATCHDOG: lgpio force-off FAILED: {e}")


def main():
    try:
        import httpx
    except ImportError:
        logger.critical("httpx not installed — watchdog cannot function")
        sys.exit(1)

    fail_count = 0
    triggered = False
    logger.info(f"Watchdog started. Health URL: {HEALTH_URL}")
    logger.info(f"Check interval: {CHECK_INTERVAL}s, threshold: {FAIL_THRESHOLD}")
    logger.info(f"Monitoring pins: {OUTPUT_PINS}")

    while True:
        try:
            with httpx.Client(timeout=2) as client:
                r = client.get(HEALTH_URL)
                if r.status_code == 200:
                    fail_count = 0
                    if triggered:
                        logger.info("Server recovered, resetting watchdog")
                        triggered = False
                else:
                    fail_count += 1
                    logger.warning(
                        f"Health check returned {r.status_code} "
                        f"(fail {fail_count}/{FAIL_THRESHOLD})"
                    )
        except Exception as e:
            fail_count += 1
            logger.warning(
                f"Health check failed: {e} (fail {fail_count}/{FAIL_THRESHOLD})"
            )

        if fail_count >= FAIL_THRESHOLD and not triggered:
            force_all_pins_off()
            triggered = True

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
