# Remote Claw Machine — Developer Build Specification

> **Audience**: Software engineers building this system.
> **Stack**: Python 3.11+ / FastAPI / gpiozero / SQLite / MediaMTX / nginx.
> **Hardware**: Raspberry Pi 5 (4GB+), Pi Camera Module 3, opto-isolation board.

---

## 0. Critical Technical Gotchas (Read First)

Before writing any code, every developer on this project must understand these five issues. Each one has caused real failures in similar Pi 5 projects.

### Gotcha 1 — gpiozero apt package is broken on Pi 5

The system-installed `python3-gpiozero` (via apt) hardcodes `gpiochip4` for Pi 5. Since kernel 6.6.45 (Aug 2024), the Pi 5 kernel maps user-facing GPIO to `gpiochip0`. The apt package will throw `'can not open gpiochip'`.

**Fix**: Always install gpiozero via pip inside a venv. The pip version auto-detects the correct chip.

```bash
python3 -m venv --system-site-packages /opt/claw/venv
source /opt/claw/venv/bin/activate
pip install gpiozero   # pulls the patched version
python -c "from gpiozero import OutputDevice; d = OutputDevice(17); d.on(); d.off(); d.close(); print('OK')"
```

### Gotcha 2 — gpiozero is synchronous; FastAPI is async

Every `OutputDevice.on()`, `.off()`, and `InputDevice` call blocks the calling thread. In a FastAPI async handler, this blocks the entire event loop.

**Fix**: Run all GPIO calls via `asyncio.get_event_loop().run_in_executor(None, ...)` or use a dedicated `ThreadPoolExecutor` with `max_workers=1` (serializes GPIO access, prevents race conditions).

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

_gpio_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpio")

async def gpio_on(device):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_gpio_executor, device.on)
```

### Gotcha 3 — gpiozero cleanup does NOT run on SIGKILL

gpiozero registers an `atexit` handler that resets pins to their initial state. This runs on normal exit, `SIGTERM`, and unhandled exceptions. It does **NOT** run on `SIGKILL` (kill -9) or a total system freeze. If the process is forcibly killed while a direction output is ON, that pin stays ON and the claw keeps moving.

**Implications for the watchdog**: The watchdog process cannot rely on the game server's gpiozero cleanup. It must use `lgpio` directly (the C library) to force pins OFF, operating on its own chip handle.

### Gotcha 4 — Two processes cannot share gpiozero pins

gpiozero claims exclusive ownership of GPIO pins. If the game server has `OutputDevice(17)`, the watchdog cannot also create `OutputDevice(17)`. They would conflict.

**Fix for watchdog**: The watchdog uses raw `lgpio` calls (not gpiozero) to override pin states. `lgpio.gpio_write()` can set any pin regardless of who "owns" it, because at the hardware level, the last write wins. The watchdog opens its own `lgpio` chip handle.

```python
import lgpio
h = lgpio.gpiochip_open(0)   # gpiochip0 on Pi 5
for pin in [17, 27, 5, 6, 24, 25]:
    lgpio.gpio_claim_output(h, pin, 0)   # claim and set LOW
lgpio.gpiochip_close(h)
```

**Important**: The watchdog should only claim pins and force them LOW during an emergency. During normal operation it only monitors health, it does not touch GPIO.

### Gotcha 5 — MediaMTX WHEP URL pattern

MediaMTX serves its built-in WebRTC reader page at `http://<host>:8889/<path>` and the raw WHEP endpoint at `http://<host>:8889/<path>/whep`. When building a custom frontend, you POST an SDP offer to the `/whep` endpoint:

```
POST http://localhost:8889/cam/whep
Content-Type: application/sdp

<SDP offer body>
```

The response is `201 Created` with the SDP answer in the body and a `Location` header pointing to the session resource (used for ICE trickling via PATCH and teardown via DELETE).

---

## 1. Architecture

```
┌─────────────────────────── Raspberry Pi 5 ────────────────────────────┐
│                                                                        │
│  ┌────────────────┐        ┌───────────────────────────────────────┐  │
│  │   MediaMTX      │        │   Game Server (FastAPI / uvicorn)    │  │
│  │                  │        │                                     │  │
│  │  CSI Camera      │        │  ┌───────────┐  ┌───────────────┐  │  │
│  │  → H.264 encode  │        │  │ REST API  │  │ WebSocket Hub │  │  │
│  │  → WebRTC (WHEP) │        │  └───────────┘  └───────────────┘  │  │
│  │  → RTSP output   │        │  ┌───────────┐  ┌───────────────┐  │  │
│  │                  │        │  │ Queue Mgr │  │ State Machine │  │  │
│  │  :8889 (HTTP)    │        │  └───────────┘  └───────────────┘  │  │
│  │  :8554 (RTSP)    │        │  ┌───────────┐  ┌───────────────┐  │  │
│  └────────────────┘        │  │ GPIO Ctrl │  │ SQLite (aio)  │  │  │
│                              │  └───────────┘  └───────────────┘  │  │
│                              │                                     │  │
│                              │  :8000 (HTTP + WS, localhost only)  │  │
│                              └───────────────────────────────────────┘  │
│                                                                        │
│  ┌────────────────┐        ┌──────────────────────┐                   │
│  │ Watchdog Svc    │        │  nginx               │                   │
│  │ (lgpio direct)  │        │  :443 (HTTPS public) │                   │
│  │ monitors health │        │  proxies to:         │                   │
│  │ kills GPIO on   │        │    /          static  │                   │
│  │ server freeze   │        │    /api/*    :8000    │                   │
│  └────────────────┘        │    /ws/*     :8000    │                   │
│                              │    /stream/* :8889    │                   │
│  ┌────────────────┐        └──────────────────────┘                   │
│  │ GPIO Header     │                                                   │
│  │  ↕ opto-isolate │                                                   │
│  │  ↕ arcade machine│                                                   │
│  └────────────────┘                                                   │
└────────────────────────────────────────────────────────────────────────┘
```

### Process Inventory (4 processes)

| Process | Runs as | Manages | Restarts |
|---------|---------|---------|----------|
| `mediamtx` | `mediamtx` user | Camera capture + stream serving | systemd, always |
| `uvicorn` (game server) | `claw` user (+gpio group) | Queue, state machine, GPIO, API | systemd, always |
| `claw-watchdog` | `claw` user (+gpio group) | Health check, emergency GPIO kill | systemd, bound to game server |
| `nginx` | `www-data` | TLS, reverse proxy, rate limit, static | systemd, always |

---

## 2. Dependencies & Environment Setup

### 2.1 System Packages

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y \
  python3 python3-venv python3-pip \
  python3-lgpio \
  nginx certbot python3-certbot-nginx \
  sqlite3
```

### 2.2 Python Virtual Environment

```bash
sudo mkdir -p /opt/claw
sudo chown claw:claw /opt/claw
python3 -m venv --system-site-packages /opt/claw/venv
source /opt/claw/venv/bin/activate
```

### 2.3 requirements.txt (pinned)

```
fastapi==0.115.*
uvicorn[standard]==0.34.*
websockets>=13.0,<14.0
aiosqlite==0.20.*
pydantic==2.10.*
pydantic-settings==2.7.*
httpx==0.28.*
gpiozero==2.0.*
python-multipart==0.0.19
```

Install: `pip install -r requirements.txt`

### 2.4 MediaMTX Installation

```bash
# Check latest at https://github.com/bluenviron/mediamtx/releases
MEDIAMTX_VERSION="1.12.3"
wget "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_arm64.tar.gz"
tar xzf mediamtx_*.tar.gz
sudo mv mediamtx /usr/local/bin/
sudo mv mediamtx.yml /etc/mediamtx.yml
```

---

## 3. Configuration System

All tunable parameters live in a `.env` file loaded by Pydantic Settings. The game server, watchdog, and deploy scripts all read from the same file.

### 3.1 .env File

```ini
# === Timing ===
TRIES_PER_PLAYER=2
TURN_TIME_SECONDS=90
TRY_MOVE_SECONDS=30
POST_DROP_WAIT_SECONDS=8
READY_PROMPT_SECONDS=15
QUEUE_GRACE_PERIOD_SECONDS=300

# === GPIO Pulse/Hold ===
COIN_PULSE_MS=150
DROP_PULSE_MS=200
MIN_INTER_PULSE_MS=500
DIRECTION_HOLD_MAX_MS=30000
COIN_EACH_TRY=true

