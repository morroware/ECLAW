#!/bin/bash
# ============================================================
# ECLAW — MediaMTX Diagnostic Script
# ============================================================
# Deep diagnostics for when MediaMTX fails to start or stream.
#
# Usage:
#   ./scripts/diagnose_mediamtx.sh
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0
INFO=0

pass() { echo -e "  ${GREEN}PASS${NC}  $*"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}FAIL${NC}  $*"; FAIL=$((FAIL + 1)); }
warn() { echo -e "  ${YELLOW}WARN${NC}  $*"; WARN=$((WARN + 1)); }
info() { echo -e "  ${BLUE}INFO${NC}  $*"; INFO=$((INFO + 1)); }
hint() { echo -e "        ${BLUE}->  $*${NC}"; }

echo ""
echo -e "${BOLD}========================================${NC}"
echo -e "${BOLD}  ECLAW — MediaMTX Diagnostics${NC}"
echo -e "${BOLD}========================================${NC}"
echo ""

# ---- 1. Binary --------------------------------------------------------
echo -e "${BOLD}1. MediaMTX Binary${NC}"

if [ -f /usr/local/bin/mediamtx ]; then
    pass "Binary exists: /usr/local/bin/mediamtx"
    if [ -x /usr/local/bin/mediamtx ]; then
        pass "Binary is executable"
    else
        fail "Binary is NOT executable"
        hint "Fix: sudo chmod +x /usr/local/bin/mediamtx"
    fi
    # Check version
    MTX_VER=$(/usr/local/bin/mediamtx --version 2>/dev/null || echo "unknown")
    info "MediaMTX version: $MTX_VER"
else
    fail "Binary not found at /usr/local/bin/mediamtx"
    hint "Fix: run ./scripts/setup_pi.sh to install, or manually download from:"
    hint "     https://github.com/bluenviron/mediamtx/releases"
fi

# ---- 2. Configuration -------------------------------------------------
echo ""
echo -e "${BOLD}2. Configuration${NC}"

if [ -f /etc/mediamtx.yml ]; then
    pass "Config exists: /etc/mediamtx.yml"

    # Detect what config type is deployed
    if grep -q "source: rpiCamera" /etc/mediamtx.yml 2>/dev/null; then
        CONFIG_TYPE="picam"
        info "Config type: Pi Camera (rpiCamera source)"
    elif grep -q "runOnInit:" /etc/mediamtx.yml 2>/dev/null; then
        CONFIG_TYPE="usb"
        info "Config type: USB Camera (ffmpeg/RTSP source)"
    else
        CONFIG_TYPE="unknown"
        warn "Config type: could not determine (custom config?)"
    fi

    # Validate YAML syntax (basic check)
    if command -v python3 &>/dev/null; then
        YAML_RESULT=$(python3 -c "
import sys
try:
    import yaml
except ImportError:
    print('skip')
    sys.exit(0)
try:
    with open('/etc/mediamtx.yml') as f:
        yaml.safe_load(f)
    print('ok')
except yaml.YAMLError as e:
    print('fail')
    print(str(e), file=sys.stderr)
" 2>/dev/null || echo "skip")

        case "$YAML_RESULT" in
            ok*)   pass "Config YAML syntax: valid" ;;
            skip*) info "YAML validation skipped (pyyaml not installed)" ;;
            fail*) warn "Config YAML syntax may have issues (MediaMTX may still accept it)"
                   hint "Install pyyaml for detailed validation: pip3 install pyyaml" ;;
        esac
    fi
else
    fail "Config not found at /etc/mediamtx.yml"
    hint "Fix: sudo cp deploy/mediamtx.yml /etc/mediamtx.yml  (Pi Camera)"
    hint "     sudo cp deploy/mediamtx-usb.yml /etc/mediamtx.yml  (USB Camera)"
fi

# ---- 3. Camera Hardware -----------------------------------------------
echo ""
echo -e "${BOLD}3. Camera Hardware${NC}"

HAS_PICAM=false
HAS_USB=false

