#!/bin/bash
# ECLAW Health Check — verifies all components are running correctly.
#
# Usage:
#   ./scripts/health_check.sh                          # Check localhost:8000
#   ./scripts/health_check.sh http://192.168.1.10:8000 # Check remote host
#
set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

pass() { echo -e "  ${GREEN}PASS${NC}  $*"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}FAIL${NC}  $*"; FAIL=$((FAIL + 1)); }
warn() { echo -e "  ${YELLOW}WARN${NC}  $*"; WARN=$((WARN + 1)); }

echo ""
echo -e "${BOLD}ECLAW Health Check${NC}"
echo -e "Target: $BASE_URL"
echo ""

# 1. Server reachable
echo -e "${BOLD}1. Server Connectivity${NC}"
if curl -sf "$BASE_URL/api/health" -o /tmp/eclaw_health.json --max-time 5 2>/dev/null; then
    pass "Server is reachable"
else
    fail "Server is not reachable at $BASE_URL"
    echo ""
    echo -e "  ${RED}Cannot continue — server must be running.${NC}"
    echo "  Start it with: make run"
    echo ""
    exit 1
fi

# 2. Parse health response
echo ""
echo -e "${BOLD}2. Health Endpoint${NC}"
if command -v python3 &>/dev/null; then
    HEALTH=$(python3 -c "
import json, sys
with open('/tmp/eclaw_health.json') as f:
    h = json.load(f)
print(f\"status={h.get('status','unknown')}\")
print(f\"gpio_locked={h.get('gpio_locked','unknown')}\")
print(f\"camera_ok={h.get('camera_ok','unknown')}\")
print(f\"queue_length={h.get('queue_length','unknown')}\")
print(f\"viewer_count={h.get('viewer_count','unknown')}\")
print(f\"uptime_seconds={h.get('uptime_seconds','unknown')}\")
")
    eval "$HEALTH"

    if [ "$status" = "ok" ]; then
        pass "Health status: ok"
    else
        fail "Health status: $status"
    fi

    if [ "$gpio_locked" = "False" ]; then
        pass "GPIO controls: unlocked"
    else
        warn "GPIO controls: LOCKED (emergency stop active?)"
    fi

    if [ "$camera_ok" = "True" ]; then
        pass "Camera stream: available"
    else
        warn "Camera stream: not available (MediaMTX may not be running)"
        echo -e "        ${YELLOW}->  Run ./scripts/diagnose_mediamtx.sh for detailed diagnostics${NC}"
    fi

    pass "Queue length: $queue_length"
    pass "Connected viewers: $viewer_count"

    # Format uptime
    if command -v python3 &>/dev/null; then
        UPTIME=$(python3 -c "
s = $uptime_seconds
h, r = divmod(int(s), 3600)
m, sec = divmod(r, 60)
print(f'{h}h {m}m {sec}s')
")
        pass "Uptime: $UPTIME"
    fi
else
    warn "python3 not found, cannot parse health response"
fi

# 3. API endpoints
echo ""
echo -e "${BOLD}3. API Endpoints${NC}"

# Queue status
if curl -sf "$BASE_URL/api/queue/status" -o /dev/null --max-time 3 2>/dev/null; then
    pass "GET /api/queue/status"
else
    fail "GET /api/queue/status"
fi

# API docs
if curl -sf "$BASE_URL/api/docs" -o /dev/null --max-time 3 2>/dev/null; then
    pass "GET /api/docs (Swagger UI)"
else
    warn "GET /api/docs (Swagger UI not available)"
fi

# 4. WebSocket endpoints
echo ""
echo -e "${BOLD}4. WebSocket Endpoints${NC}"
WS_URL=$(echo "$BASE_URL" | sed 's|http://|ws://|;s|https://|wss://|')

if command -v python3 &>/dev/null; then
    WS_OK=$(python3 -c "
import asyncio, sys
async def check():
    try:
        import websockets
        async with websockets.connect('${WS_URL}/ws/status', open_timeout=3) as ws:
            return True
    except ImportError:
        # Try without websockets library
        return None
    except Exception:
        return False
result = asyncio.run(check())
if result is True:
    print('ok')
elif result is None:
    print('skip')
else:
    print('fail')
" 2>/dev/null)

    if [ "$WS_OK" = "ok" ]; then
        pass "WebSocket /ws/status: connectable"
    elif [ "$WS_OK" = "skip" ]; then
        warn "WebSocket check skipped (websockets library not installed)"
    else
        fail "WebSocket /ws/status: connection failed"
    fi
else
    warn "Cannot check WebSocket (python3 not available)"
fi

# 5. Static files
echo ""
echo -e "${BOLD}5. Frontend${NC}"
if curl -sf "$BASE_URL/" -o /dev/null --max-time 3 2>/dev/null; then
    pass "Frontend index.html served"
else
    warn "Frontend not served (OK if using nginx in production)"
fi

# 6. Systemd services (if on Pi)
if command -v systemctl &>/dev/null; then
    echo ""
    echo -e "${BOLD}6. Systemd Services${NC}"
    for svc in claw-server claw-watchdog mediamtx; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            pass "$svc: active"
        elif systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            warn "$svc: enabled but not running"
            if [ "$svc" = "mediamtx" ]; then
                # Show last few log lines to help diagnose
                RECENT_LOG=$(journalctl -u mediamtx --no-pager -n 3 --no-hostname 2>/dev/null || true)
                if [ -n "$RECENT_LOG" ]; then
                    echo -e "        ${YELLOW}Recent logs:${NC}"
                    echo "$RECENT_LOG" | while IFS= read -r line; do
                        echo -e "        ${YELLOW}|${NC} $line"
                    done
                fi
                echo -e "        ${YELLOW}->  Run ./scripts/diagnose_mediamtx.sh for detailed diagnostics${NC}"
            fi
        else
            warn "$svc: not configured (OK for dev mode)"
        fi
    done
fi

# Summary
echo ""
echo "========================================"
echo -e "  ${GREEN}PASS: $PASS${NC}  ${RED}FAIL: $FAIL${NC}  ${YELLOW}WARN: $WARN${NC}"
echo "========================================"
echo ""

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