# === Control ===
COMMAND_RATE_LIMIT_HZ=25
DIRECTION_CONFLICT_MODE=ignore_new

# === Pins (BCM numbering) ===
PIN_COIN=17
PIN_NORTH=27
PIN_SOUTH=5
PIN_WEST=6
PIN_EAST=24
PIN_DROP=25
PIN_WIN=16

# === Server ===
HOST=127.0.0.1
PORT=8000
DATABASE_PATH=/opt/claw/data/claw.db
ADMIN_API_KEY=changeme_in_production

# === Watchdog ===
WATCHDOG_HEALTH_URL=http://127.0.0.1:8000/api/health
WATCHDOG_CHECK_INTERVAL_S=2
WATCHDOG_FAIL_THRESHOLD=3

# === Stream ===
MEDIAMTX_HEALTH_URL=http://127.0.0.1:8889/v3/paths/list
```

### 3.2 Pydantic Settings Class

```python
# app/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Timing
    tries_per_player: int = 2
    turn_time_seconds: int = 90
    try_move_seconds: int = 30
    post_drop_wait_seconds: int = 8
    ready_prompt_seconds: int = 15
    queue_grace_period_seconds: int = 300

    # GPIO pulse/hold
    coin_pulse_ms: int = 150
    drop_pulse_ms: int = 200
    min_inter_pulse_ms: int = 500
    direction_hold_max_ms: int = 30000
    coin_each_try: bool = True

    # Control
    command_rate_limit_hz: int = 25
    direction_conflict_mode: str = "ignore_new"  # or "replace"

    # Pins
    pin_coin: int = 17
    pin_north: int = 27
    pin_south: int = 5
    pin_west: int = 6
    pin_east: int = 24
    pin_drop: int = 25
    pin_win: int = 16

    # Server
    host: str = "127.0.0.1"
    port: int = 8000
    database_path: str = "/opt/claw/data/claw.db"
    admin_api_key: str = "changeme"

    # Watchdog
    watchdog_health_url: str = "http://127.0.0.1:8000/api/health"
    watchdog_check_interval_s: int = 2
    watchdog_fail_threshold: int = 3

    # Stream
    mediamtx_health_url: str = "http://127.0.0.1:8889/v3/paths/list"

    model_config = {"env_file": "/opt/claw/.env", "env_file_encoding": "utf-8"}

settings = Settings()
```

---

## 4. GPIO Controller

### 4.1 Design Principles

- All GPIO operations are synchronous (gpiozero limitation) and execute in a single-threaded executor.
- The controller is the **only** module that imports gpiozero. Everything else calls controller methods.
- Every public method is async and internally dispatches to the executor.
- All outputs initialize to OFF. On `cleanup()`, all outputs are set OFF and devices closed.
- The controller enforces: hold timeouts, pulse cooldowns, direction conflicts, and rate limits.

### 4.2 Implementation Skeleton

```python
# app/gpio/controller.py
import asyncio
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from gpiozero import OutputDevice, DigitalInputDevice
from app.config import settings

logger = logging.getLogger("gpio")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gpio")

OPPOSING = {
    "north": "south", "south": "north",
    "east": "west", "west": "east",
}

