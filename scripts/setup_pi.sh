#!/bin/bash
# ============================================================
# Remote Claw — Pi 5 Setup Script
# ============================================================
# Run this on a Raspberry Pi 5 with Pi OS (64-bit).
# Supports both Lite and Desktop editions.
#
# Usage:
#   ./scripts/setup_pi.sh          # Full production setup
#   ./scripts/setup_pi.sh --demo   # Setup optimized for PoC demo
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

# Sanity check: make sure we're in the Remote Claw project root
for required_file in requirements.txt .env.example app/main.py; do
    if [ ! -f "$SCRIPT_DIR/$required_file" ]; then
        echo "ERROR: Cannot find $SCRIPT_DIR/$required_file"
        echo "This script must be run from a full Remote Claw repository clone."
        echo "  git clone <repo-url> && cd remote-claw && ./scripts/setup_pi.sh"
        exit 1
    fi
done

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

DEMO_MODE=false
if [[ "${1:-}" == "--demo" ]]; then
    DEMO_MODE=true
fi

echo ""
echo -e "${BOLD}========================================${NC}"
echo -e "${BOLD}  Remote Claw — Pi 5 Setup${NC}"
if $DEMO_MODE; then
echo -e "${BOLD}  Mode: PoC DEMO${NC}"
else
echo -e "${BOLD}  Mode: Production${NC}"
fi
echo -e "${BOLD}========================================${NC}"
echo ""

# --- Pre-flight checks ---
if [ "$(id -u)" -eq 0 ]; then
    die "Do not run this script as root. Run as a normal user with sudo access."
fi

if ! command -v sudo &>/dev/null; then
    die "sudo is required. Install it with: apt install sudo"
fi

if [ -f /proc/device-tree/model ]; then
    MODEL=$(tr -d '\0' < /proc/device-tree/model)
    ok "Detected: $MODEL"
else
    warn "This doesn't appear to be a Raspberry Pi."
    echo -n "  Continue anyway? [y/N] "
    read -r answer
    if [[ ! "$answer" =~ ^[Yy] ]]; then
        echo "Aborted."
        exit 0
    fi
fi

# --- [1/8] System packages ---
echo ""
echo -e "${BOLD}[1/8] Installing system packages...${NC}"
sudo apt update -qq
sudo apt install -y \
    python3 python3-venv python3-pip \
    python3-lgpio \
    nginx sqlite3 \
    ffmpeg v4l-utils \
    libopenblas0 libatlas-base-dev \
    curl wget 2>&1 | tail -1
ok "System packages installed"

# --- [2/8] Create users ---
echo ""
echo -e "${BOLD}[2/8] Creating system users...${NC}"
sudo useradd -r -s /usr/sbin/nologin -m -d /opt/mediamtx mediamtx 2>/dev/null && ok "Created user: mediamtx" || ok "User mediamtx already exists"
sudo useradd -r -s /usr/sbin/nologin -m -d /opt/claw claw 2>/dev/null && ok "Created user: claw" || ok "User claw already exists"
sudo usermod -a -G gpio,video claw 2>/dev/null || true
sudo usermod -a -G video mediamtx 2>/dev/null || true

# --- [3/8] Install MediaMTX ---
echo ""
echo -e "${BOLD}[3/8] Installing MediaMTX...${NC}"
MEDIAMTX_VERSION="1.12.3"

# Detect architecture
ARCH=$(uname -m)
case "$ARCH" in
    aarch64) MTX_ARCH="arm64" ;;
    armv7l)  MTX_ARCH="armv7" ;;
    x86_64)  MTX_ARCH="amd64" ;;
    *)       die "Unsupported architecture: $ARCH" ;;
esac

if [ -f /usr/local/bin/mediamtx ]; then
    ok "MediaMTX already installed"
else
    MTX_URL="https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_${MTX_ARCH}.tar.gz"
    info "Downloading MediaMTX ${MEDIAMTX_VERSION} for ${MTX_ARCH}..."
    cd /tmp
    wget -q "$MTX_URL" -O mediamtx.tar.gz || die "Failed to download MediaMTX"
    tar xzf mediamtx.tar.gz mediamtx
    sudo mv mediamtx /usr/local/bin/
    rm -f mediamtx.tar.gz
    cd "$SCRIPT_DIR"
    ok "MediaMTX ${MEDIAMTX_VERSION} installed"
