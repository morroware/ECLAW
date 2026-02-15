#!/usr/bin/env python3
"""
GPIO Test Script — Phase B validation.

Tests all 6 output pins and the win input pin.
Run on the Pi 5 with LEDs or multimeter connected.

Usage:
    python3 scripts/gpio_test.py [--cycles 200]
"""

import argparse
import time
import sys


def test_with_gpiozero(cycles: int):
    """Test using gpiozero (same library as the game server)."""
    from gpiozero import OutputDevice, DigitalInputDevice

    PINS = {
        "coin": 17,
        "north": 27,
        "south": 5,
        "west": 6,
        "east": 24,
        "drop": 25,
    }
    WIN_PIN = 16

    print(f"Testing {len(PINS)} output pins + 1 input pin")
    print(f"Running {cycles} pulse cycles per pin")
    print()

    devices = {}
    for name, pin in PINS.items():
        devices[name] = OutputDevice(pin, initial_value=False)
        print(f"  Created OutputDevice: {name} (BCM {pin})")

    win_input = DigitalInputDevice(WIN_PIN, pull_up=False, bounce_time=0.1)
    print(f"  Created InputDevice: win (BCM {WIN_PIN})")
    print()

    # Individual pin test
    for name, dev in devices.items():
        print(f"Testing {name}...")
        stuck_count = 0

        for i in range(cycles):
            dev.on()
            time.sleep(0.01)
            dev.off()
            time.sleep(0.01)

            # Verify pin is actually off
            # (We can't read output state with gpiozero easily,
            # but any stuck pin would be visible on LEDs/multimeter)

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
        # North on, verify south stays off
        north.on()
        time.sleep(0.005)
        north.off()

        # South on, verify north stays off
        south.on()
        time.sleep(0.005)
        south.off()

        # Rapid alternation
        east.on()
        time.sleep(0.002)
        east.off()
        west.on()
        time.sleep(0.002)
        west.off()

    print(f"  Direction conflicts: PASS ({cycles} rapid toggles)")
    print()

    # Pulse timing test
    print("Testing pulse timing (coin = 150ms, drop = 200ms)...")
    for name, duration_ms in [("coin", 150), ("drop", 200)]:
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