class GPIOController:
    def __init__(self):
        self._outputs: dict[str, OutputDevice] = {}
        self._active_holds: dict[str, asyncio.Task] = {}
        self._last_pulse: dict[str, float] = {}
        self._locked = False
        self._initialized = False

    # ── Lifecycle ──────────────────────────────────────────────

    async def initialize(self):
        """Call once at server startup."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, self._init_devices)
        self._initialized = True
        logger.info("GPIO controller initialized")

    def _init_devices(self):
        """Runs in executor thread. Creates all gpiozero devices."""
        pin_map = {
            "coin":  settings.pin_coin,
            "north": settings.pin_north,
            "south": settings.pin_south,
            "west":  settings.pin_west,
            "east":  settings.pin_east,
            "drop":  settings.pin_drop,
        }
        for name, pin in pin_map.items():
            self._outputs[name] = OutputDevice(pin, initial_value=False)
            self._last_pulse[name] = 0.0

        self._win_input = DigitalInputDevice(settings.pin_win, pull_up=False)

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
        if hasattr(self, '_win_input'):
            self._win_input.close()

    # ── Emergency Stop ─────────────────────────────────────────

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

    # ── Direction Hold ─────────────────────────────────────────

    async def direction_on(self, direction: str) -> bool:
        """Start holding a direction. Returns False if rejected."""
        if self._locked or direction not in OPPOSING:
            return False

        # Conflict check
        opposite = OPPOSING[direction]
        if opposite in self._active_holds:
            if settings.direction_conflict_mode == "ignore_new":
                return False
            else:  # replace
                await self.direction_off(opposite)

        # Already held
        if direction in self._active_holds:
            return True

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, self._outputs[direction].on)
        logger.debug(f"Direction ON: {direction}")

        # Safety timeout task
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
            pass  # Normal cancellation on release

    async def all_directions_off(self):
        """Release all directions. Call on turn transitions."""
        for d in list(self._active_holds.keys()):
            await self.direction_off(d)

    # ── Pulse Outputs ──────────────────────────────────────────

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

    # ── Win Input ──────────────────────────────────────────────

    def register_win_callback(self, callback):
        """Register an async-safe callback for win detection.
        The callback will be called from a background thread
        (gpiozero's event thread), so it must schedule work
        onto the event loop.
        """
        self._win_input.when_activated = callback

    def unregister_win_callback(self):
        self._win_input.when_activated = None

    # ── Status ─────────────────────────────────────────────────

    @property
    def active_directions(self) -> list[str]:
        return list(self._active_holds.keys())

    @property
    def is_locked(self) -> bool:
        return self._locked
```

### 4.3 Win Input Threading Issue

gpiozero fires `when_activated` callbacks in its own background thread. To bridge into the async state machine:

```python
# In the state machine, when entering POST_DROP:
import asyncio

def _on_win_trigger():
    """Called from gpiozero thread. Schedules async handler on the event loop."""
    loop = asyncio.get_event_loop()
    loop.call_soon_threadsafe(asyncio.create_task, state_machine.handle_win())

gpio_controller.register_win_callback(_on_win_trigger)
```

### 4.4 Software Debounce for Win Input

gpiozero's `DigitalInputDevice` has a `bounce_time` parameter. Set it at initialization:

```python
self._win_input = DigitalInputDevice(settings.pin_win, pull_up=False, bounce_time=0.1)
```

This ignores state changes within 100ms of the last trigger.

---

## 5. Database Schema & Access

### 5.1 Schema (SQLite)

```sql
-- migrations/001_initial.sql

CREATE TABLE IF NOT EXISTS queue_entries (
    id TEXT PRIMARY KEY,                          -- uuid4
    token_hash TEXT UNIQUE NOT NULL,              -- sha256 of bearer token
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    ip_address TEXT,
    state TEXT NOT NULL DEFAULT 'waiting',
    -- states: waiting, ready, active, done, cancelled, skipped, expired
    position INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    activated_at TEXT,
    completed_at TEXT,
    result TEXT,                                   -- win, loss, expired, skipped
    tries_used INTEGER NOT NULL DEFAULT 0,
    try_move_end_at TEXT,                          -- ISO timestamp: when current move phase ends
    turn_end_at TEXT                               -- ISO timestamp: hard turn deadline
);

CREATE TABLE IF NOT EXISTS game_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_entry_id TEXT REFERENCES queue_entries(id),
    event_type TEXT NOT NULL,
    -- types: join, leave, activate, ready_prompt, move_start, direction,
    --        drop, win, try_end, turn_end, disconnect, reconnect,
    --        emergency_stop, admin_action, error
    detail TEXT,                                    -- JSON string
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);

CREATE INDEX IF NOT EXISTS idx_queue_state ON queue_entries(state);
CREATE INDEX IF NOT EXISTS idx_queue_position ON queue_entries(position)
    WHERE state IN ('waiting', 'ready', 'active');
CREATE INDEX IF NOT EXISTS idx_events_entry ON game_events(queue_entry_id);
CREATE INDEX IF NOT EXISTS idx_events_time ON game_events(created_at);
```

### 5.2 Database Module

```python
# app/database.py
import aiosqlite
import hashlib
import os
from app.config import settings

_db: aiosqlite.Connection | None = None

async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(settings.database_path), exist_ok=True)
        _db = await aiosqlite.connect(settings.database_path)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await _run_migrations(_db)
    return _db

async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None

async def _run_migrations(db: aiosqlite.Connection):
    """Run any pending migration files."""
    # Read current version
    try:
        async with db.execute("SELECT MAX(version) FROM schema_version") as cur:
            row = await cur.fetchone()
            current = row[0] if row and row[0] else 0
    except aiosqlite.OperationalError:
        current = 0

    # Apply migrations in order
    migrations_dir = os.path.join(os.path.dirname(__file__), "..", "migrations")
    if os.path.isdir(migrations_dir):
        for fname in sorted(os.listdir(migrations_dir)):
            if not fname.endswith(".sql"):
                continue
            version = int(fname.split("_")[0])
            if version > current:
                with open(os.path.join(migrations_dir, fname)) as f:
                    await db.executescript(f.read())
                await db.execute(
                    "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                    (version,),
                )
                await db.commit()

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
```

### 5.3 Token Security

Tokens are generated with `secrets.token_urlsafe(32)` and returned to the client exactly once (in the join response). The database stores only the SHA-256 hash. Verification flow:

```python
# On any authenticated request:
raw_token = request.headers["Authorization"].removeprefix("Bearer ").strip()
token_hash = hash_token(raw_token)
entry = await db.execute("SELECT * FROM queue_entries WHERE token_hash = ?", (token_hash,))
```

---

## 6. State Machine

This is the core game logic. It runs as an async singleton managing the current game state.

### 6.1 State Diagram

```
                          ┌─────────────────────────────────────┐
                          │            IDLE                       │
                          │  (no active player, queue may exist) │
                          └──────────────┬──────────────────────┘
                                         │ queue has waiting entries
                                         ▼
                          ┌──────────────────────────────────────┐
                          │          READY_PROMPT                 │
                          │  Next player notified via WebSocket  │
                          │  Timer: READY_PROMPT_SECONDS          │
                          └───────┬──────────────┬───────────────┘
                           confirmed           timeout/disconnect
                                  │                     │
                                  ▼                     ▼
                          ┌──────────────┐      mark SKIPPED
                          │   COIN       │      advance to next
                          │ (if enabled) │
                          └──────┬───────┘
                                 │
                                 ▼
                          ┌──────────────────────────────────────┐
                          │           MOVING                      │
                          │  Directions enabled                   │
                          │  Timer: TRY_MOVE_SECONDS              │
                          │  Player can press Drop at any time    │
                          └───────┬──────────────┬───────────────┘
                          player drops      timer expires
                                  │                │
                                  ▼                ▼
                          ┌──────────────────────────────────────┐
                          │         DROPPING                      │
                          │  All directions forced OFF             │
                          │  Drop pulse fires                     │
                          └──────────────┬───────────────────────┘
                                         │
                                         ▼
                          ┌──────────────────────────────────────┐
                          │         POST_DROP                     │
                          │  Win sensor monitored                 │
                          │  Timer: POST_DROP_WAIT_SECONDS        │
                          └───────┬──────────────┬───────────────┘
                           win detected       timeout (no win)
                                  │                     │
                                  ▼                     ▼
                          ┌──────────────┐   ┌──────────────────┐
                          │   WIN        │   │   TRY_END        │
                          │  record win  │   │  tries_used += 1 │
                          └──────┬───────┘   └───────┬──────────┘
                                 │                    │
                                 │           tries_used < max?
                                 │            yes │        no │
                                 │                ▼           │
                                 │         back to COIN/      │
                                 │         MOVING              │
                                 ▼                             ▼
                          ┌──────────────────────────────────────┐
                          │          TURN_END                     │
                          │  All outputs OFF                      │
                          │  Persist result (win/loss/expired)   │
                          │  Advance queue                       │
                          └──────────────┬───────────────────────┘
                                         │
                                         ▼
                                     back to IDLE or READY_PROMPT
```

### 6.2 Hard Turn Timeout

In addition to per-try timers, there is a `TURN_TIME_SECONDS` hard cap. If a player's total turn time exceeds this, their turn ends immediately regardless of which try/state they're in. This prevents edge cases where many short tries accumulate beyond a reasonable total.

### 6.3 State Machine Implementation Pattern

```python
# app/game/state_machine.py
import asyncio
import logging
from enum import Enum
from datetime import datetime, timezone

logger = logging.getLogger("state_machine")

class TurnState(str, Enum):
    IDLE = "idle"
    READY_PROMPT = "ready_prompt"
    MOVING = "moving"
    DROPPING = "dropping"
    POST_DROP = "post_drop"
    TURN_END = "turn_end"

class StateMachine:
    def __init__(self, gpio_controller, queue_manager, ws_hub, settings):
        self.gpio = gpio_controller
        self.queue = queue_manager
        self.ws = ws_hub
        self.settings = settings

        self.state = TurnState.IDLE
        self.active_entry_id: str | None = None
        self.current_try: int = 0
        self._state_timer: asyncio.Task | None = None
        self._turn_timer: asyncio.Task | None = None

    async def advance_queue(self):
        """Called when queue changes or a turn ends. Starts next player if any."""
        if self.state != TurnState.IDLE:
            return

        next_entry = await self.queue.peek_next_waiting()
        if next_entry is None:
            return

        self.active_entry_id = next_entry["id"]
        await self.queue.set_state(next_entry["id"], "ready")
        await self._enter_state(TurnState.READY_PROMPT)

    async def handle_ready_confirm(self, entry_id: str):
        """Called when the prompted player confirms they are ready."""
        if self.state != TurnState.READY_PROMPT:
            return
        if entry_id != self.active_entry_id:
            return

        await self.queue.set_state(entry_id, "active")
        self.current_try = 0
        # Start hard turn timer
        self._turn_timer = asyncio.create_task(
            self._hard_turn_timeout(self.settings.turn_time_seconds)
        )
        await self._start_try()

    async def handle_drop(self, entry_id: str):
        """Called when active player presses drop."""
        if self.state != TurnState.MOVING or entry_id != self.active_entry_id:
            return
        await self._enter_state(TurnState.DROPPING)

    async def handle_win(self):
        """Called from win sensor callback (thread-safe bridged)."""
        if self.state != TurnState.POST_DROP:
            logger.warning(f"Win trigger ignored: state is {self.state}")
            return
        logger.info("WIN DETECTED")
        await self._end_turn("win")

    async def handle_disconnect(self, entry_id: str):
        """Called when active player's WebSocket disconnects."""
        if entry_id != self.active_entry_id:
            return
        await self.gpio.all_directions_off()
        # Grace period handled by caller (ws handler)
        # If not reconnected in time, caller should invoke handle_disconnect_timeout()

    async def handle_disconnect_timeout(self, entry_id: str):
        """Called after grace period expires without reconnection."""
        if entry_id != self.active_entry_id:
            return
        await self._end_turn("expired")

    # ── Internal State Transitions ─────────────────────────────

    async def _enter_state(self, new_state: TurnState):
        """Transition to a new state. Cancels any existing state timer."""
        if self._state_timer and not self._state_timer.done():
            self._state_timer.cancel()

        old_state = self.state
        self.state = new_state
        logger.info(f"State: {old_state} → {new_state}")

        await self.ws.broadcast_state(new_state, self._build_state_payload())

        if new_state == TurnState.READY_PROMPT:
            self._state_timer = asyncio.create_task(
                self._ready_timeout(self.settings.ready_prompt_seconds)
            )
            await self.ws.notify_player_ready(self.active_entry_id)

        elif new_state == TurnState.MOVING:
            self._state_timer = asyncio.create_task(
                self._move_timeout(self.settings.try_move_seconds)
            )

        elif new_state == TurnState.DROPPING:
            await self.gpio.all_directions_off()
            await self.gpio.pulse("drop")
            await self._enter_state(TurnState.POST_DROP)

        elif new_state == TurnState.POST_DROP:
            self.gpio.register_win_callback(self._win_bridge)
            self._state_timer = asyncio.create_task(
                self._post_drop_timeout(self.settings.post_drop_wait_seconds)
            )

        elif new_state == TurnState.TURN_END:
            pass  # Handled by _end_turn

    async def _start_try(self):
        """Begin a new try. Optionally pulse coin, then enter MOVING."""
        self.current_try += 1
        if self.settings.coin_each_try:
            await self.gpio.pulse("coin")
            await asyncio.sleep(0.5)  # Let machine register credit
        await self._enter_state(TurnState.MOVING)

    async def _end_turn(self, result: str):
        """Clean up and finalize the turn."""
        self.gpio.unregister_win_callback()
        await self.gpio.emergency_stop()
        await self.gpio.unlock()

        if self._turn_timer and not self._turn_timer.done():
            self._turn_timer.cancel()
        if self._state_timer and not self._state_timer.done():
            self._state_timer.cancel()

        await self.queue.complete_entry(
            self.active_entry_id, result, self.current_try
        )
        await self.ws.broadcast_turn_end(self.active_entry_id, result)

        self.state = TurnState.IDLE
        self.active_entry_id = None
        self.current_try = 0

        # Immediately try to start the next player
        await self.advance_queue()

    # ── Timers ─────────────────────────────────────────────────

    async def _ready_timeout(self, seconds: int):
        await asyncio.sleep(seconds)
        if self.state == TurnState.READY_PROMPT:
            logger.info("Ready prompt timed out, skipping player")
            await self.queue.set_state(self.active_entry_id, "skipped")
            self.state = TurnState.IDLE
            self.active_entry_id = None
            await self.advance_queue()

    async def _move_timeout(self, seconds: int):
        await asyncio.sleep(seconds)
        if self.state == TurnState.MOVING:
            logger.info("Move timer expired, auto-dropping")
            await self._enter_state(TurnState.DROPPING)

    async def _post_drop_timeout(self, seconds: int):
        await asyncio.sleep(seconds)
        if self.state == TurnState.POST_DROP:
            self.gpio.unregister_win_callback()
            logger.info("Post-drop timeout, no win")
            if self.current_try < self.settings.tries_per_player:
                await self._start_try()
            else:
                await self._end_turn("loss")

    async def _hard_turn_timeout(self, seconds: int):
        await asyncio.sleep(seconds)
        if self.state not in (TurnState.IDLE, TurnState.TURN_END):
            logger.warning("Hard turn timeout reached")
            await self._end_turn("expired")

    # ── Helpers ─────────────────────────────────────────────────

    def _win_bridge(self):
        """Called from gpiozero thread. Bridges into async."""
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(asyncio.create_task, self.handle_win())
        except RuntimeError:
            logger.error("Failed to bridge win callback to event loop")

    def _build_state_payload(self) -> dict:
        return {
            "state": self.state.value,
            "active_entry_id": self.active_entry_id,
            "current_try": self.current_try,
            "max_tries": self.settings.tries_per_player,
        }
```

---

## 7. Queue Manager

```python
# app/game/queue_manager.py
import uuid
import secrets
from datetime import datetime, timezone
from app.database import get_db, hash_token

class QueueManager:
    async def join(self, name: str, email: str, ip: str) -> dict:
        """Add a user to the queue. Returns {id, token, position}."""
        db = await get_db()
        entry_id = str(uuid.uuid4())
        raw_token = secrets.token_urlsafe(32)
        token_h = hash_token(raw_token)

        # Determine next position
        async with db.execute(
            "SELECT MAX(position) FROM queue_entries WHERE state = 'waiting'"
        ) as cur:
            row = await cur.fetchone()
            next_pos = (row[0] or 0) + 1

        await db.execute(
            """INSERT INTO queue_entries (id, token_hash, name, email, ip_address, state, position)
               VALUES (?, ?, ?, ?, ?, 'waiting', ?)""",
            (entry_id, token_h, name, email, ip, next_pos),
        )
        await db.commit()
        return {"id": entry_id, "token": raw_token, "position": next_pos}

    async def leave(self, token_hash: str) -> bool:
        db = await get_db()
        await db.execute(
            "UPDATE queue_entries SET state = 'cancelled', completed_at = datetime('now') "
            "WHERE token_hash = ? AND state IN ('waiting', 'ready')",
            (token_hash,),
        )
        await db.commit()
        return True

    async def peek_next_waiting(self) -> dict | None:
        db = await get_db()
        async with db.execute(
            "SELECT * FROM queue_entries WHERE state = 'waiting' ORDER BY position ASC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def set_state(self, entry_id: str, state: str):
        db = await get_db()
        updates = {"state": state}
        if state == "active":
            updates["activated_at"] = datetime.now(timezone.utc).isoformat()
        await db.execute(
            f"UPDATE queue_entries SET state = ?, activated_at = COALESCE(?, activated_at) WHERE id = ?",
            (state, updates.get("activated_at"), entry_id),
        )
        await db.commit()

    async def complete_entry(self, entry_id: str, result: str, tries_used: int):
        db = await get_db()
        await db.execute(
            "UPDATE queue_entries SET state = 'done', result = ?, tries_used = ?, "
            "completed_at = datetime('now') WHERE id = ?",
            (result, tries_used, entry_id),
        )
        await db.commit()

    async def get_by_token(self, token_hash: str) -> dict | None:
        db = await get_db()
        async with db.execute(
            "SELECT * FROM queue_entries WHERE token_hash = ?", (token_hash,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_queue_status(self) -> dict:
        db = await get_db()
        async with db.execute(
            "SELECT COUNT(*) FROM queue_entries WHERE state = 'waiting'"
        ) as cur:
            waiting = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT name, state FROM queue_entries WHERE state IN ('active', 'ready') LIMIT 1"
        ) as cur:
            active_row = await cur.fetchone()
        return {
            "queue_length": waiting,
            "current_player": dict(active_row)["name"] if active_row else None,
            "current_player_state": dict(active_row)["state"] if active_row else None,
        }

    async def cleanup_stale(self, grace_seconds: int):
        """Called on startup. Expire entries that disconnected too long ago."""
        db = await get_db()
        await db.execute(
            "UPDATE queue_entries SET state = 'expired' "
            "WHERE state = 'active' AND activated_at IS NOT NULL "
            "AND (julianday('now') - julianday(activated_at)) * 86400 > ?",
            (grace_seconds,),
        )
        await db.commit()
```

---

## 8. WebSocket Hub

### 8.1 Architecture

Two WebSocket endpoints:

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `/ws/status` | None | Broadcast-only: queue updates, state changes, activity |
| `/ws/control` | Token required | Bidirectional: auth, control input, server events |

### 8.2 Status Hub (Broadcast)

```python
# app/ws/status_hub.py
import asyncio
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

    def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)

    async def broadcast(self, message: dict):
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
        await self.broadcast({"type": "turn_end", "result": result})

    async def broadcast_queue_update(self, status: dict):
        await self.broadcast({"type": "queue_update", **status})

    async def notify_player_ready(self, entry_id: str):
        """Notify via the control channel that a specific player should confirm ready."""
        # This is actually sent through the control handler, not the broadcast hub.
        # The control handler manages per-player connections.
        pass

    @property
    def viewer_count(self) -> int:
        return len(self._clients)
```

### 8.3 Control Handler

```python
# app/ws/control_handler.py
import asyncio
import json
import time
import logging
from fastapi import WebSocket
from app.database import hash_token

logger = logging.getLogger("ws.control")

VALID_DIRECTIONS = {"north", "south", "east", "west"}

class ControlHandler:
    def __init__(self, state_machine, queue_manager, gpio_controller, settings):
        self.sm = state_machine
        self.queue = queue_manager
        self.gpio = gpio_controller
        self.settings = settings
        self._player_ws: dict[str, WebSocket] = {}  # entry_id -> ws
        self._last_command_time: dict[str, float] = {}  # entry_id -> monotonic

    async def handle_connection(self, ws: WebSocket):
        await ws.accept()
        entry_id = None
        try:
            # First message must be auth
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") != "auth" or "token" not in msg:
                await ws.send_text(json.dumps({"type": "error", "message": "Auth required"}))
                await ws.close(1008)
                return

            token_hash = hash_token(msg["token"])
            entry = await self.queue.get_by_token(token_hash)
            if not entry:
                await ws.send_text(json.dumps({"type": "error", "message": "Invalid token"}))
                await ws.close(1008)
                return

            entry_id = entry["id"]

            # Handle duplicate tabs: close previous connection
            if entry_id in self._player_ws:
                try:
                    await self._player_ws[entry_id].close(1000, "Replaced by new connection")
                except Exception:
                    pass
            self._player_ws[entry_id] = ws

            await ws.send_text(json.dumps({
                "type": "auth_ok",
                "state": entry["state"],
                "position": entry["position"],
            }))

            # Main message loop
            async for raw_msg in ws.iter_text():
                await self._handle_message(entry_id, raw_msg, ws)

        except asyncio.TimeoutError:
            await ws.close(1008)
        except Exception as e:
            logger.error(f"Control WS error for {entry_id}: {e}")
        finally:
            if entry_id:
                self._player_ws.pop(entry_id, None)
                if entry_id == self.sm.active_entry_id:
                    await self.sm.handle_disconnect(entry_id)
                    # Start grace period
                    asyncio.create_task(
                        self._disconnect_grace(entry_id, self.settings.queue_grace_period_seconds)
                    )

    async def _handle_message(self, entry_id: str, raw: str, ws: WebSocket):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")

        # Rate limit
        now = time.monotonic()
        last = self._last_command_time.get(entry_id, 0)
        min_interval = 1.0 / self.settings.command_rate_limit_hz
        if now - last < min_interval:
            return  # Silently drop
        self._last_command_time[entry_id] = now

        # Only active player can send controls
        if entry_id != self.sm.active_entry_id:
            if msg_type == "ready_confirm":
                await self.sm.handle_ready_confirm(entry_id)
            elif msg_type == "latency_pong":
                pass  # Used for latency measurement
            return

        # Control messages (active player only)
        if msg_type == "keydown" and msg.get("key") in VALID_DIRECTIONS:
            if self.sm.state.value == "moving":
                ok = await self.gpio.direction_on(msg["key"])
                await ws.send_text(json.dumps({
                    "type": "control_ack", "key": msg["key"], "active": ok
                }))

        elif msg_type == "keyup" and msg.get("key") in VALID_DIRECTIONS:
            await self.gpio.direction_off(msg["key"])

        elif msg_type == "drop":
            await self.sm.handle_drop(entry_id)

    async def _disconnect_grace(self, entry_id: str, grace_seconds: int):
        """Wait for reconnection. If not reconnected, end turn."""
        await asyncio.sleep(min(grace_seconds, 10))  # Active player gets short grace
        if entry_id not in self._player_ws and entry_id == self.sm.active_entry_id:
            logger.info(f"Grace period expired for {entry_id}")
            await self.sm.handle_disconnect_timeout(entry_id)

    async def send_to_player(self, entry_id: str, message: dict):
        ws = self._player_ws.get(entry_id)
        if ws:
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                pass

    async def send_latency_ping(self, entry_id: str):
        await self.send_to_player(entry_id, {
            "type": "latency_ping", "server_time": time.time()
        })
```

---

## 9. REST API

```python
# app/api/routes.py
import re
from fastapi import APIRouter, Request, HTTPException, Depends, Header
from pydantic import BaseModel, EmailStr, Field
from app.config import settings
from app.database import hash_token

router = APIRouter(prefix="/api")

# ── Models ─────────────────────────────────────────────────────

class JoinRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=50, pattern=r"^[\w\s\-'.]+$")
    email: EmailStr

class JoinResponse(BaseModel):
    token: str
    position: int
    estimated_wait_seconds: int

class QueueStatusResponse(BaseModel):
    current_player: str | None
    current_player_state: str | None
    queue_length: int

class SessionResponse(BaseModel):
    state: str
    position: int | None
    tries_left: int | None
    current_try: int | None

class HealthResponse(BaseModel):
    status: str
    gpio_locked: bool
    camera_ok: bool
    queue_length: int
    viewer_count: int
    uptime_seconds: float

# ── Dependencies ───────────────────────────────────────────────

async def get_entry_from_token(authorization: str = Header(...)):
    raw = authorization.removeprefix("Bearer ").strip()
    if not raw:
        raise HTTPException(401, "Missing token")
    entry = await request.app.state.queue_manager.get_by_token(hash_token(raw))
    if not entry:
        raise HTTPException(401, "Invalid token")
    return entry

# ── Rate Limiting (in-memory, simple) ──────────────────────────

from collections import defaultdict
import time

_join_limits: dict[str, list[float]] = defaultdict(list)

def check_rate_limit(key: str, max_per_hour: int):
    now = time.time()
    _join_limits[key] = [t for t in _join_limits[key] if now - t < 3600]
    if len(_join_limits[key]) >= max_per_hour:
        raise HTTPException(429, "Rate limit exceeded. Try again later.")
    _join_limits[key].append(now)

# ── Endpoints ──────────────────────────────────────────────────

@router.post("/queue/join", response_model=JoinResponse)
async def queue_join(body: JoinRequest, request: Request):
    ip = request.client.host
    check_rate_limit(f"ip:{ip}", 5)
    check_rate_limit(f"email:{body.email}", 3)

    qm = request.app.state.queue_manager
    result = await qm.join(body.name.strip(), body.email.strip(), ip)

    # Kick off queue advancement
    await request.app.state.state_machine.advance_queue()

    est_wait = result["position"] * settings.turn_time_seconds
    return JoinResponse(
        token=result["token"],
        position=result["position"],
        estimated_wait_seconds=est_wait,
    )

@router.delete("/queue/leave")
async def queue_leave(authorization: str = Header(...), request: Request = None):
    raw = authorization.removeprefix("Bearer ").strip()
    await request.app.state.queue_manager.leave(hash_token(raw))
    await request.app.state.ws_hub.broadcast_queue_update(
        await request.app.state.queue_manager.get_queue_status()
    )
    return {"ok": True}

@router.get("/queue/status", response_model=QueueStatusResponse)
async def queue_status(request: Request):
    status = await request.app.state.queue_manager.get_queue_status()
    return QueueStatusResponse(**status)

@router.get("/session/me", response_model=SessionResponse)
async def session_me(authorization: str = Header(...), request: Request = None):
    raw = authorization.removeprefix("Bearer ").strip()
    entry = await request.app.state.queue_manager.get_by_token(hash_token(raw))
    if not entry:
        raise HTTPException(401, "Invalid token")
    sm = request.app.state.state_machine
    return SessionResponse(
        state=entry["state"],
        position=entry.get("position"),
        tries_left=(settings.tries_per_player - (sm.current_try if entry["id"] == sm.active_entry_id else entry.get("tries_used", 0))),
        current_try=sm.current_try if entry["id"] == sm.active_entry_id else None,
    )

@router.get("/health", response_model=HealthResponse)
async def health(request: Request):
    import httpx
    camera_ok = False
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(settings.mediamtx_health_url, timeout=2)
            camera_ok = r.status_code == 200
    except Exception:
        pass

    sm = request.app.state.state_machine
    qm = request.app.state.queue_manager
    gpio = request.app.state.gpio_controller

    return HealthResponse(
        status="ok",
        gpio_locked=gpio.is_locked,
        camera_ok=camera_ok,
        queue_length=(await qm.get_queue_status())["queue_length"],
        viewer_count=request.app.state.ws_hub.viewer_count,
        uptime_seconds=time.time() - request.app.state.start_time,
    )
```

### Admin Endpoints

```python
# app/api/admin.py
from fastapi import APIRouter, Header, HTTPException, Request
from app.config import settings

admin_router = APIRouter(prefix="/admin")

def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(403, "Forbidden")

@admin_router.post("/advance", dependencies=[Depends(require_admin)])
async def admin_advance(request: Request):
    sm = request.app.state.state_machine
    if sm.active_entry_id:
        await sm._end_turn("admin_skipped")
    return {"ok": True}

@admin_router.post("/emergency-stop", dependencies=[Depends(require_admin)])
async def admin_estop(request: Request):
    await request.app.state.gpio_controller.emergency_stop()
    return {"ok": True, "warning": "Controls locked. POST /admin/unlock to re-enable."}

@admin_router.post("/unlock", dependencies=[Depends(require_admin)])
async def admin_unlock(request: Request):
    await request.app.state.gpio_controller.unlock()
    return {"ok": True}

@admin_router.post("/pause", dependencies=[Depends(require_admin)])
async def admin_pause(request: Request):
    request.app.state.maintenance_mode = True
    return {"ok": True}

@admin_router.post("/resume", dependencies=[Depends(require_admin)])
async def admin_resume(request: Request):
    request.app.state.maintenance_mode = False
    await request.app.state.state_machine.advance_queue()
    return {"ok": True}
```

---

## 10. Application Entrypoint

```python
# app/main.py
import time
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import get_db, close_db
from app.gpio.controller import GPIOController
from app.game.queue_manager import QueueManager
from app.game.state_machine import StateMachine
from app.ws.status_hub import StatusHub
from app.ws.control_handler import ControlHandler
from app.api.routes import router as api_router
from app.api.admin import admin_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    logger.info("Starting claw machine server")
    app.state.start_time = time.time()
    app.state.maintenance_mode = False

    # Init database
    await get_db()

    # Init GPIO
    gpio = GPIOController()
    await gpio.initialize()
    app.state.gpio_controller = gpio

    # Init managers
    qm = QueueManager()
    await qm.cleanup_stale(settings.turn_time_seconds * 2)
    app.state.queue_manager = qm

    ws_hub = StatusHub()
    app.state.ws_hub = ws_hub

    sm = StateMachine(gpio, qm, ws_hub, settings)
    app.state.state_machine = sm

    ctrl = ControlHandler(sm, qm, gpio, settings)
    app.state.control_handler = ctrl

    # Resume queue if entries exist
    await sm.advance_queue()

    logger.info("Server ready")
    yield

    # Shutdown
    logger.info("Shutting down")
    await gpio.cleanup()
    await close_db()
    logger.info("Shutdown complete")

app = FastAPI(lifespan=lifespan, docs_url="/api/docs", redoc_url=None)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to your domain in production
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Key"],
)

