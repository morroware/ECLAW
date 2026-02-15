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
git clone <your-repo-url> ECLAW
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
- A video area (will show "Stream not available" without a camera — that's expected)
- A "Join the Queue" form
- A live queue list

### Step 4: Test the game flow

1. Enter a name and email, click "Join Queue"
2. You'll be prompted to confirm you're ready — click "READY"
3. Use WASD or arrow keys to "move" the claw (mock GPIO logs movements)
4. Press Space or click DROP to drop
5. After the drop, you'll see a result (win/loss)

To simulate multiple players at once:

```bash
# In another terminal, with the server running:
make simulate
```

### Step 5: View the admin dashboard

```bash
curl -H "X-Admin-Key: changeme" http://localhost:8000/admin/dashboard | python3 -m json.tool
```

Or open http://localhost:8000/api/docs for the interactive Swagger UI.

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
git clone <your-repo-url> ECLAW
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
1. Install system packages (python3, nginx, sqlite3, lgpio)
2. Create system users (claw, mediamtx)
3. Download and install MediaMTX (camera streaming server)
4. Deploy the app to `/opt/claw`
5. Set up the Python virtual environment
6. Test GPIO access
7. Install nginx, systemd services
8. Start everything automatically

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

ECLAW controls the claw machine via GPIO pins. Each pin acts as a digital switch (HIGH/LOW) that can drive a relay or optocoupler.

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

### Wiring Steps

1. Connect each GPIO output pin to a relay module input
2. Connect relay outputs in parallel with the claw machine's physical buttons
3. Connect the win sensor to GPIO 16 (pulls LOW when prize is detected)
4. Power the relay module from the Pi's 5V and GND pins

### Test GPIO (before connecting to claw machine)

```bash
# With LEDs or multimeter on the pins:
cd /opt/claw
sudo -u claw venv/bin/python /path/to/ECLAW/scripts/gpio_test.py
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
```

Then restart: `sudo systemctl restart claw-server`

---

## Camera Setup

ECLAW streams video via MediaMTX using WebRTC. Both Pi Camera modules and USB cameras are supported. The setup script auto-detects which type is connected.

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

If no Pi Camera is detected, the setup script automatically configures MediaMTX to use a USB camera via FFmpeg.

```bash
# Check that your USB camera is recognized:
v4l2-ctl --list-devices

# List supported formats:
v4l2-ctl -d /dev/video0 --list-formats-ext
```

If your camera is on a different device (e.g. `/dev/video2`), edit `/etc/mediamtx.yml` and change the `-i /dev/video0` path in the `runOnInit` line.

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

Then: `sudo systemctl restart mediamtx`

---

## Demo Day Checklist

Before the demo:

- [ ] Pi 5 is powered and on the network
- [ ] `./scripts/health_check.sh http://localhost` shows all green
- [ ] Camera stream is visible at http://\<pi-ip\>:8889/cam
- [ ] Web UI loads at http://\<pi-ip\>
- [ ] You can join the queue and complete a full game cycle
- [ ] GPIO pins are driving the claw relays correctly
- [ ] ADMIN_API_KEY has been changed from "changeme"
- [ ] Note down the Pi's IP to share with demo attendees

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
```

Common causes:
- **Pi Camera**: Cable not seated, or interface not enabled (`sudo raspi-config` > Interface Options > Camera)
- **USB Camera**: Device not at `/dev/video0` — check `v4l2-ctl --list-devices` and update `/etc/mediamtx.yml`
- **USB Camera**: Camera doesn't support MJPEG — change `-input_format mjpeg` to `-input_format yuyv422` in `/etc/mediamtx.yml`
- **Both**: Wrong config file deployed — check `/etc/mediamtx.yml` matches your camera type
- ffmpeg not installed (USB cameras require it) — `sudo apt install ffmpeg`

### GPIO not responding

```bash
# Test GPIO directly:
sudo -u claw /opt/claw/venv/bin/python scripts/gpio_test.py --cycles 5
```

Common causes:
- `claw` user not in `gpio` group
- Wrong `GPIOZERO_PIN_FACTORY` setting (must be `lgpio` on Pi 5)
- Pin numbers don't match your wiring

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
make run            # Start dev server (mock GPIO)
make demo           # Start demo mode (short timers, mock GPIO)
make demo-pi        # Start demo on Pi 5 (short timers, real GPIO)
make test           # Run test suite
make simulate       # Simulate 3 players
make status         # Health check
make clean          # Remove cache + database
make db-reset       # Reset the database
make logs           # Tail game server logs
make restart        # Restart all services
```

---

## Architecture Overview

```
                  +-----------+
                  |  Browser  |  (Desktop/Mobile)
                  +-----------+
                       |
              HTTP + WebSocket
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
     Queue   State    GPIO  Camera
     (SQLite) Machine (lgpio)(rpicam/usb)
        |       |       |
        +-------+-------+
                |
         Physical Claw
           Machine
```