# Check Pi Camera
if command -v rpicam-still &>/dev/null; then
    PICAM_OUTPUT=$(rpicam-still --list-cameras 2>&1 || true)
    if echo "$PICAM_OUTPUT" | grep -q "Available cameras"; then
        HAS_PICAM=true
        pass "Pi Camera detected"
        # Show camera info
        echo "$PICAM_OUTPUT" | grep -E "^\s+[0-9]" | while read -r line; do
            info "  $line"
        done
    else
        info "rpicam-still found but no Pi Camera detected"
    fi
else
    info "rpicam-still not installed (OK if not using Pi Camera)"
fi

# Check USB cameras
if ls /dev/video* &>/dev/null 2>&1; then
    HAS_USB=true
    pass "Video devices found"
    for dev in /dev/video*; do
        info "  $dev"
    done

    # Show device names if v4l2-ctl is available
    if command -v v4l2-ctl &>/dev/null; then
        info "Device details (v4l2-ctl):"
        v4l2-ctl --list-devices 2>/dev/null | while IFS= read -r line; do
            info "  $line"
        done
    else
        info "Install v4l-utils for detailed device info: sudo apt install v4l-utils"
    fi
else
    info "No /dev/video* devices found"
fi

if ! $HAS_PICAM && ! $HAS_USB; then
    fail "No camera hardware detected"
    hint "Connect a Pi Camera module or USB camera, then check again"
    hint "Pi Camera: Make sure it's enabled in raspi-config -> Interface Options"
fi

# ---- 4. Config vs Hardware Match ---------------------------------------
echo ""
echo -e "${BOLD}4. Config / Hardware Match${NC}"

if [ "${CONFIG_TYPE:-}" = "picam" ] && $HAS_PICAM; then
    pass "Config (Pi Camera) matches detected hardware"
elif [ "${CONFIG_TYPE:-}" = "usb" ] && $HAS_USB; then
    pass "Config (USB Camera) matches detected hardware"
elif [ "${CONFIG_TYPE:-}" = "picam" ] && ! $HAS_PICAM; then
    fail "MISMATCH: Config is set for Pi Camera, but NO Pi Camera detected"
    if $HAS_USB; then
        hint "Fix: sudo cp deploy/mediamtx-usb.yml /etc/mediamtx.yml"
        hint "     sudo systemctl restart mediamtx"
    else
        hint "Connect a Pi Camera module, or switch to USB camera config"
    fi
elif [ "${CONFIG_TYPE:-}" = "usb" ] && ! $HAS_USB; then
    fail "MISMATCH: Config is set for USB Camera, but NO /dev/video* devices found"
    if $HAS_PICAM; then
        hint "Fix: sudo cp deploy/mediamtx.yml /etc/mediamtx.yml"
        hint "     sudo systemctl restart mediamtx"
    else
        hint "Connect a USB camera, or switch to Pi Camera config"
    fi
else
    warn "Could not verify config/hardware match"
fi

# If USB config, check that ffmpeg is available
if [ "${CONFIG_TYPE:-}" = "usb" ]; then
    if command -v ffmpeg &>/dev/null; then
        pass "ffmpeg is installed (required for USB camera)"
    else
        fail "ffmpeg is NOT installed (required for USB camera config)"
        hint "Fix: sudo apt install ffmpeg"
    fi

    # Check the specific video device referenced in the config
    USB_DEV=$(grep -oP '/dev/video\d+' /etc/mediamtx.yml 2>/dev/null | head -1 || true)
    if [ -n "$USB_DEV" ]; then
        if [ -e "$USB_DEV" ]; then
            pass "Configured device $USB_DEV exists"
        else
            fail "Configured device $USB_DEV does NOT exist"
            hint "Available devices:"
            ls /dev/video* 2>/dev/null | while read -r d; do hint "  $d"; done
            hint "Edit /etc/mediamtx.yml and update the device path"
        fi
    fi
fi

# ---- 5. System User and Permissions -----------------------------------
echo ""
echo -e "${BOLD}5. User and Permissions${NC}"

if id mediamtx &>/dev/null; then
    pass "System user 'mediamtx' exists"
    if id -nG mediamtx 2>/dev/null | grep -qw video; then
        pass "User 'mediamtx' is in the 'video' group"
    else
        fail "User 'mediamtx' is NOT in the 'video' group"
        hint "Fix: sudo usermod -a -G video mediamtx && sudo systemctl restart mediamtx"
    fi
