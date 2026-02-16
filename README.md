# ECLAW — Remote Claw Machine Controller

A full-stack platform for controlling a physical claw machine remotely over the web. Players join a queue, watch a live camera stream via WebRTC, control the claw in real-time with keyboard or touch controls, and see their results instantly.

Built for **Raspberry Pi 5** with real GPIO control, but runs anywhere with mock GPIO for development and testing.

---

## Quick Start

### Try it locally (any machine)

```bash
git clone <repo-url> ECLAW && cd ECLAW
./install.sh dev
make run
# Open http://localhost:8000
```

### Deploy on Pi 5 for a PoC demo

```bash
git clone <repo-url> ECLAW && cd ECLAW
./install.sh demo
# Open http://<pi-ip> from any device on the network
```

### Deploy on Pi 5 for production

```bash
./install.sh pi
```

See **[QUICKSTART.md](QUICKSTART.md)** for detailed step-by-step instructions including wiring guide, camera setup, demo day checklist, and troubleshooting.

---

## How It Works

```
Player's Phone/Laptop
        |
    HTTP + WebSocket
        |
   nginx (port 80)
    /           \
FastAPI        MediaMTX
(game server)  (camera stream)
    |               |
 SQLite  GPIO    Pi Camera
 (queue) (lgpio)
    |       |
  Queue   Relays --> Physical Claw Machine
```

1. **Player joins** via the web UI — enters name/email, gets a queue position
2. **Queue advances** — when it's your turn, you get a ready prompt
3. **Confirm ready** — the claw machine credits a coin (via GPIO pulse)
4. **Move the claw** — WASD/arrows on desktop, touch D-pad on mobile
5. **Drop** — space bar or DROP button fires the drop mechanism
6. **Win detection** — GPIO input pin checks if a prize was grabbed
7. **Results** — win/loss displayed, next player is automatically advanced

---

## Project Structure

```
app/                    FastAPI backend (API, game logic, GPIO, camera, WebSocket)
  api/                  REST endpoints (public + admin + stream)
  game/                 Queue manager + state machine
  gpio/                 GPIO controller (gpiozero wrapper)
  ws/                   WebSocket hubs (status broadcast + player control)
  camera.py             Built-in USB camera capture (MJPEG fallback)
web/                    Browser UI (vanilla JS, no build step)
watchdog/               Independent GPIO safety monitor
migrations/             SQLite schema
deploy/                 nginx, systemd, MediaMTX configs
scripts/                Dev tools, health check, GPIO test, player simulator
tests/                  pytest test suite
install.sh              One-command setup (dev / pi / demo / test)
Makefile                Common commands
QUICKSTART.md           Detailed setup and demo guide
```

---

## Make Commands

```bash
make help             # Show all commands
make install          # Dev environment setup
make run              # Dev server (mock GPIO, auto-reload)
make demo             # Demo mode (short timers, mock GPIO)
make demo-pi          # Demo on Pi 5 (short timers, real GPIO)
make test             # Run test suite
make simulate         # Simulate 3 players
make status           # Health check
make logs             # Tail server logs
make restart          # Restart all services
make db-reset         # Reset database
```

---

## Configuration

All settings are in `.env` (copied from `.env.example` during install). Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `MOCK_GPIO` | `true` | Use mock GPIO (set `false` on Pi 5) |
| `TRIES_PER_PLAYER` | `2` | Number of drop attempts per turn |
| `TRY_MOVE_SECONDS` | `30` | Time to move before auto-drop |
| `TURN_TIME_SECONDS` | `90` | Hard limit for entire turn |
| `ADMIN_API_KEY` | `changeme` | **Change this in production** |
| `PORT` | `8000` | Server listen port |

For PoC demos, use `.env.demo` which has shorter timers (15s move, 45s turn) for faster cycles:

```bash
cp .env.demo .env
# or: ECLAW_ENV_FILE=.env.demo make run
```

Full configuration reference is in `.env.example`.

---

## API

### Public Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/queue/join` | Join the queue (name + email) |
| DELETE | `/api/queue/leave` | Leave the queue (Bearer token) |
| GET | `/api/queue/status` | Queue length + current player |
| GET | `/api/queue` | Full queue listing |
| GET | `/api/session/me` | Your session state (Bearer token) |
| GET | `/api/history` | Recent game results |
| GET | `/api/health` | Server health status |

### Admin Endpoints (require `X-Admin-Key` header)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/admin/advance` | Force-end current turn |
| POST | `/admin/emergency-stop` | Lock all GPIO |
| POST | `/admin/unlock` | Unlock GPIO |
| POST | `/admin/pause` | Pause queue |
| POST | `/admin/resume` | Resume queue |
| GET | `/admin/dashboard` | Full status dashboard |

### WebSockets

- `/ws/status` — Broadcast to all viewers (queue updates, state changes)
- `/ws/control` — Authenticated player channel (auth, controls, results)

Interactive API docs available at `/api/docs` (Swagger UI).

---

## Testing

```bash
make test             # Full test suite
make test-quick       # Quick run
make simulate         # 3 simulated players (sequential)
make simulate-parallel  # 5 simulated players (concurrent)
make status           # Health check against running server
```

---

## Requirements

### Development

- Python 3.11+
- No hardware required (uses mock GPIO)

### Pi 5 Production

- Raspberry Pi 5 with Pi OS 64-bit (Bookworm)
- `python3-lgpio` (installed automatically)
- `libopenblas0`, `libatlas-base-dev` (installed automatically, needed by OpenCV/numpy)
- nginx (installed automatically)
- MediaMTX (installed automatically)
- Pi Camera Module **or** USB webcam (for live stream)

---

## Safety

ECLAW includes multiple safety layers:

- **State machine timeouts** — auto-drop if player is idle, hard turn timeout
- **Emergency stop** — admin endpoint locks all GPIO immediately
- **Watchdog process** — independent monitor that forces GPIO off if the server crashes
- **Rate limiting** — prevents input flooding (25 Hz max)
- **Direction conflict handling** — prevents opposing directions simultaneously

---

## Camera Support

ECLAW supports two streaming modes:

- **WebRTC via MediaMTX** — Primary mode. Uses Pi Camera Module or USB webcam via FFmpeg. Low-latency WebRTC stream proxied through nginx. The setup script auto-detects your camera type.
- **Built-in MJPEG fallback** — If MediaMTX is not running (e.g., during development), the server captures directly from a USB camera via OpenCV and serves an MJPEG stream at `/api/stream/camera`. The camera auto-detects the correct `/dev/video*` device.

---

## Known Limitations

- Rate limiting is in-memory (single process) — fine for one Pi
- Frontend is vanilla JS with no build tooling (intentional simplicity)
- CORS is permissive (`*`) by default — restrict for internet-facing deployment
- Built-in MJPEG fallback requires `opencv-python-headless` and a USB camera (not Pi Camera CSI)
