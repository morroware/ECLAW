# ECLAW — Remote Claw Machine Controller

A full-stack platform for controlling a physical claw machine remotely over the web. Players join a queue, watch a live camera stream via WebRTC, control the claw in real-time with keyboard or touch controls, and see their results instantly.

Built for **Raspberry Pi 5** with real GPIO control, but runs anywhere with mock GPIO for development and testing. Designed and tested for **50+ concurrent internet users**.

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

See **[QUICKSTART.md](QUICKSTART.md)** for detailed step-by-step instructions including wiring guide, camera setup, internet deployment, and troubleshooting.

---

## How It Works

```
Player's Phone/Laptop
        |
    HTTPS + WSS
        |
   nginx (port 443)
   rate limiting + TLS
   connection limiting
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
2. **Queue advances** — when it's your turn, you get a ready prompt (all viewers see real-time queue updates)
3. **Confirm ready** — the claw machine credits a coin (via GPIO pulse)
4. **Move the claw** — WASD/arrows on desktop, touch D-pad on mobile (timers synced via SSOT deadlines)
5. **Drop** — space bar or DROP button fires the drop mechanism
6. **Win detection** — GPIO input pin checks if a prize was grabbed
7. **Results** — win/loss displayed, next player is automatically advanced
8. **Leave anytime** — players can leave the queue at any point, including while actively playing

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
docs/                   Architecture diagrams & protocol reference
install.sh              One-command setup (dev / pi / demo / test)
Makefile                Common commands
QUICKSTART.md           Detailed setup, wiring, and deployment guide
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
make audit-internet   # Offline internet-readiness config audit
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
| `CORS_ALLOWED_ORIGINS` | `http://localhost,http://127.0.0.1` | Comma-separated browser origins allowed to call API. **Set to your domain for internet deployment.** |

For PoC demos, use `.env.demo` which has shorter timers (15s move, 45s turn) for faster cycles:

```bash
cp .env.demo .env
# or: ECLAW_ENV_FILE=.env.demo make run
```