else
    fail "System user 'mediamtx' does not exist"
    hint "Fix: sudo useradd -r -s /usr/sbin/nologin -m -d /opt/mediamtx mediamtx"
    hint "     sudo usermod -a -G video mediamtx"
fi

# Check config file readability by mediamtx user
if [ -f /etc/mediamtx.yml ]; then
    if sudo -u mediamtx test -r /etc/mediamtx.yml 2>/dev/null; then
        pass "Config is readable by mediamtx user"
    else
        warn "Cannot verify config readability (sudo required)"
    fi
fi

# ---- 6. Ports ---------------------------------------------------------
echo ""
echo -e "${BOLD}6. Port Availability${NC}"

check_port() {
    local port=$1
    local name=$2

    # Use sudo for ss/netstat so we can see process names for all users
    if command -v ss &>/dev/null; then
        LISTENER=$(sudo ss -tlnp "sport = :$port" 2>/dev/null | grep -v "State" || true)
    elif command -v netstat &>/dev/null; then
        LISTENER=$(sudo netstat -tlnp 2>/dev/null | grep ":$port " || true)
    else
        info "Neither ss nor netstat available, skipping port check for $port"
        return
    fi

    if [ -z "$LISTENER" ]; then
        if systemctl is-active --quiet mediamtx 2>/dev/null; then
            fail "Port $port ($name) is NOT in use but mediamtx should be listening"
        else
            info "Port $port ($name) is free (expected since mediamtx is not running)"
        fi
    else
        if echo "$LISTENER" | grep -q mediamtx; then
            pass "Port $port ($name) is in use by mediamtx"
        elif systemctl is-active --quiet mediamtx 2>/dev/null; then
            # Service is running and port is in use — likely mediamtx but
            # process name not visible (can happen with older ss versions)
            pass "Port $port ($name) is in use (mediamtx service is active)"
        else
            fail "Port $port ($name) is in use by another process"
            info "  $LISTENER"
            hint "Kill the conflicting process or change the port in /etc/mediamtx.yml"
        fi
    fi
}

check_port 8889 "WebRTC/WHEP"
check_port 8554 "RTSP"

# ---- 7. Systemd Service -----------------------------------------------
echo ""
echo -e "${BOLD}7. Systemd Service${NC}"

if ! command -v systemctl &>/dev/null; then
    info "systemctl not available (not running under systemd)"
    info "This is expected in dev/container environments"
else
    if [ -f /etc/systemd/system/mediamtx.service ]; then
        pass "Service file installed"
    else
        fail "Service file missing: /etc/systemd/system/mediamtx.service"
        hint "Fix: sudo cp deploy/systemd/mediamtx.service /etc/systemd/system/"
        hint "     sudo systemctl daemon-reload"
    fi

    if systemctl is-enabled --quiet mediamtx 2>/dev/null; then
        pass "Service is enabled (starts on boot)"
    else
        warn "Service is NOT enabled"
        hint "Fix: sudo systemctl enable mediamtx"
    fi

    if systemctl is-active --quiet mediamtx 2>/dev/null; then
        pass "Service is running"
    else
        fail "Service is NOT running"

        # Show recent logs for diagnosis
        echo ""
        echo -e "  ${BOLD}Recent mediamtx logs:${NC}"
        journalctl -u mediamtx --no-pager -n 15 --no-hostname 2>/dev/null | while IFS= read -r line; do
            echo -e "  ${RED}|${NC} $line"
        done

        echo ""
        # Show exit status
        EXIT_INFO=$(systemctl show mediamtx --property=ExecMainStatus,ExecMainCode,ActiveState,SubState 2>/dev/null || true)
        if [ -n "$EXIT_INFO" ]; then
            echo "$EXIT_INFO" | while IFS= read -r line; do
                info "$line"
            done
        fi

        hint "Try starting manually to see errors:"
        hint "  sudo /usr/local/bin/mediamtx /etc/mediamtx.yml"
        hint "Or restart the service:"
        hint "  sudo systemctl restart mediamtx"
        hint "  sudo journalctl -u mediamtx -f  (watch logs)"
    fi