# REST routes
app.include_router(api_router)
app.include_router(admin_router)

# WebSocket routes
from fastapi import WebSocket

@app.websocket("/ws/status")
async def ws_status(ws: WebSocket):
    hub = app.state.ws_hub
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()  # Keep alive; ignore messages
    except Exception:
        hub.disconnect(ws)

@app.websocket("/ws/control")
async def ws_control(ws: WebSocket):
    await app.state.control_handler.handle_connection(ws)

# Static files (served by nginx in prod, useful in dev)
import os
static_dir = os.path.join(os.path.dirname(__file__), "..", "web")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
```

---

## 11. Watchdog Service

The watchdog is a **separate Python script** that runs as its own systemd service. It does not import gpiozero. It uses `lgpio` directly and `httpx` to check health.

```python
#!/usr/bin/env python3
# watchdog/main.py
"""
Claw Machine GPIO Watchdog.

Monitors the game server health endpoint. If the server is unresponsive
for WATCHDOG_FAIL_THRESHOLD consecutive checks, forces all GPIO output
pins LOW using lgpio directly.

This process does NOT use gpiozero and does NOT conflict with the game
server's pin ownership during normal operation. It only claims pins
during an emergency.
"""
import time
import sys
import os
import logging
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [watchdog] %(message)s")
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
    import lgpio
    try:
        h = lgpio.gpiochip_open(0)  # gpiochip0 on Pi 5
        for pin in OUTPUT_PINS:
            try:
                lgpio.gpio_claim_output(h, pin, 0)  # Claim and set LOW
            except lgpio.error as e:
                # Pin may be claimed by the (now dead/frozen) game server.
                # On the Pi 5 with lgpio, re-claiming usually succeeds
                # because the kernel allows it.
                logger.warning(f"Could not claim pin {pin}: {e}")
        lgpio.gpiochip_close(h)
        logger.critical("WATCHDOG: All pins forced OFF")
    except Exception as e:
        logger.critical(f"WATCHDOG: lgpio force-off FAILED: {e}")