Full configuration reference is in `.env.example` and [docs/queue-flow.md](docs/queue-flow.md#13-configuration-reference).

---

## API

### Public Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/queue/join` | Join the queue (name + email) |
| DELETE | `/api/queue/leave` | Leave the queue — works in any state including active (Bearer token) |
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

- `/ws/status` — Broadcast to all viewers (queue updates, state changes, keepalive pings)
- `/ws/control` — Authenticated player channel (auth, controls, results)

Interactive API docs available at `/api/docs` (Swagger UI).

---

## Internet Deployment (50+ Users)

ECLAW is designed to serve 50+ concurrent internet users from a single Raspberry Pi 5. The architecture includes:

### Built-in Protection Layers

| Layer | Protection |
|-------|-----------|
| **nginx rate limiting** | 10 req/s per IP (API), 3 req/min per IP (queue join) |
| **nginx connection limiting** | 30 connections per IP max |
| **Application rate limiting** | 15 joins/hr per email, 30 joins/hr per IP, 25 Hz command rate |
| **WebSocket limits** | 500 status viewers, 100 control connections, 1024-byte message max |
| **Broadcast timeout** | 5s per-client send timeout prevents slow viewers from blocking others |
| **TLS + security headers** | HSTS, CSP, X-Frame-Options, X-Content-Type-Options |
| **Admin IP restriction** | Admin endpoints only accessible from private networks |

### Pre-deployment Checklist

Before opening to the internet:

1. Change `ADMIN_API_KEY` from `changeme` to a strong random value
2. Set `CORS_ALLOWED_ORIGINS` to your domain (e.g., `https://claw.yourdomain.com`)
3. Set up TLS certificates (Let's Encrypt recommended)
4. Update `server_name` in `deploy/nginx/claw.conf` to your domain
5. Run `./scripts/internet_readiness_audit.sh` to verify configuration
6. Consider putting Cloudflare or a similar WAF in front for DDoS protection

### Capacity

| Resource | Capacity |
|----------|----------|
| Concurrent viewers (WebSocket) | 500 |
| Concurrent queued players | 100 (WebSocket), unlimited (queue depth) |
| Video stream (WebRTC) | Handled by MediaMTX, efficient per-viewer |
| Video stream (MJPEG fallback) | 20 concurrent streams max |
| API throughput | 10 req/s per IP with burst of 20 |

See [docs/queue-flow.md](docs/queue-flow.md) for the complete architecture reference, flow charts, protocol documentation, and scaling analysis.

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

ECLAW includes multiple safety layers to prevent hardware damage and ensure fair play:

- **SSOT deadline tracking** — all timers backed by monotonic clock deadlines; clients receive accurate `state_seconds_left` on every state change and reconnect
- **DB deadline persistence** — `try_move_end_at` and `turn_end_at` written to SQLite for crash recovery reference
- **State machine timeouts** — auto-drop if player is idle, hard turn timeout (90s), ready timeout (15s)
- **Emergency stop** — admin endpoint locks all GPIO immediately
- **Watchdog process** — independent monitor that forces GPIO off if the server crashes (3 consecutive health check failures)
- **Rate limiting** — prevents input flooding (25 Hz max commands, nginx edge rate limiting)
- **Direction conflict handling** — prevents opposing directions simultaneously
- **Disconnect recovery** — directions released immediately on disconnect, 300s grace period for reconnection; timers continue running (no stuck queue)
- **Periodic safety net** — checks every 10s for stuck states and auto-recovers
- **Broadcast timeout** — per-client 5s send timeout prevents one slow viewer from blocking all others
- **Queue broadcast on ready** — viewers see real-time queue state changes (not just join/leave/turn-end)

---

## Camera Support

ECLAW supports two streaming modes:

- **WebRTC via MediaMTX** — Primary mode. Uses Pi Camera Module or USB webcam via FFmpeg. Low-latency WebRTC stream proxied through nginx. The setup script auto-detects your camera type.
- **Built-in MJPEG fallback** — If MediaMTX is not running (e.g., during development), the server captures directly from a USB camera via OpenCV and serves an MJPEG stream at `/api/stream/mjpeg`. Max 20 concurrent streams (enforced via semaphore).

---

## Documentation

- **[QUICKSTART.md](QUICKSTART.md)** — Step-by-step setup, wiring, camera configuration, and troubleshooting
- **[docs/queue-flow.md](docs/queue-flow.md)** — Complete architecture reference with Mermaid flow charts:
  - System architecture diagram
  - **Single Source of Truth (SSOT) design** — what is authoritative where
  - Queue entry lifecycle (database states, including active-player leave)
  - State machine turn flow with SSOT deadline tracking
  - Player join & turn sequence diagram (with deadline/broadcast annotations)
  - Page refresh & reconnection flow (with SSOT timer sync)
  - Disconnect & recovery flow
  - Safety nets & recovery architecture
  - WebSocket message reference (with `state_seconds_left` / `turn_seconds_left` fields)
  - REST API reference
  - Database schema (ER diagram, deadline columns documented)
  - Authentication & security measures
  - Scaling analysis for 50+ users
  - Full configuration reference
  - Hardware wiring diagram
  - Deployment architecture
  - Frontend architecture (with SSOT sync details)
  - Reconnection behavior

---

## Known Limitations

- Rate limiting is in-memory (single process) — nginx handles edge rate limiting for production
- Frontend is vanilla JS with no build tooling (intentional simplicity)
- Single uvicorn worker (required for GPIO ownership and shared state) — async handles concurrency
- SQLite is the only supported database (sufficient for single-machine deployment)
- Built-in MJPEG fallback requires `opencv-python-headless` and a USB camera (not Pi Camera CSI)
- `current_try` counter is in-memory only — on server restart, active entries are expired and the counter resets (by design: `cleanup_stale` handles recovery)
