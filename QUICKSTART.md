# ECLAW Quick Start Guide

This guide gets ECLAW running on a Raspberry Pi 5 for a PoC demo. If you just want to try it on your laptop first, skip to [Local Development](#local-development).

---

## What You Need

### For the Pi 5 Demo

- Raspberry Pi 5 (4GB+ RAM recommended)
- Pi OS 64-bit (Lite or Desktop) — Bookworm or newer
- MicroSD card (16GB+)
- Ethernet or Wi-Fi connection
- Pi Camera Module 3 **or** USB webcam (optional, for live stream)
- Relay board or direct wiring to claw machine (optional for first test)

### For Local Development (any machine)

- Python 3.11+
- git
- A web browser

---

## Local Development (Any Machine)

### Step 1: Clone and install

```bash
git clone https://github.com/morroware/ECLAW.git ECLAW
cd ECLAW
./install.sh dev
```

This creates a Python venv, installs all dependencies, and runs the test suite.

### Step 2: Start the server

```bash
make run
```

Or for faster demo cycles with shorter timers:

```bash
make demo
```

### Step 3: Open the UI

Open http://localhost:8000 in your browser. You should see the ECLAW interface with:
- A video area (shows WebRTC stream if MediaMTX is running, or built-in MJPEG if a USB camera is connected, or "Stream not available" if neither — all expected for local dev)
- A "Join the Queue" form
- A live queue list

### Step 4: Test the game flow

1. Enter a name and email, click "Join Queue"
2. You'll be prompted to confirm you're ready — click "READY"
3. Use WASD or arrow keys to "move" the claw (mock GPIO logs movements)
4. Press Space or click DROP to drop
5. After the drop, you'll see a result (win/loss)
6. You can also click "Leave Queue" at any point — even while actively playing (the turn ends immediately and the next player advances)

To simulate multiple players at once:

```bash
# In another terminal, with the server running:
make simulate
```

### Step 5: View the admin dashboard

```bash
curl -H "X-Admin-Key: changeme" http://localhost:8000/admin/dashboard | python3 -m json.tool
```

Or open http://localhost:8000/api/docs for the interactive Swagger UI (available in dev mode only; requires `MOCK_GPIO=true`).

### Step 6: Try the admin panel

Open http://localhost:8000/admin/panel in your browser. Enter the admin API key (`changeme` by default) to sign in. The admin panel provides:

- **Dashboard** — Live view of uptime, game state, viewers, queue size, win rate, active player
- **Game Controls** — Skip player, pause/resume queue, emergency stop, unlock GPIO
- **Queue Management** — View all players with IDs, kick individual players
- **Configuration** — Edit any server setting in real time; changes are saved to `.env`

For production, change the `ADMIN_API_KEY` in `.env` before sharing access.

---

## Raspberry Pi 5 Setup

### Step 1: Flash Pi OS

1. Download [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. Flash **Raspberry Pi OS (64-bit)** — Lite is sufficient
3. In Imager settings, enable SSH and set your Wi-Fi credentials
4. Boot the Pi and SSH in:
   ```bash
   ssh pi@raspberrypi.local
   ```

### Step 2: Clone the repository

```bash
git clone https://github.com/morroware/ECLAW.git ECLAW
cd ECLAW
```

### Step 3: Run the installer

For a PoC demo with short timers:

```bash
./install.sh demo
```

For production with standard timers:

```bash
./install.sh pi
```

The installer will:
1. Install system packages (python3, nginx, sqlite3, lgpio, ffmpeg, v4l-utils, libopenblas0, libatlas-base-dev)
2. Create system users (`claw` with gpio+video groups, `mediamtx` with video group)
3. Download and install MediaMTX (camera streaming server)
4. Deploy the app to `/opt/claw`
5. Set up the Python virtual environment with all dependencies (including OpenCV)
6. Test GPIO access
7. Install nginx, systemd services (with camera access for the game server)
8. Auto-detect camera type (Pi Camera vs USB) and configure MediaMTX accordingly
9. Start everything automatically

### Step 4: Verify it's running

After the installer finishes, it prints the Pi's IP address. From any device on the same network:

```
http://<pi-ip>
```

You can also run the health check:

```bash
./scripts/health_check.sh http://localhost
```

### Step 5: Connect from phones/laptops

Share the Pi's URL with anyone on the same network. The UI works on both desktop and mobile browsers:
- **Desktop**: Use WASD/arrow keys + Space to drop
- **Mobile**: Touch D-pad + DROP button

---

## Wiring the Claw Machine

ECLAW controls the claw machine via GPIO pins. Each pin drives a relay that switches the claw machine's physical controls.

### Default Pin Map (BCM numbering)

| Function | BCM Pin | Physical Pin | Notes |
|----------|---------|-------------|-------|
| Coin     | 17      | 11          | Pulse output (150ms) |
| North    | 27      | 13          | Hold output (direction) |
| South    | 5       | 29          | Hold output (direction) |
| West     | 6       | 31          | Hold output (direction) |
| East     | 24      | 18          | Hold output (direction) |
| Drop     | 25      | 22          | Pulse output (200ms) |
| Win      | 16      | 36          | Digital input (win sensor) |

### SainSmart 8-Channel Relay Board Wiring

SainSmart boards (and most 8-channel relay modules) are **active-low**: the relay engages when the input pin is driven LOW. ECLAW handles this automatically when `RELAY_ACTIVE_LOW=true` (the default).

**Board header pins:**

```
Relay board header:
  GND  ─── Pi GND (any ground pin, e.g. physical pin 6)
  IN1  ─── BCM 17 (coin)
  IN2  ─── BCM 27 (north)
  IN3  ─── BCM 5  (south)
  IN4  ─── BCM 6  (west)
  IN5  ─── BCM 24 (east)
  IN6  ─── BCM 25 (drop)
  IN7  ─── (unused)
  IN8  ─── (unused)
  VCC  ─── Pi 5V  (physical pin 2 or 4)
```

**Critical: JD-VCC jumper and relay coil power**

SainSmart boards have a **JD-VCC** jumper that controls power to the relay coils. This is the most common cause of "LEDs light up but relays don't click":

```
                ┌─────────────┐
  Signal side   │  JD-VCC VCC │   Relay coil side
  (optocoupler  │  [JUMPER]   │   (electromagnet)
   inputs)      │             │
                └─────────────┘
```

- **Jumper IN (default from factory)**: VCC powers both the signal side and relay coils. Connect VCC to the Pi's **5V pin** (not 3.3V).
- **Jumper OUT (isolated mode)**: You must supply a separate 5V power source to the JD-VCC pin for the relay coils. The Pi's VCC pin only powers the optocoupler/LED side. Use this mode if the Pi's 5V rail cannot supply enough current (each relay coil draws ~70-80mA; 6 active relays = ~450mA).

**If you see LEDs toggling but relays don't click**, check:

1. **VCC is connected to 5V, not 3.3V** — The 3.3V pin can light the LEDs but cannot drive the relay coils
2. **JD-VCC jumper is in place** — Or provide separate 5V to JD-VCC
3. **Power supply is adequate** — If using many relays simultaneously, use an external 5V supply on JD-VCC instead of the Pi's 5V rail

**Relay output wiring:**

Each relay has three terminals: COM (common), NO (normally open), NC (normally closed). Wire COM and NO in parallel with the claw machine's physical buttons — when the relay engages, it closes the NO contact, simulating a button press.

### Wiring Steps

1. Connect GND from the relay board to any Pi ground pin
2. Connect VCC from the relay board to the Pi's **5V** pin (physical pin 2 or 4)
3. Ensure the **JD-VCC jumper** is in place (or supply separate 5V to JD-VCC)
4. Connect each IN pin to the corresponding GPIO pin (see pin map above)
5. Wire relay outputs (COM + NO) in parallel with the claw machine's physical buttons
6. Connect the win sensor to GPIO 16 (pulls LOW when prize is detected)

### Test GPIO (before connecting to claw machine)

```bash
# Run the test — it reads pin config and polarity from .env:
cd /opt/claw
sudo -u claw venv/bin/python scripts/gpio_test.py --cycles 5

# The test will:
# 1. Show which polarity is configured
# 2. Hold each relay ON for 1 second (you should hear a click)
# 3. Run rapid-cycle tests
# 4. Test pulse timing
```

### Modify Pin Numbers

Edit `/opt/claw/.env`:

```ini
PIN_COIN=17
PIN_NORTH=27
PIN_SOUTH=5
PIN_WEST=6
PIN_EAST=24
PIN_DROP=25
PIN_WIN=16

# Active-low for SainSmart / most 8-channel boards (default: true)
RELAY_ACTIVE_LOW=true
```

Then restart: `sudo systemctl restart claw-server`

---

## Camera Setup

ECLAW supports two streaming modes:

1. **WebRTC via MediaMTX** (primary) — Low-latency WebRTC stream. Supports both Pi Camera modules and USB cameras. The setup script auto-detects which type is connected.
2. **Built-in MJPEG fallback** — If MediaMTX is unavailable, the game server captures directly from a USB camera via OpenCV and serves MJPEG at `/api/stream/mjpeg` plus snapshots at `/api/stream/snapshot`. The camera auto-detects the correct `/dev/video*` device and can fall back to RTSP input. This is useful for development or if MediaMTX has issues.

### Pi Camera

```bash
# Test the camera works:
rpicam-still -o /tmp/test.jpg
```

If this fails, check that the camera ribbon cable is seated properly and the camera interface is enabled in `raspi-config`.

Settings in `/etc/mediamtx.yml`:

```yaml
paths:
  cam:
    source: rpiCamera
    rpiCameraWidth: 1280
    rpiCameraHeight: 720
    rpiCameraFPS: 30
    # Uncomment if camera is upside down:
    # rpiCameraHFlip: true
    # rpiCameraVFlip: true
```

### USB Camera

If no Pi Camera is detected, the setup script automatically configures MediaMTX to use a USB camera via FFmpeg. The built-in MJPEG fallback also auto-detects the correct `/dev/video*` device (USB cameras often create multiple device nodes; the server scans even-numbered devices which are typically the capture interfaces).

```bash
# Check that your USB camera is recognized:
v4l2-ctl --list-devices

# List supported formats:
v4l2-ctl -d /dev/video0 --list-formats-ext
```

If your camera is on a different device (e.g. `/dev/video2`), edit `/etc/mediamtx.yml` and change the `-i /dev/video0` path in the `runOnInit` line. The built-in MJPEG fallback will auto-detect the device automatically.

**Important**: The user running the server needs to be in the `video` group to access `/dev/video*`:
```bash
sudo usermod -a -G video $USER
# Log out and back in for the group change to take effect
```

To manually switch to USB camera config:

```bash
sudo cp /opt/claw/deploy/mediamtx-usb.yml /etc/mediamtx.yml
sudo systemctl restart mediamtx
```

### Verify the stream

MediaMTX should already be running after install. Check:

```bash
# Direct stream test:
curl http://localhost:8889/v3/paths/list
```

The stream is available at:
- WebRTC: http://\<pi-ip\>:8889/cam (direct MediaMTX)
- Via nginx: http://\<pi-ip\>/stream/cam/whep (proxied, used by the UI)

---

## Custom Sound Effects

ECLAW plays synthesized sound effects for game events (join, your-turn, drop, win, loss, etc.) using the Web Audio API. You can replace any sound with a custom audio file:

1. Place audio files in the `web/sounds/` directory (or `/opt/claw/web/sounds/` on Pi)
2. Name the file to match the event: `join`, `your-turn`, `ready`, `move`, `drop`, `dropping`, `win`, `loss`, `timer`, `next-try`
3. Supported formats: `.mp3`, `.wav`, `.ogg`, `.webm`

Example: To use a custom win sound, place a file named `win.mp3` in `web/sounds/`. The engine checks for custom files on page load and falls back to synthesized sounds for any event without a custom file. Players can mute all sounds via a toggle in the UI.

---

## Internet Deployment (50+ Users)

To serve ECLAW over the public internet to 50+ concurrent users, follow these steps after completing the Pi 5 setup above.

### Step 1: Get a domain and TLS certificate

```bash
# Install certbot for Let's Encrypt:
sudo apt install -y certbot python3-certbot-nginx

# Obtain a certificate (replace with your domain):
sudo certbot --nginx -d claw.yourdomain.com

# Certbot auto-renews via systemd timer. Verify:
sudo systemctl status certbot.timer
```

### Step 2: Configure nginx

Edit the nginx config to use your domain:

```bash
sudo nano /etc/nginx/sites-enabled/claw.conf
```

Update `server_name` to your domain on both the HTTP and HTTPS server blocks. The certificate paths should already be set by certbot. The provided config includes:

- **Rate limiting**: 10 req/s per IP (API), 3 req/min per IP (queue join)
- **Connection limiting**: 30 connections per IP max
- **TLS hardening**: TLS 1.2+, strong ciphers, HSTS
- **Security headers**: CSP, X-Frame-Options, X-Content-Type-Options
- **WebSocket proxy**: buffering disabled for real-time control
- **Admin IP restriction**: only accessible from private networks
- **Static asset caching**: 1-hour browser cache for JS/CSS/images

Test and reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### Step 3: Update application settings

```bash
sudo nano /opt/claw/.env
```

Set these values:

```ini
# CRITICAL: Change from default
ADMIN_API_KEY=your-strong-random-key-here

# Set to your public domain (comma-separated if multiple)
CORS_ALLOWED_ORIGINS=https://claw.yourdomain.com

# Trust local nginx for X-Forwarded-For (required for rate limiting)
TRUSTED_PROXIES=127.0.0.1/32,::1/128

# Ensure real GPIO mode on Pi
MOCK_GPIO=false

# Auto-prune completed entries after this many hours (default: 48).
# Set higher for multi-day events where you want full audit history.
DB_RETENTION_HOURS=48
```

Restart the server:

```bash
sudo systemctl restart claw-server
```

### Step 4: Configure your router/firewall

- Forward ports **80** and **443** from your router to the Pi's local IP
- If using Cloudflare, point your DNS A record to your public IP and enable the proxy (orange cloud)
- WebRTC requires UDP traffic for the media stream; if behind a strict firewall, ensure STUN/TURN is reachable

### Step 5: Run the readiness audit

```bash
./scripts/internet_readiness_audit.sh --env /opt/claw/.env --nginx /etc/nginx/sites-enabled/claw.conf
```

This checks for common misconfigurations:
- ADMIN_API_KEY still set to `changeme`
- CORS_ALLOWED_ORIGINS too permissive
- nginx TLS and security header configuration
- Service health

### Step 6: Verify from outside your network

From a phone on cellular data (not your home Wi-Fi):

1. Open `https://claw.yourdomain.com`
2. Verify the video stream loads
3. Join the queue and complete a full game cycle
4. Check WebSocket connection (green dot in header)
5. Verify latency display shows reasonable numbers

### Capacity Reference

| Resource | Limit | Notes |
|----------|-------|-------|
| Concurrent viewers | 500 | WebSocket status hub |
| Concurrent players (queued) | 100 | WebSocket control connections |
| Queue depth | Unlimited | SQLite handles it |
| MJPEG streams (fallback) | 20 | Use WebRTC instead |
| API requests | 10/s per IP | Burst of 20 |
| Queue joins | 3/min per IP | Plus 15/hr per email |
| Connections per IP | 30 | nginx limit_conn |

### Monitoring

```bash
# Live server logs
sudo journalctl -u claw-server -f

# Admin dashboard (from Pi or local network)
curl -H "X-Admin-Key: YOUR_KEY" http://localhost/admin/dashboard | python3 -m json.tool

# System resource usage
htop

# nginx access log
sudo tail -f /var/log/nginx/access.log
```

---

## Demo Day Checklist

Before the demo:

- [ ] Pi 5 is powered and on the network
- [ ] `./scripts/health_check.sh http://localhost` shows all green
- [ ] Camera stream is visible at http://\<pi-ip\>:8889/cam
- [ ] Web UI loads at http://\<pi-ip\> (or https://your-domain.com)
- [ ] You can join the queue and complete a full game cycle
- [ ] GPIO pins are driving the claw relays correctly
- [ ] ADMIN_API_KEY has been changed from "changeme"
- [ ] CORS_ALLOWED_ORIGINS is set to your domain (not `*` or `localhost`)
- [ ] Note down the Pi's IP/URL to share with demo attendees

For internet-facing demos, also verify:

- [ ] `./scripts/internet_readiness_audit.sh` passes
- [ ] TLS certificate is valid (check browser padlock icon)
- [ ] WebSocket connects over WSS (green dot in header)
- [ ] Test from a device outside your network (e.g., phone on cellular)

During the demo:

- Share `http://<pi-ip>` with attendees
- Monitor with: `sudo journalctl -u claw-server -f`
- Emergency stop: `curl -X POST -H "X-Admin-Key: <key>" http://localhost/admin/emergency-stop`
- Force skip player: `curl -X POST -H "X-Admin-Key: <key>" http://localhost/admin/advance`
- Reset database: `sudo systemctl stop claw-server && rm /opt/claw/data/claw.db && sudo systemctl start claw-server`

---

## Troubleshooting

### Server won't start

```bash
sudo journalctl -u claw-server -n 50 --no-pager
```

Common causes:
- Missing `.env` file — copy from `.env.example`
- Port 8000 in use — change PORT in `.env`
- GPIO permission denied — `sudo usermod -a -G gpio claw && reboot`

### Camera not working

```bash
# Test Pi Camera directly:
rpicam-still -o /tmp/test.jpg

# Test USB camera:
v4l2-ctl --list-devices
ffmpeg -f v4l2 -i /dev/video0 -frames:v 1 /tmp/test.jpg

# Check MediaMTX logs:
sudo journalctl -u mediamtx -n 50 --no-pager

# Check built-in camera fallback (in game server logs):
sudo journalctl -u claw-server -n 50 --no-pager | grep -i camera
```

Common causes:
- **Permission denied on /dev/video***: User not in `video` group — `sudo usermod -a -G video $USER` and log out/in
- **Pi Camera**: Cable not seated, or interface not enabled (`sudo raspi-config` > Interface Options > Camera)
- **USB Camera**: Device not at `/dev/video0` — check `v4l2-ctl --list-devices` and update `/etc/mediamtx.yml` (the built-in MJPEG fallback auto-detects)
- **USB Camera**: Camera doesn't support MJPEG — change `-input_format mjpeg` to `-input_format yuyv422` in `/etc/mediamtx.yml`
- **Both**: Wrong config file deployed — check `/etc/mediamtx.yml` matches your camera type
- ffmpeg not installed (USB cameras require it for MediaMTX) — `sudo apt install ffmpeg`
- OpenCV not installed (needed for built-in MJPEG fallback) — `pip install opencv-python-headless`
- Missing system libraries for OpenCV — `sudo apt install libopenblas0 libatlas-base-dev`

### GPIO not responding

```bash
# Test GPIO directly:
sudo -u claw /opt/claw/venv/bin/python scripts/gpio_test.py --cycles 5
```

Common causes:
- `claw` user not in `gpio` group — `sudo usermod -a -G gpio claw`
- Wrong `GPIOZERO_PIN_FACTORY` setting (must be `lgpio` on Pi 5)
- Pin numbers don't match your wiring

### LEDs light up on relay board but relays don't click

This means GPIO signals are reaching the board but the relay coils don't have enough power:
- **VCC connected to 3.3V instead of 5V** — relay coils need 5V. Move VCC to physical pin 2 or 4
- **JD-VCC jumper missing** — check the jumper between JD-VCC and VCC on the relay board
- **Insufficient current** — if using 6+ relays, provide a separate 5V/2A supply to JD-VCC (remove the jumper first)
- **Wrong polarity** — verify `RELAY_ACTIVE_LOW=true` in `.env` for SainSmart boards

### WebSocket connection fails

- Check that nginx is running: `sudo systemctl status nginx`
- Check nginx config: `sudo nginx -t`
- For dev mode, connect directly to port 8000 (no nginx needed)

### Players can't connect from other devices

- Ensure the Pi and devices are on the same network
- Check firewall: `sudo ufw status` (if ufw is installed)
- Verify nginx listens on port 80: `sudo ss -tlnp | grep :80`

---

## Common Make Commands

```bash
make help           # Show all available commands
make install        # Set up dev environment
make install-prod   # Set up Pi 5 production environment
make run            # Start dev server (mock GPIO)
make run-prod       # Start production server (localhost bind)
make demo           # Start demo mode (short timers, mock GPIO)
make demo-pi        # Start demo on Pi 5 (short timers, real GPIO)
make test           # Run test suite
make simulate       # Simulate 3 players
make status         # Health check
make clean          # Remove cache + database
make db-reset       # Reset the database
make logs           # Tail game server logs
make logs-watchdog  # Tail watchdog logs
make logs-all       # Tail all service logs
make restart        # Restart all services
make stop           # Stop game server + watchdog
```

---

## Architecture Overview

```
                  +-----------+
                  |  Browser  |  (Desktop/Mobile)
                  +-----------+
                       |
              HTTP + WebSocket
              (SSOT timer sync)
                       |
                  +-----------+
                  |   nginx   |  (reverse proxy, port 80)
                  +-----------+
                   /         \
            +--------+   +----------+
            |FastAPI |   | MediaMTX |
            |:8000   |   | :8889    |
            +--------+   +----------+
                |              |
        +-------+-------+     |
        |       |       |     |
     Queue   State    GPIO  Camera (WebRTC or MJPEG fallback)
     (SQLite) Machine (lgpio)(rpicam/usb/opencv)
        |     (SSOT)    |
        |   deadlines   |
        +-------+-------+
                |
         Physical Claw
           Machine
```

**Single Source of Truth (SSOT):** The server's `StateMachine` tracks monotonic-clock deadlines for all timers. Every state update sent to clients includes `state_seconds_left` and `turn_seconds_left`, ensuring timers display accurately even after page refresh or network reconnection. Deadlines are also persisted to SQLite (`try_move_end_at`, `turn_end_at`) for crash recovery.