def main():
    fail_count = 0
    triggered = False
    logger.info(f"Watchdog started. Health URL: {HEALTH_URL}")
    logger.info(f"Check interval: {CHECK_INTERVAL}s, threshold: {FAIL_THRESHOLD}")

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
                    logger.warning(f"Health check returned {r.status_code} (fail {fail_count}/{FAIL_THRESHOLD})")
        except Exception as e:
            fail_count += 1
            logger.warning(f"Health check failed: {e} (fail {fail_count}/{FAIL_THRESHOLD})")

        if fail_count >= FAIL_THRESHOLD and not triggered:
            force_all_pins_off()
            triggered = True

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
```

---

## 12. Frontend — Key Implementation Details

### 12.1 WebRTC Stream Connection

```javascript
// web/stream.js
class StreamPlayer {
  constructor(videoElement, streamBaseUrl) {
    this.video = videoElement;
    this.baseUrl = streamBaseUrl; // e.g., "/stream/cam"
    this.pc = null;
    this.sessionUrl = null;
  }

  async connect() {
    this.pc = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });

    this.pc.addTransceiver("video", { direction: "recvonly" });
    this.pc.addTransceiver("audio", { direction: "recvonly" });

    this.pc.ontrack = (event) => {
      this.video.srcObject = event.streams[0];
    };

    this.pc.oniceconnectionstatechange = () => {
      if (this.pc.iceConnectionState === "failed" ||
          this.pc.iceConnectionState === "disconnected") {
        console.warn("Stream disconnected, reconnecting in 3s...");
        setTimeout(() => this.reconnect(), 3000);
      }
    };

    const offer = await this.pc.createOffer();
    await this.pc.setLocalDescription(offer);

    // Wait for ICE gathering to complete (or timeout)
    await new Promise((resolve) => {
      if (this.pc.iceGatheringState === "complete") {
        resolve();
      } else {
        const check = () => {
          if (this.pc.iceGatheringState === "complete") {
            this.pc.removeEventListener("icegatheringstatechange", check);
            resolve();
          }
        };
        this.pc.addEventListener("icegatheringstatechange", check);
        setTimeout(resolve, 2000); // Timeout fallback
      }
    });

    const res = await fetch(this.baseUrl + "/whep", {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: this.pc.localDescription.sdp,
    });

    if (res.status !== 201) {
      throw new Error(`WHEP failed: ${res.status}`);
    }

    this.sessionUrl = res.headers.get("Location");
    const answerSdp = await res.text();
    await this.pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
  }

  async reconnect() {
    this.disconnect();
    try {
      await this.connect();
    } catch (e) {
      console.error("Reconnect failed:", e);
      setTimeout(() => this.reconnect(), 5000);
    }
  }

  disconnect() {
    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }
    if (this.sessionUrl) {
      fetch(this.sessionUrl, { method: "DELETE" }).catch(() => {});
      this.sessionUrl = null;
    }
  }
}
```

### 12.2 Control WebSocket with Reconnection

```javascript
// web/controls.js
class ControlSocket {
  constructor(token) {
    this.token = token;
    this.ws = null;
    this.reconnectDelay = 1000;
    this.latencyMs = 0;
    this.onStateChange = null;
  }

  connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    this.ws = new WebSocket(`${proto}//${location.host}/ws/control`);

    this.ws.onopen = () => {
      this.ws.send(JSON.stringify({ type: "auth", token: this.token }));
      this.reconnectDelay = 1000;
    };

    this.ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === "latency_ping") {
        this.ws.send(JSON.stringify({ type: "latency_pong", server_time: msg.server_time }));
        this.latencyMs = (Date.now() / 1000 - msg.server_time) * 1000;
      }
      if (msg.type === "state_update" && this.onStateChange) {
        this.onStateChange(msg);
      }
    };

    this.ws.onclose = () => {
      setTimeout(() => this.connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 10000);
    };
  }

  send(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  keydown(key) { this.send({ type: "keydown", key }); }
  keyup(key) { this.send({ type: "keyup", key }); }
  drop() { this.send({ type: "drop" }); }
  readyConfirm() { this.send({ type: "ready_confirm" }); }
}
```

### 12.3 Touch D-Pad (Mobile)

```javascript
// web/touch_dpad.js
class TouchDPad {
  constructor(element, controlSocket) {
    this.el = element;
    this.ctrl = controlSocket;
    this.activeKey = null;

    this.el.style.touchAction = "none"; // Prevent scrolling/zooming

    this.el.addEventListener("pointerdown", (e) => this._onPointer(e));
    this.el.addEventListener("pointermove", (e) => this._onPointer(e));
    this.el.addEventListener("pointerup", () => this._release());
    this.el.addEventListener("pointercancel", () => this._release());
    this.el.addEventListener("pointerleave", () => this._release());
  }

