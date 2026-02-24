# Remote Claw Quickstart

This guide reflects the current implementation in this repository.

## 1. Prerequisites

### Development host
- Python 3.10+
- `git`
- Linux/macOS shell environment

### Raspberry Pi target
- Raspberry Pi 5 (64-bit Raspberry Pi OS Bookworm)
- Network access
- Optional camera (Pi Camera or USB webcam)
- Optional relay wiring to claw machine I/O

## 2. Local Development Setup

```bash
git clone https://github.com/morroware/remote-claw.git remote-claw
cd remote-claw
./install.sh dev
make run
```

Open `http://localhost:8000`.

### Validate local setup

```bash
make test
make status
```

## 3. Demo vs Production Runtime

### Demo profile (short timers, local testing)

```bash
./install.sh demo
# or
make demo
```

### Production profile (Pi hardware)

```bash
./install.sh pi
```

## 4. Operator Workflow

1. Player joins via `POST /api/queue/join`.
2. Queue manager creates `waiting` entry.
3. State machine promotes next player to `ready_prompt`.
4. Player confirms readiness over `/ws/control`.
5. State machine transitions through `moving -> dropping -> post_drop` until result.
6. Entry finalizes (`win`, `loss`, `expired`, `skipped`, `admin_skipped`, or `cancelled`).
7. Next player advances automatically.

## 5. Admin Operations

- Admin panel URL: `/admin/panel`
- Authentication header for API: `X-Admin-Key`

Common tasks:
- Skip active player: `POST /admin/advance`
- Pause/resume queue: `POST /admin/pause`, `POST /admin/resume`
- Emergency relay lockout: `POST /admin/emergency-stop`, then `POST /admin/unlock`
- Update configuration: `GET/PUT /admin/config`
- Export contacts: `GET /admin/contacts/csv`

## 6. Hardware Pin Mapping (default)

| Function | BCM |
|---|---|
| Coin | 17 |
| North | 27 |
| South | 5 |
| West | 6 |
| East | 24 |
| Drop | 25 |
| Win sensor | 16 |

Pin values are configurable in `.env`.

## 7. Service Health and Logs

```bash
make status
make logs
make logs-watchdog
make logs-all
```

Health endpoint:

```text
GET /api/health
```

## 8. Safety Model

- Watchdog polls health endpoint and forces GPIO-safe state on repeated failures.
- State machine enforces hard turn timeout, ready timeout, and drop safety timeout.
- Disconnect handling turns directions off immediately.
- Periodic safety task recovers from stuck states.

## 9. Streaming Paths

- Primary (production): WebRTC via MediaMTX under `/stream/*`.
- Fallback (app-native): `/api/stream/mjpeg` and `/api/stream/snapshot`.

## 10. Next Documents

- [docs/queue-flow.md](docs/queue-flow.md)
- [docs/wordpress-embed.md](docs/wordpress-embed.md)
- [docs/video-stream-ssl-fix.md](docs/video-stream-ssl-fix.md)
