#!/bin/bash
# ECLAW — Pi 5 Setup Script
# Run this on a fresh Raspberry Pi 5 with Pi OS Lite 64-bit
set -euo pipefail

echo "=== ECLAW Pi 5 Setup ==="

# --- System packages ---
echo "[1/8] Installing system packages..."
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y \
  python3 python3-venv python3-pip \
  python3-lgpio \
  nginx certbot python3-certbot-nginx \
  sqlite3

# --- Create users ---
echo "[2/8] Creating system users..."
sudo useradd -r -s /usr/sbin/nologin -m mediamtx 2>/dev/null || true
sudo useradd -r -s /usr/sbin/nologin -m claw 2>/dev/null || true
sudo usermod -a -G gpio claw 2>/dev/null || true
sudo usermod -a -G video mediamtx 2>/dev/null || true

# --- Install MediaMTX ---
echo "[3/8] Installing MediaMTX..."
MEDIAMTX_VERSION="1.12.3"
if [ ! -f /usr/local/bin/mediamtx ]; then
  cd /tmp
  wget -q "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_arm64.tar.gz"
  tar xzf mediamtx_*.tar.gz
  sudo mv mediamtx /usr/local/bin/
  rm -f mediamtx_*.tar.gz
  echo "MediaMTX ${MEDIAMTX_VERSION} installed"
else
  echo "MediaMTX already installed"
fi

# --- Set up application directory ---
echo "[4/8] Setting up /opt/claw..."
sudo mkdir -p /opt/claw/data
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
sudo cp -r "$SCRIPT_DIR/app" /opt/claw/
sudo cp -r "$SCRIPT_DIR/migrations" /opt/claw/
sudo cp -r "$SCRIPT_DIR/watchdog" /opt/claw/
sudo cp -r "$SCRIPT_DIR/web" /opt/claw/
sudo cp "$SCRIPT_DIR/requirements.txt" /opt/claw/
sudo cp "$SCRIPT_DIR/.env" /opt/claw/
if ! grep -q "^GPIOZERO_PIN_FACTORY=" /opt/claw/.env; then
  echo "GPIOZERO_PIN_FACTORY=lgpio" | sudo tee -a /opt/claw/.env >/dev/null
fi
sudo chown -R claw:claw /opt/claw

# --- Python venv ---
echo "[5/8] Creating Python virtual environment..."
sudo -u claw python3 -m venv --system-site-packages /opt/claw/venv
sudo -u claw /opt/claw/venv/bin/pip install --upgrade pip
sudo -u claw /opt/claw/venv/bin/pip install -r /opt/claw/requirements.txt

# --- Verify gpiozero ---
echo "[6/8] Verifying gpiozero..."
sudo -u claw /opt/claw/venv/bin/python -c "
from gpiozero import OutputDevice
d = OutputDevice(17)
d.on(); d.off(); d.close()
print('gpiozero OK - pin 17 tested')
"

# --- Deploy configs ---
echo "[7/8] Installing service files..."
sudo cp "$SCRIPT_DIR/deploy/mediamtx.yml" /etc/mediamtx.yml
sudo cp "$SCRIPT_DIR/deploy/systemd/mediamtx.service" /etc/systemd/system/
sudo cp "$SCRIPT_DIR/deploy/systemd/claw-server.service" /etc/systemd/system/
sudo cp "$SCRIPT_DIR/deploy/systemd/claw-watchdog.service" /etc/systemd/system/
sudo cp "$SCRIPT_DIR/deploy/nginx/claw.conf" /etc/nginx/sites-available/claw

sudo systemctl daemon-reload

# --- Enable services ---
echo "[8/8] Enabling services..."
sudo systemctl enable mediamtx
sudo systemctl enable claw-server
sudo systemctl enable claw-watchdog

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit /opt/claw/.env with your settings (especially ADMIN_API_KEY)"
echo "  2. Test camera:  rpicam-still -o /tmp/test.jpg"
echo "  3. Start services:"
echo "     sudo systemctl start mediamtx"
echo "     sudo systemctl start claw-server"
echo "     sudo systemctl start claw-watchdog"
echo "  4. Check stream:  http://<pi-ip>:8889/cam"
echo "  5. Check game:    http://<pi-ip>:8000"
echo "  6. For production with TLS, configure nginx and certbot"
echo ""