  _onPointer(e) {
    e.preventDefault();
    const rect = this.el.getBoundingClientRect();
    const cx = rect.width / 2;
    const cy = rect.height / 2;
    const x = e.clientX - rect.left - cx;
    const y = e.clientY - rect.top - cy;

    // Dead zone (center 20%)
    const deadZone = rect.width * 0.1;
    if (Math.abs(x) < deadZone && Math.abs(y) < deadZone) {
      this._release();
      return;
    }

    // Determine direction by dominant axis
    let newKey;
    if (Math.abs(x) > Math.abs(y)) {
      newKey = x > 0 ? "east" : "west";
    } else {
      newKey = y > 0 ? "south" : "north";
    }

    if (newKey !== this.activeKey) {
      if (this.activeKey) this.ctrl.keyup(this.activeKey);
      this.ctrl.keydown(newKey);
      this.activeKey = newKey;
    }
  }

  _release() {
    if (this.activeKey) {
      this.ctrl.keyup(this.activeKey);
      this.activeKey = null;
    }
  }
}
```

### 12.4 Keyboard Controls (Desktop)

```javascript
// web/keyboard.js
function setupKeyboard(controlSocket) {
  const KEY_MAP = {
    ArrowUp: "north", KeyW: "north",
    ArrowDown: "south", KeyS: "south",
    ArrowLeft: "west", KeyA: "west",
    ArrowRight: "east", KeyD: "east",
  };
  const pressed = new Set();

  document.addEventListener("keydown", (e) => {
    const dir = KEY_MAP[e.code];
    if (dir && !pressed.has(dir)) {
      pressed.add(dir);
      controlSocket.keydown(dir);
      e.preventDefault();
    }
    if (e.code === "Space") {
      controlSocket.drop();
      e.preventDefault();
    }
  });

  document.addEventListener("keyup", (e) => {
    const dir = KEY_MAP[e.code];
    if (dir && pressed.has(dir)) {
      pressed.delete(dir);
      controlSocket.keyup(dir);
      e.preventDefault();
    }
  });

  // Safety: release all on window blur
  window.addEventListener("blur", () => {
    for (const dir of pressed) {
      controlSocket.keyup(dir);
    }
    pressed.clear();
  });
}
```

---

## 13. Deployment Configuration

### 13.1 MediaMTX Config

```yaml
# /etc/mediamtx.yml
logLevel: warn

webrtc:
  address: :8889
  iceServers:
    - url: stun:stun.l.google.com:19302

rtsp:
  address: :8554

paths:
  cam:
    source: rpiCamera
    sourceOnDemand: false
    rpiCameraWidth: 1280
    rpiCameraHeight: 720
    rpiCameraFPS: 30
    rpiCameraBitrate: 2000000
    rpiCameraIDRPeriod: 30
    rpiCameraAfMode: continuous
    rpiCameraAfRange: normal
    # Uncomment if mounted upside down:
    # rpiCameraHFlip: true
    # rpiCameraVFlip: true
```

### 13.2 systemd Services

**mediamtx.service:**
```ini
[Unit]
Description=MediaMTX Streaming Server
After=network.target

[Service]
ExecStart=/usr/local/bin/mediamtx /etc/mediamtx.yml
Restart=always
RestartSec=3
User=mediamtx
SupplementaryGroups=video
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
```

**claw-server.service:**
```ini
[Unit]
Description=Claw Machine Game Server
After=network.target mediamtx.service
Wants=mediamtx.service

[Service]
Type=exec
WorkingDirectory=/opt/claw
EnvironmentFile=/opt/claw/.env
ExecStart=/opt/claw/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3
User=claw
SupplementaryGroups=gpio
Environment=PYTHONUNBUFFERED=1
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

**claw-watchdog.service:**
```ini
[Unit]
Description=Claw Machine GPIO Watchdog
After=claw-server.service

[Service]
Type=exec
EnvironmentFile=/opt/claw/.env
ExecStart=/opt/claw/venv/bin/python /opt/claw/watchdog/main.py
Restart=always
RestartSec=1
User=claw
SupplementaryGroups=gpio

[Install]
WantedBy=multi-user.target
```

### 13.3 nginx Site Config

```nginx
# /etc/nginx/sites-available/claw
limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;
limit_req_zone $binary_remote_addr zone=join:10m rate=3r/m;

server {
    listen 80;
    server_name claw.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name claw.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/claw.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/claw.yourdomain.com/privkey.pem;

    # Security headers
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    add_header Referrer-Policy strict-origin-when-cross-origin;

    # Static frontend
    root /opt/claw/web;
    index index.html;
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API — general rate limit
    location /api/ {
        limit_req zone=api burst=20 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Queue join — stricter rate limit
    location = /api/queue/join {
        limit_req zone=join burst=2 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # WebSocket — status
    location /ws/status {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }

    # WebSocket — control
    location /ws/control {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }

    # MediaMTX WebRTC (WHEP + signaling)
    location /stream/ {
        proxy_pass http://127.0.0.1:8889/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }

    # Admin — IP restricted
    location /admin/ {
        allow 192.168.0.0/16;
        allow 10.0.0.0/8;
        deny all;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 14. Testing Strategy

### 14.1 Unit Tests (run on any machine, no GPIO needed)

gpiozero provides a mock pin factory for testing:

```python
# tests/conftest.py
import os
os.environ["GPIOZERO_PIN_FACTORY"] = "mock"

import pytest
from app.config import settings

@pytest.fixture
def mock_settings(tmp_path):
    settings.database_path = str(tmp_path / "test.db")
    return settings
```

**Test the state machine** by driving it through transitions and asserting states:

```python
# tests/test_state_machine.py
import pytest
import asyncio

@pytest.mark.asyncio
async def test_full_game_loop(mock_settings):
    # Setup components with mock GPIO
    # Join a player, advance queue, confirm ready
    # Assert state is MOVING
    # Send drop, assert DROPPING -> POST_DROP
    # Trigger win, assert turn ends with "win"
    # Assert queue advances
    pass

@pytest.mark.asyncio
async def test_move_timeout_auto_drops(mock_settings):
    # Enter MOVING state
    # Wait for TRY_MOVE_SECONDS
    # Assert auto-drop fired
    pass

@pytest.mark.asyncio
async def test_disconnect_forces_outputs_off(mock_settings):
    # Enter MOVING state with active direction
    # Call handle_disconnect
    # Assert all directions OFF
    pass
```

### 14.2 Integration Tests (on Pi, with real GPIO)

```bash
# scripts/gpio_test.py — Phase B validation
# Manually test each pin, verify with multimeter or LEDs
# Run 200+ pulse/hold cycles, check for stuck outputs
```

### 14.3 Acceptance Tests (on Pi, with real machine)

| Test | Procedure | Pass Criteria |
|------|-----------|---------------|
| Kill browser tab | Close active player's tab mid-move | All outputs OFF within 2s |
| Kill server process | `kill $(pidof uvicorn)` during active turn | Watchdog forces outputs OFF within 6s |
| Freeze server | `kill -STOP $(pidof uvicorn)` during active turn | Watchdog forces outputs OFF within 6s |
| Restart server | `systemctl restart claw-server` with queue | Queue preserved, resumes or advances |
| Hold timeout | Hold a direction for 31s | Auto-releases at 30s |
| Rapid direction toggle | Alternate N/S at 30Hz | No opposing simultaneous outputs |
| Concurrent viewers | 10 browsers watching stream | Stream stable, CPU < 80% |
| Queue spam | Attempt 10 joins from same IP in 1 min | Rate limited after 5 |

---

## 15. Project File Structure

```
/opt/claw/
├── .env                          # Configuration
├── requirements.txt              # Python dependencies
│
├── app/
│   ├── __init__.py
│   ├── main.py                   # FastAPI app + lifespan
│   ├── config.py                 # Pydantic Settings
│   ├── database.py               # aiosqlite + migrations
│   ├── models.py                 # Shared Pydantic models
│   │
│   ├── gpio/
│   │   ├── __init__.py
│   │   └── controller.py         # GPIOController (gpiozero wrapper)
│   │
│   ├── game/
│   │   ├── __init__.py
│   │   ├── queue_manager.py      # Queue CRUD
│   │   └── state_machine.py      # Turn/try logic
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py             # Public REST endpoints
│   │   └── admin.py              # Admin endpoints
│   │
│   └── ws/
│       ├── __init__.py
│       ├── status_hub.py         # Broadcast WebSocket hub
│       └── control_handler.py    # Player control WebSocket
│
├── watchdog/
│   └── main.py                   # Standalone watchdog (lgpio direct)
│
├── migrations/
│   └── 001_initial.sql           # Database schema
│
├── web/
│   ├── index.html
│   ├── style.css
│   ├── app.js                    # Main UI orchestration
│   ├── stream.js                 # WebRTC WHEP connection
│   ├── controls.js               # Control WebSocket + reconnect
│   ├── keyboard.js               # Desktop keyboard handler
│   └── touch_dpad.js             # Mobile touch D-pad
│
├── deploy/
│   ├── systemd/
│   │   ├── mediamtx.service
│   │   ├── claw-server.service
│   │   └── claw-watchdog.service
│   └── nginx/
│       └── claw.conf
│
├── scripts/
│   ├── gpio_test.py              # Phase B manual test
│   ├── stress_test.py            # Rapid toggle stress test
│   └── simulate_player.py        # Automated play simulation
│
└── tests/
    ├── conftest.py
    ├── test_state_machine.py
    ├── test_queue_manager.py
    ├── test_gpio_controller.py
    └── test_api.py