fi

# --- [4/8] Set up application directory ---
echo ""
echo -e "${BOLD}[4/8] Setting up /opt/claw...${NC}"
sudo mkdir -p /opt/claw/data

# Copy application files
for dir in app migrations watchdog; do
    sudo cp -r "$SCRIPT_DIR/$dir" /opt/claw/
done
# web/ may not exist yet if the frontend hasn't been built
if [ -d "$SCRIPT_DIR/web" ]; then
    sudo cp -r "$SCRIPT_DIR/web" /opt/claw/
else
    warn "web/ directory not found — frontend files not deployed"
fi
sudo cp "$SCRIPT_DIR/requirements.txt" /opt/claw/

# Set up .env
if $DEMO_MODE && [ -f "$SCRIPT_DIR/.env.demo" ]; then
    sudo cp "$SCRIPT_DIR/.env.demo" /opt/claw/.env
    ok "Installed demo .env (short timers)"
else
    if [ -f /opt/claw/.env ]; then
        ok "Keeping existing /opt/claw/.env"
    else
        sudo cp "$SCRIPT_DIR/.env.example" /opt/claw/.env
        ok "Installed default .env"
    fi
fi

# Ensure correct GPIO settings on Pi
sudo sed -i 's/^MOCK_GPIO=true/MOCK_GPIO=false/' /opt/claw/.env
sudo sed -i 's/^# *GPIOZERO_PIN_FACTORY=lgpio/GPIOZERO_PIN_FACTORY=lgpio/' /opt/claw/.env
if ! grep -q "^GPIOZERO_PIN_FACTORY=" /opt/claw/.env; then
    echo "GPIOZERO_PIN_FACTORY=lgpio" | sudo tee -a /opt/claw/.env >/dev/null
fi

# Bind to all interfaces
sudo sed -i 's/^HOST=127.0.0.1/HOST=0.0.0.0/' /opt/claw/.env

sudo chown -R claw:claw /opt/claw
ok "Application files deployed to /opt/claw"

# --- [5/8] Python venv ---
echo ""
echo -e "${BOLD}[5/8] Creating Python virtual environment...${NC}"
sudo -u claw python3 -m venv --system-site-packages /opt/claw/venv
sudo -u claw /opt/claw/venv/bin/pip install --upgrade pip -q
sudo -u claw /opt/claw/venv/bin/pip install -r /opt/claw/requirements.txt -q
ok "Python venv created and dependencies installed"

# --- [6/8] Verify gpiozero ---
echo ""
echo -e "${BOLD}[6/8] Verifying GPIO access...${NC}"
if sudo -u claw GPIOZERO_PIN_FACTORY=lgpio /opt/claw/venv/bin/python -c "
from gpiozero import OutputDevice
d = OutputDevice(17)
d.on(); d.off(); d.close()
print('gpiozero OK - pin 17 tested')
" 2>/dev/null; then
    ok "GPIO access verified (lgpio backend)"
else
    warn "GPIO test failed — check that the claw user is in the gpio group"
    warn "Try: sudo usermod -a -G gpio claw && reboot"
fi

# --- [7/8] Deploy configs ---
echo ""
echo -e "${BOLD}[7/8] Installing service files and nginx config...${NC}"

# MediaMTX config — auto-detect camera type
CAMERA_TYPE="none"
if command -v rpicam-still &>/dev/null && rpicam-still --list-cameras 2>&1 | grep -q "Available cameras"; then
    CAMERA_TYPE="picam"
elif ls /dev/video* &>/dev/null; then
    CAMERA_TYPE="usb"
fi

if [ "$CAMERA_TYPE" = "picam" ]; then
    sudo cp "$SCRIPT_DIR/deploy/mediamtx.yml" /etc/mediamtx.yml
    ok "MediaMTX config installed (Pi Camera detected)"