fi

# ---- 8. Network Health Check ------------------------------------------
echo ""
echo -e "${BOLD}8. Stream Endpoint Health${NC}"

HEALTH_URL="http://127.0.0.1:8889/v3/paths/list"

if curl -sf "$HEALTH_URL" -o /tmp/mtx_health.json --max-time 3 2>/dev/null; then
    pass "MediaMTX API responding at $HEALTH_URL"

    if command -v python3 &>/dev/null; then
        STREAM_INFO=$(python3 -c "
import json, sys
try:
    with open('/tmp/mtx_health.json') as f:
        data = json.load(f)
    # v3 API: {'items': [{'name': 'cam', 'ready': true, ...}]}
    # v2 API: {'paths': {'cam': {'sourceReady': true, ...}}}
    items = data.get('items') or []
    paths = data.get('paths') or {}
    if items:
        for item in items:
            name = item.get('name', 'unknown')
            ready = item.get('ready', item.get('sourceReady', False))
            src = item.get('source', None)
            src_type = src.get('type', 'unknown') if isinstance(src, dict) else str(src or 'unknown')
            status = 'ready' if ready else 'NOT ready'
            print(f'/{name}  status={status}  source={src_type}')
    elif paths:
        for name, pdata in paths.items():
            ready = pdata.get('sourceReady', pdata.get('ready', False))
            status = 'ready' if ready else 'NOT ready'
            print(f'/{name}  status={status}')
    else:
        print('(none)')
except Exception as e:
    print(f'(parse error: {e})', file=sys.stderr)
    sys.exit(1)
" 2>/dev/null)

        if [ -n "$STREAM_INFO" ] && [ "$STREAM_INFO" != "(none)" ]; then
            echo "$STREAM_INFO" | while IFS= read -r line; do
                info "Stream: $line"
            done
        else
            info "No active streams found (ffmpeg may still be starting)"
        fi
    fi

    # Quick WHEP availability check (GET returns 404 if path doesn't exist, 400 if it does)
    WHEP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
        "http://127.0.0.1:8889/cam/whep" -X POST \
        -H "Content-Type: application/sdp" \
        -d "v=0" --max-time 3 2>/dev/null || echo "000")

    if [ "$WHEP_STATUS" = "201" ]; then
        pass "WHEP endpoint responding for /cam"
    elif [ "$WHEP_STATUS" = "400" ] || [ "$WHEP_STATUS" = "401" ]; then
        # 400 = bad SDP (expected with our dummy payload), means endpoint exists
        pass "WHEP endpoint exists for /cam (HTTP $WHEP_STATUS)"
    elif [ "$WHEP_STATUS" = "404" ]; then
        warn "WHEP path /cam not found (stream may not be published yet)"
        hint "Check: sudo journalctl -u mediamtx -f"
    else
        warn "WHEP endpoint returned HTTP $WHEP_STATUS (stream may not be ready)"
    fi
else
    fail "MediaMTX API not responding at $HEALTH_URL"
    hint "MediaMTX is either not running or not listening on port 8889"
fi

# ---- Summary -----------------------------------------------------------
echo ""
echo -e "${BOLD}========================================${NC}"
echo -e "  ${GREEN}PASS: $PASS${NC}  ${RED}FAIL: $FAIL${NC}  ${YELLOW}WARN: $WARN${NC}  ${BLUE}INFO: $INFO${NC}"
echo -e "${BOLD}========================================${NC}"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}Issues found.${NC} Fix the FAIL items above, then:"
    echo "  sudo systemctl restart mediamtx"
    echo "  ./scripts/diagnose_mediamtx.sh    # re-run diagnostics"
    echo ""
    exit 1
elif [ "$WARN" -gt 0 ]; then
    echo -e "${YELLOW}Warnings found.${NC} MediaMTX may still work, but review the WARN items above."
    echo ""
else
    echo -e "${GREEN}All checks passed!${NC} MediaMTX should be working correctly."
    echo ""
fi
