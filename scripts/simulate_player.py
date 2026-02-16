#!/usr/bin/env python3
"""
Player Simulation — automated play-through for testing.

Connects to the game server, joins the queue, waits for turn,
moves randomly, and drops. Useful for testing the full game loop.

Usage:
    python3 scripts/simulate_player.py [--base-url http://localhost:8000] [--count 5]
"""

import argparse
import asyncio
import json
import random
import sys
import time

import httpx
import websockets


async def simulate_player(base_url: str, player_num: int):
    """Simulate one complete player session."""
    name = f"TestPlayer_{player_num}"
    email = f"test{player_num}@example.com"

    print(f"[{name}] Joining queue...")

    # Join the queue
    async with httpx.AsyncClient(base_url=base_url) as client:
        res = await client.post("/api/queue/join", json={"name": name, "email": email})
        if res.status_code != 200:
            print(f"[{name}] Failed to join: {res.status_code} {res.text}")
            return
        data = res.json()
        token = data["token"]
        position = data["position"]
        print(f"[{name}] Joined at position {position}")

    # Connect via WebSocket
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url += "/ws/control"

    async with websockets.connect(ws_url) as ws:
        # Auth
        await ws.send(json.dumps({"type": "auth", "token": token}))
        auth_resp = json.loads(await ws.recv())
        if auth_resp.get("type") != "auth_ok":
            print(f"[{name}] Auth failed: {auth_resp}")
            return
        print(f"[{name}] Authenticated, state={auth_resp['state']}")

        # Wait for our turn
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=120)
            except asyncio.TimeoutError:
                print(f"[{name}] Timeout waiting for turn")
                return

            msg = json.loads(raw)
            print(f"[{name}] Received: {msg['type']}")

            if msg.get("type") == "ready_prompt":
                print(f"[{name}] Got ready prompt, confirming...")
                await asyncio.sleep(1)
                await ws.send(json.dumps({"type": "ready_confirm"}))

            elif msg.get("type") == "state_update" and msg.get("state") == "moving":
                print(f"[{name}] Moving! Try {msg.get('current_try')}/{msg.get('max_tries')}")

                # Simulate random movements
                directions = ["north", "south", "east", "west"]
                for _ in range(random.randint(3, 8)):
                    d = random.choice(directions)
                    await ws.send(json.dumps({"type": "keydown", "key": d}))
                    await asyncio.sleep(random.uniform(0.2, 1.5))
                    await ws.send(json.dumps({"type": "keyup", "key": d}))
                    await asyncio.sleep(0.1)

                # Drop
                print(f"[{name}] Dropping!")
                await ws.send(json.dumps({"type": "drop_start"}))

            elif msg.get("type") == "turn_end":
                result = msg.get("result", "unknown")
                print(f"[{name}] Turn ended: {result}")
                return

            elif msg.get("type") == "state_update" and msg.get("state") == "idle":
                # Turn ended without explicit turn_end message
                print(f"[{name}] Game returned to idle")
                return


async def main():
    parser = argparse.ArgumentParser(description="ECLAW Player Simulator")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=5, help="Number of simulated players")
    parser.add_argument("--parallel", action="store_true", help="Run all players simultaneously")
    args = parser.parse_args()

    print(f"Simulating {args.count} players against {args.base_url}")
    print()

    if args.parallel:
        tasks = [simulate_player(args.base_url, i + 1) for i in range(args.count)]
        await asyncio.gather(*tasks, return_exceptions=True)
    else:
        for i in range(args.count):
            await simulate_player(args.base_url, i + 1)
            print()


if __name__ == "__main__":
    asyncio.run(main())