elif [ "$CAMERA_TYPE" = "usb" ]; then
    sudo cp "$SCRIPT_DIR/deploy/mediamtx-usb.yml" /etc/mediamtx.yml
    ok "MediaMTX config installed (USB camera detected)"
else
    sudo cp "$SCRIPT_DIR/deploy/mediamtx.yml" /etc/mediamtx.yml
    warn "No camera detected — installed Pi Camera config as default"
    warn "If using a USB camera, copy deploy/mediamtx-usb.yml to /etc/mediamtx.yml"
fi

# Systemd services
sudo cp "$SCRIPT_DIR/deploy/systemd/mediamtx.service" /etc/systemd/system/
sudo cp "$SCRIPT_DIR/deploy/systemd/claw-server.service" /etc/systemd/system/
sudo cp "$SCRIPT_DIR/deploy/systemd/claw-watchdog.service" /etc/systemd/system/
sudo systemctl daemon-reload
ok "Systemd services installed"

# Nginx — use LAN config (no TLS needed for PoC)
sudo cp "$SCRIPT_DIR/deploy/nginx/claw-lan.conf" /etc/nginx/sites-available/claw
sudo ln -sf /etc/nginx/sites-available/claw /etc/nginx/sites-enabled/claw
sudo rm -f /etc/nginx/sites-enabled/default
ok "nginx config installed (LAN mode, no TLS)"

if sudo nginx -t 2>/dev/null; then
    sudo systemctl reload nginx
    ok "nginx config tested and reloaded"
else
    warn "nginx config test failed — check /etc/nginx/sites-available/claw"
fi

# --- [8/8] Enable and start services ---
echo ""
echo -e "${BOLD}[8/8] Enabling and starting services...${NC}"
sudo systemctl enable mediamtx claw-server claw-watchdog 2>/dev/null

# Clear any prior failure state / start-limit counters from previous installs
# so services can start cleanly (critical if MediaMTX was crash-looping).
sudo systemctl reset-failed mediamtx claw-server claw-watchdog 2>/dev/null || true

sudo systemctl start mediamtx 2>/dev/null || warn "mediamtx failed to start (camera may not be connected)"
sleep 1
sudo systemctl start claw-server 2>/dev/null || warn "claw-server failed to start"
sleep 1
sudo systemctl start claw-watchdog 2>/dev/null || warn "claw-watchdog failed to start"

# Check status
echo ""
for svc in mediamtx claw-server claw-watchdog nginx; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        ok "$svc is running"
    else
        warn "$svc is NOT running"
    fi
done

# --- Get Pi's IP address ---
PI_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<pi-ip>")

echo ""
echo -e "${BOLD}========================================${NC}"
echo -e "${GREEN}  Setup Complete!${NC}"
echo -e "${BOLD}========================================${NC}"
echo ""
echo "  Your Pi's IP address: $PI_IP"
echo ""
echo "  Access the game:"
echo "    Web UI:     http://$PI_IP"
echo "    API Health: http://$PI_IP/api/health"
echo "    API Docs:   http://$PI_IP/api/docs"
echo "    Admin:      http://$PI_IP/admin/dashboard"
echo "                (Header: X-Admin-Key: <your-admin-key>)"
echo ""
if $DEMO_MODE; then
echo "  Demo mode is active (short timers for fast demo cycles)"
echo ""
fi
echo "  Useful commands:"
echo "    sudo systemctl status claw-server    # Check game server"
echo "    sudo journalctl -u claw-server -f    # Tail game logs"
echo "    sudo systemctl restart claw-server   # Restart game"
echo "    make health-check                    # Run health check"
echo ""
echo "  IMPORTANT:"
echo "    1. Change ADMIN_API_KEY in /opt/claw/.env"
if [ "$CAMERA_TYPE" = "usb" ]; then
echo "    2. Camera: USB camera on /dev/video0 (list devices: v4l2-ctl --list-devices)"
else
echo "    2. Test camera: rpicam-still -o /tmp/test.jpg"
fi
echo "    3. Connect to http://$PI_IP from any device on your network"
echo ""
