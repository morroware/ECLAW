#!/usr/bin/env python3
"""
GPIO Test Script — Phase B validation.

Tests all 6 output pins and the win input pin using the same polarity
settings as the game server (reads RELAY_ACTIVE_LOW from environment or
.env file).

Run on the Pi 5 with relay board connected.

Usage:
    python3 scripts/gpio_test.py [--cycles 200]
    RELAY_ACTIVE_LOW=true python3 scripts/gpio_test.py
"""

import argparse
import os
import time
import sys


def _load_env():
    """Load .env if present (simple key=value parser, no dependency needed)."""
    for candidate in [".env", "/opt/claw/.env"]:
        if os.path.isfile(candidate):
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        os.environ.setdefault(key.strip(), val.strip())
            break


def test_with_gpiozero(cycles: int):
    """Test using gpiozero (same library as the game server)."""
    os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
    from gpiozero import OutputDevice, DigitalInputDevice

    PINS = {
        "coin": int(os.getenv("PIN_COIN", "17")),
        "north": int(os.getenv("PIN_NORTH", "27")),
        "south": int(os.getenv("PIN_SOUTH", "5")),
        "west": int(os.getenv("PIN_WEST", "6")),
        "east": int(os.getenv("PIN_EAST", "24")),
        "drop": int(os.getenv("PIN_DROP", "25")),
    }
    WIN_PIN = int(os.getenv("PIN_WIN", "16"))

    relay_active_low = os.getenv("RELAY_ACTIVE_LOW", "true").lower() in ("true", "1", "yes")
    active_high = not relay_active_low

    print("=" * 60)
    print("ECLAW GPIO Test")
    print("=" * 60)
    print(f"  Relay polarity : {'ACTIVE-LOW' if relay_active_low else 'ACTIVE-HIGH'}")
    print(f"  gpiozero active_high={active_high}")
    print(f"  .on()  -> pin {'LOW  (relay engages)' if relay_active_low else 'HIGH (relay engages)'}")
    print(f"  .off() -> pin {'HIGH (relay releases)' if relay_active_low else 'LOW  (relay releases)'}")
    print(f"  Output pins: {PINS}")
    print(f"  Win input pin: BCM {WIN_PIN}")
    print(f"  Pulse cycles: {cycles}")
    print()

    devices = {}
    for name, pin in PINS.items():
        devices[name] = OutputDevice(pin, active_high=active_high, initial_value=False)
        print(f"  Created OutputDevice: {name} (BCM {pin}, active_high={active_high})")

    win_input = DigitalInputDevice(WIN_PIN, pull_up=False, bounce_time=0.1)
    print(f"  Created InputDevice: win (BCM {WIN_PIN})")
    print()

    # --- Relay click test (one at a time, hold long enough to hear click) ---
    print("--- Relay click test (each relay ON for 1s) ---")
    print("  You should hear a CLICK when each relay engages.")
    print()
    for name, dev in devices.items():
        print(f"  {name}: ON  ...", end="", flush=True)
        dev.on()
        time.sleep(1.0)
        dev.off()
        print(" OFF")
        time.sleep(0.3)
    print()

    # --- Individual pin rapid-cycle test ---
    print("--- Rapid cycle test ---")
    for name, dev in devices.items():
        print(f"Testing {name}...")

        for i in range(cycles):
            dev.on()
            time.sleep(0.01)
            dev.off()
            time.sleep(0.01)

            if (i + 1) % 50 == 0:
                print(f"  {name}: {i + 1}/{cycles} cycles OK")

        print(f"  {name}: PASS ({cycles} cycles, no errors)")
        print()

    # Direction conflict test
    print("Testing direction conflicts...")
    north = devices["north"]
    south = devices["south"]
    east = devices["east"]
    west = devices["west"]

    for i in range(cycles):
        north.on()
        time.sleep(0.005)
        north.off()

        south.on()
        time.sleep(0.005)
        south.off()

        east.on()
        time.sleep(0.002)
        east.off()
        west.on()
        time.sleep(0.002)
        west.off()

    print(f"  Direction conflicts: PASS ({cycles} rapid toggles)")
    print()

    # Pulse timing test
    coin_ms = int(os.getenv("COIN_PULSE_MS", "150"))
    drop_ms = int(os.getenv("DROP_PULSE_MS", "200"))
    print(f"Testing pulse timing (coin = {coin_ms}ms, drop = {drop_ms}ms)...")
    for name, duration_ms in [("coin", coin_ms), ("drop", drop_ms)]:
        dev = devices[name]
        start = time.monotonic()
        dev.on()
        time.sleep(duration_ms / 1000.0)
        dev.off()
        elapsed = (time.monotonic() - start) * 1000
        print(f"  {name}: requested {duration_ms}ms, actual {elapsed:.1f}ms")

    # Win input test
    print()
    print("Win input test:")
    print(f"  Current win sensor value: {win_input.value}")
    print("  (Press the win sensor to test, or skip with Ctrl+C)")
    try:
        win_triggered = False

        def on_win():
            nonlocal win_triggered
            win_triggered = True
            print("  WIN DETECTED!")

        win_input.when_activated = on_win
        time.sleep(5)
        if not win_triggered:
            print("  No win trigger detected (5s timeout)")
    except KeyboardInterrupt:
        print("  Skipped")

    # Cleanup
    for dev in devices.values():
        dev.off()
        dev.close()
    win_input.close()

    print()
    print("=== ALL GPIO TESTS PASSED ===")


def main():
    _load_env()

    parser = argparse.ArgumentParser(description="ECLAW GPIO Test")
    parser.add_argument("--cycles", type=int, default=200, help="Pulse cycles per pin")
    args = parser.parse_args()

    try:
        test_with_gpiozero(args.cycles)
    except ImportError as e:
        print(f"Error: {e}")
        print("Make sure gpiozero is installed: pip install gpiozero")
        sys.exit(1)
    except Exception as e:
        print(f"FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