```

---

## 16. Implementation Phases (Developer Milestones)

### Phase A — Streaming (Days 1–3)
- [ ] Flash Pi OS Lite 64-bit, update, enable camera
- [ ] Test camera: `rpicam-still -o test.jpg`
- [ ] Install MediaMTX, configure `mediamtx.yml`
- [ ] Create and enable `mediamtx.service`
- [ ] Open browser to `http://<pi>:8889/cam`, verify WebRTC stream
- [ ] Measure latency (should be < 300ms on LAN)
- [ ] Verify RTSP: `vlc rtsp://<pi>:8554/cam`

### Phase B — GPIO (Days 3–5)
- [ ] Create venv, install gpiozero via pip, verify `OutputDevice` works
- [ ] Wire 6 LEDs + 1 button to breadboard per pin table
- [ ] Implement `GPIOController` with all safety features
- [ ] Run `scripts/gpio_test.py`: 200+ cycles, zero stuck outputs
- [ ] Implement and test watchdog script (`watchdog/main.py`)
- [ ] Test watchdog: stop server, verify watchdog forces pins OFF

### Phase C — Web App MVP (Days 5–12)
- [ ] Scaffold FastAPI app with lifespan
- [ ] Implement database schema + `database.py`
- [ ] Implement `QueueManager` (join, leave, status, persistence)
- [ ] Implement `StatusHub` (broadcast WebSocket)
- [ ] Implement `ControlHandler` (auth, control messages)
- [ ] Implement REST routes (join, leave, status, session/me, health)
- [ ] Build frontend: video embed, queue display, control pad (desktop + mobile)
- [ ] Test: 2 browsers — 1 controls LEDs, 1 watches but cannot control
- [ ] Test: disconnect active player → LEDs OFF within 2s
- [ ] Test: restart server → queue entries preserved

### Phase D — State Machine + Win (Days 12–18)
- [ ] Implement `StateMachine` with full state graph
- [ ] Implement ready prompt (WS notification + confirm message)
- [ ] Implement per-try coin pulse + move timer + auto-drop
- [ ] Implement POST_DROP win detection with debounce
- [ ] Implement try counting, turn advancement
- [ ] Add admin endpoints (advance, emergency stop, pause/resume)
- [ ] Run 20+ automated game loops via `scripts/simulate_player.py`
- [ ] Test all edge cases: timeout, win, disconnect, browser refresh, multiple tries

### Phase E — Real Machine (Days 18–25)
- [ ] Survey machine electrically (see Appendix D)
- [ ] Install opto-isolation board
- [ ] Connect and test one function at a time: coin → directions → drop → win
- [ ] Optimize camera angle and lighting
- [ ] 30 real game sessions
- [ ] Crash/disconnect safety tests on real hardware
- [ ] 2-hour unattended soak test

### Phase F — Production (Days 25–35)
- [ ] Register domain, configure DNS
- [ ] Install certbot, obtain TLS certificate
- [ ] Configure nginx reverse proxy
- [ ] Test WebRTC from external networks (home, mobile, corporate WiFi)
- [ ] Add TURN server if needed
- [ ] Email verification on queue join (optional)
- [ ] Admin dashboard page
- [ ] 4-hour sustained test with 20+ viewers
- [ ] Document machine-specific settings in `.env`

---

## Appendix A — GPIO Wiring Table

| Function | BCM | Phys Pin | Dir | Mode | Pulse/Hold | Safety |
|----------|-----|----------|-----|------|------------|--------|
| Coin | 17 | 11 | OUT | Pulse | 150ms on, 500ms cooldown | — |
| North | 27 | 13 | OUT | Hold | Until release | 30s max |
| South | 5 | 29 | OUT | Hold | Until release | 30s max |
| West | 6 | 31 | OUT | Hold | Until release | 30s max |
| East | 24 | 18 | OUT | Hold | Until release | 30s max |
| Drop | 25 | 22 | OUT | Pulse | 200ms on, 500ms cooldown | 1/try |
| Win | 16 | 36 | IN | Edge | 100ms debounce | POST_DROP only |

**Adjacent pin check** (verify no opposing functions are next to each other):
- Pin 11 (Coin) ↔ Pin 13 (North) — not opposing ✓
- Pin 29 (South) ↔ Pin 31 (West) — not opposing ✓
- Pin 18 (East) ↔ Pin 22 (Drop) — not opposing ✓

## Appendix B — Machine Integration Checklist

Complete this before wiring anything to the machine:

- [ ] Identify coin mechanism wiring point. Measured voltage: ___ V. Active: HIGH / LOW / switch-to-ground.
- [ ] Identify North joystick line. Measured voltage: ___ V. Active: HIGH / LOW / switch-to-ground.
- [ ] Identify South joystick line. Measured voltage: ___ V. Active: HIGH / LOW / switch-to-ground.
- [ ] Identify West joystick line. Measured voltage: ___ V. Active: HIGH / LOW / switch-to-ground.
- [ ] Identify East joystick line. Measured voltage: ___ V. Active: HIGH / LOW / switch-to-ground.
- [ ] Identify Drop mechanism. Measured voltage: ___ V. Active: HIGH / LOW / switch-to-ground.
- [ ] Identify win detection source: chute sensor / microswitch / win lamp / other: ___
- [ ] Win signal type: voltage level / switch closure / open collector. Voltage: ___ V.
- [ ] Machine has internal timer: YES / NO. Duration: ___ seconds.
- [ ] Machine requires coin per try: YES / NO. Coins per credit: ___.
- [ ] Machine auto-drops if internal timer expires: YES / NO.
- [ ] Photographs taken: control board / wiring harness / sensor locations.

## Appendix C — Message Protocol Reference

### WebSocket `/ws/control` — Client → Server

| Message | When | Fields |
|---------|------|--------|
| `{"type":"auth","token":"..."}` | Immediately after connect | `token`: bearer token from join |
| `{"type":"ready_confirm"}` | When prompted to start turn | — |
| `{"type":"keydown","key":"north"}` | Direction press | `key`: north/south/east/west |
| `{"type":"keyup","key":"north"}` | Direction release | `key`: north/south/east/west |
| `{"type":"drop"}` | Drop button pressed | — |
| `{"type":"latency_pong","server_time":...}` | Response to ping | Echo `server_time` back |

### WebSocket `/ws/control` — Server → Client

| Message | When | Fields |
|---------|------|--------|
| `{"type":"auth_ok","state":"...","position":N}` | After successful auth | Current state + position |
| `{"type":"error","message":"..."}` | On invalid action | Human-readable error |
| `{"type":"control_ack","key":"...","active":bool}` | After keydown processed | Whether it was accepted |
| `{"type":"state_update","state":"...","current_try":N,...}` | On state transitions | Full state payload |
| `{"type":"latency_ping","server_time":...}` | Periodic (~5s) | Server timestamp |

### WebSocket `/ws/status` — Server → All Viewers

| Message | When | Fields |
|---------|------|--------|
| `{"type":"queue_update","queue_length":N,"current_player":"..."}` | Queue changes | — |
| `{"type":"state_update","state":"...","current_try":N,...}` | Game state changes | — |
| `{"type":"turn_end","result":"win\|loss\|expired"}` | Turn finishes | — |
