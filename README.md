# ECLAW — Remote Claw Machine Controller

ECLAW is a full-stack remote claw machine platform built around FastAPI, WebSockets, SQLite, and Raspberry Pi GPIO control.

It lets remote players:
- join a queue,
- watch a live camera stream,
- control the claw in real time,
- and receive turn/game results.

The project includes local mock mode for development, production deployment assets for Raspberry Pi 5, and an independent watchdog process that can force GPIO outputs off if the game server fails.

---

## Table of Contents

- [What this project does](#what-this-project-does)
- [Core architecture](#core-architecture)
- [Repository layout](#repository-layout)
- [Requirements](#requirements)
- [Quick start (development)](#quick-start-development)
- [Configuration](#configuration)
- [How gameplay works](#how-gameplay-works)
- [Backend API and WebSocket interfaces](#backend-api-and-websocket-interfaces)
- [Database model](#database-model)
- [Production deployment (Raspberry Pi 5)](#production-deployment-raspberry-pi-5)
- [Operations and maintenance](#operations-and-maintenance)
- [Testing and validation](#testing-and-validation)
- [Known limitations and notes](#known-limitations-and-notes)

---

## What this project does

At runtime, ECLAW coordinates four concerns:

1. **Queueing** players (join, leave, session lookup).
2. **Turn orchestration** (ready prompt, timed tries, drop flow, win/loss/end).
3. **Real-time control** over WebSocket (`/ws/control`) for authenticated players.
4. **Live status broadcast** over WebSocket (`/ws/status`) for all viewers.

It also exposes:
- a public REST API under `/api/*`,
- admin controls under `/admin/*` (header-protected),
- static web UI served from `web/` for development.

---

## Core architecture

### Server-side components

- **FastAPI application** (`app/main.py`)
  - owns startup/shutdown lifecycle,
  - initializes DB, GPIO, queue manager, status hub, control handler, state machine,
  - mounts static frontend.

- **State machine** (`app/game/state_machine.py`)
  - canonical game flow + timers,
  - state transitions: `idle -> ready_prompt -> moving -> dropping -> post_drop -> turn_end/idle`,
  - handles turn timeout, move timeout, win callback bridge, and queue advancement.

- **Queue manager** (`app/game/queue_manager.py`)
  - SQLite-backed queue CRUD,
  - tokenized player identity,
  - stale-entry cleanup and queue status aggregation.

- **GPIO controller** (`app/gpio/controller.py`)
  - async-safe GPIO wrapper using a dedicated single-thread executor,
  - supports hold directions + pulse outputs (`coin`, `drop`),
  - emergency stop lock/unlock,
  - mock GPIO implementation for non-Pi development.

- **WebSocket layers**
  - `StatusHub` (`app/ws/status_hub.py`) for fan-out broadcast,
  - `ControlHandler` (`app/ws/control_handler.py`) for player auth + control events + rate limiting.

- **Watchdog process** (`watchdog/main.py`)
  - polls health endpoint,
  - on repeated failure, claims GPIO pins directly via `lgpio` and forces outputs LOW.

### Frontend components

- Static UI in `web/`:
  - `index.html` + `style.css` for layout,
  - `app.js` orchestrates state/UI,
  - `controls.js` manages control websocket and latency pings,
  - `keyboard.js` + `touch_dpad.js` provide desktop/mobile input,
  - `stream.js` handles WHEP/WebRTC playback from MediaMTX.

---

## Repository layout

```text
app/                    FastAPI backend (API, game logic, GPIO, WS)
web/                    Browser frontend (static app)
watchdog/               GPIO fail-safe monitor process
migrations/             SQLite schema migrations
deploy/                 nginx, MediaMTX, systemd templates
scripts/                Dev, health check, GPIO test, player simulation
tests/                  API + state-machine tests
install.sh              Interactive/dev/pi/test setup entrypoint
Makefile                Common developer and ops commands
```

---

## Requirements

### Development (local machine)

- Python **3.11+**
- `venv`
- no physical GPIO required (use `MOCK_GPIO=true`)

### Raspberry Pi production

- Raspberry Pi 5 (recommended)
- Python 3.11+
- gpio access (`lgpio` backend)
- MediaMTX for camera/streaming
- nginx for reverse proxy + TLS
- systemd for service management

---

## Quick start (development)

### 1) Install

```bash
./install.sh dev
```

This creates `venv/`, installs dev dependencies, prepares `.env`, verifies imports, and runs tests.

### 2) Start the app

```bash
make run
```

or:

```bash
./scripts/dev.sh
```

Default URL: `http://localhost:8000`

### 3) Run tests

```bash
make test
```

### 4) Simulate players (optional)

```bash
make simulate
```

---

## Configuration

Configuration is loaded by `pydantic-settings` from `.env` (or `ECLAW_ENV_FILE` if set).

Key groups:

- **Timing**
  - `TRIES_PER_PLAYER`
  - `TURN_TIME_SECONDS`
  - `TRY_MOVE_SECONDS`
  - `POST_DROP_WAIT_SECONDS`
  - `READY_PROMPT_SECONDS`
  - `QUEUE_GRACE_PERIOD_SECONDS`

- **GPIO behavior**
  - `COIN_PULSE_MS`, `DROP_PULSE_MS`, `MIN_INTER_PULSE_MS`
  - `DIRECTION_HOLD_MAX_MS`
  - `COIN_EACH_TRY`

- **Input control/rate limits**
  - `COMMAND_RATE_LIMIT_HZ`
  - `DIRECTION_CONFLICT_MODE` (`ignore_new` or `replace`)

- **Pin mapping (BCM)**
  - `PIN_COIN`, `PIN_NORTH`, `PIN_SOUTH`, `PIN_WEST`, `PIN_EAST`, `PIN_DROP`, `PIN_WIN`

- **Server/admin**
  - `HOST`, `PORT`, `DATABASE_PATH`, `ADMIN_API_KEY`

- **Watchdog/stream health**
  - `WATCHDOG_HEALTH_URL`, `WATCHDOG_CHECK_INTERVAL_S`, `WATCHDOG_FAIL_THRESHOLD`
  - `MEDIAMTX_HEALTH_URL`

- **Development mode**
  - `MOCK_GPIO=true` to run without hardware.

> ⚠️ Always set a strong `ADMIN_API_KEY` in production.

---

## How gameplay works

### High-level flow

1. Player submits `/api/queue/join` (name + email).
2. Backend enqueues player and may advance queue.
3. Next waiting player transitions to `ready` and receives `ready_prompt` on `/ws/control`.
4. Player confirms readiness.
5. Turn begins:
   - hard turn timer starts,
   - try begins (`coin` pulse if enabled),
   - movement phase (`moving`) starts.
6. During `moving`, player sends direction key events and can `drop`.
7. In `dropping`, direction outputs are shut off and `drop` pulse is fired.
8. In `post_drop`, win sensor callback is armed for a short timeout window.
9. Outcome:
   - win sensor => `win`,
   - tries remaining => next try,
   - no tries left => `loss`,
   - timeout/disconnect/admin => `expired` or `admin_skipped`.
10. Turn finalizes, queue status broadcasts, next player is considered.

### Safety behavior

- `emergency_stop()` immediately kills all outputs and sets lock.
- State machine unlocks after turn finalization.
- Watchdog can force pins OFF independently if server health collapses.

---

## Backend API and WebSocket interfaces

## REST API

### Public (`/api`)

- `POST /api/queue/join`
  - input: `{ name, email }`
  - output: `{ token, position, estimated_wait_seconds }`
  - in-memory anti-abuse limit per IP/email.

- `DELETE /api/queue/leave`
  - header: `Authorization: Bearer <token>`
  - marks waiting/ready entry cancelled.

- `GET /api/queue/status`
  - returns queue length + current player/state.

- `GET /api/session/me`
  - header: bearer token
  - returns current entry state, queue position, tries left.

- `GET /api/health`
  - server health + GPIO lock status + camera check + queue/viewer stats + uptime.

### Admin (`/admin`)

Header required: `X-Admin-Key: <ADMIN_API_KEY>`

- `POST /admin/advance` — force-end active turn.
- `POST /admin/emergency-stop` — lock GPIO immediately.
- `POST /admin/unlock` — clear emergency lock.
- `POST /admin/pause` — pause queue advancement.
- `POST /admin/resume` — resume and advance queue.

## WebSockets

- `/ws/status`
  - open to viewers,
  - receives broadcast events: `queue_update`, `state_update`, `turn_end`.

- `/ws/control`
  - authenticated player socket,
  - first message must be: `{ "type": "auth", "token": "..." }`,
  - accepted control messages: `ready_confirm`, `keydown`, `keyup`, `drop`, `latency_ping`.

---

## Database model

SQLite schema is initialized by migration scripts in `migrations/`.

Primary tables:

- `queue_entries`
  - player identity (token hash, name, email, IP),
  - queue/turn lifecycle fields (`state`, `position`, timestamps, `result`, `tries_used`).

- `game_events`
  - append-only audit/event log with optional JSON detail.

- `schema_version`
  - applied migration tracking.

DB path defaults to `./data/claw.db` in development.

---

## Production deployment (Raspberry Pi 5)

### One-command install path

```bash
./install.sh pi
```

This delegates to `scripts/setup_pi.sh`, which:
- installs system dependencies,
- creates `claw` and `mediamtx` service users,
- deploys app files into `/opt/claw`,
- builds venv and installs requirements,
- installs systemd services + nginx config,
- enables services.

### systemd services

- `mediamtx.service`
- `claw-server.service`
- `claw-watchdog.service`

### nginx role

`deploy/nginx/claw.conf` routes:
- `/` -> static frontend,
- `/api/*` -> FastAPI,
- `/ws/*` -> FastAPI websockets,
- `/stream/*` -> MediaMTX WebRTC paths.

Includes rate limiting and optional network restrictions for admin endpoints.

---

## Operations and maintenance

Useful commands:

```bash
make status          # health checks
make logs            # claw-server logs
make logs-watchdog   # watchdog logs
make logs-all        # all relevant services
make restart         # restart server/watchdog/mediamtx
make stop            # stop server/watchdog
```

Data and cleanup:

```bash
make db-reset        # delete SQLite DB
make clean           # caches + DB artifacts
make clean-all       # clean + remove venv
```

---

## Testing and validation

### Included automated tests

- `tests/test_api.py`
  - health endpoint,
  - queue join/status/session/leave,
  - request validation behavior.

- `tests/test_state_machine.py`
  - win callback bridge to running event loop.

### Run all tests

```bash
pytest -q
```

or:

```bash
make test
```

### Runtime smoke check

```bash
scripts/health_check.sh http://localhost:8000
```

---

## Known limitations and notes

- Queue status reports aggregate counts/current active player; it is not a full queue listing API.
- Join rate limiting is in-memory (per process), not distributed.
- Mock mode validates control flow but does not emulate physical machine timing perfectly.
- Frontend is static JS/CSS without build tooling (intentional simplicity).
- For internet-facing deployment, TLS and stricter CORS/origin policies are strongly recommended.

---

If you want, the next step can be adding:
1) sequence diagrams for state transitions and websocket events,
2) OpenAPI snippets/examples for each endpoint,
3) a dedicated operator runbook (incident handling + recovery checklist).
