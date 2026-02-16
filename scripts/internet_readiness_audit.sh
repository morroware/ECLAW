#!/bin/bash
# Internet readiness audit for multi-day public demos.
#
# Usage:
#   ./scripts/internet_readiness_audit.sh
#   ./scripts/internet_readiness_audit.sh --env /opt/claw/.env --nginx /etc/nginx/sites-enabled/claw.conf

set -euo pipefail

ENV_FILE=".env"
NGINX_CONF="deploy/nginx/claw.conf"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      ENV_FILE="$2"; shift 2 ;;
    --nginx)
      NGINX_CONF="$2"; shift 2 ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2 ;;
  esac
done

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

get_env() {
  local key="$1"
  if [[ ! -f "$ENV_FILE" ]]; then
    return 1
  fi
  awk -F= -v k="$key" '$1==k{print substr($0, index($0,$2)); exit}' "$ENV_FILE"
}

echo ""
echo -e "${BOLD}ECLAW Internet Readiness Audit${NC}"
echo "env:   $ENV_FILE"
echo "nginx: $NGINX_CONF"
echo ""

echo -e "${BOLD}1. Environment safety${NC}"
if [[ -f "$ENV_FILE" ]]; then
  pass "Environment file exists"
else
  fail "Environment file not found ($ENV_FILE)"
fi

admin_key="$(get_env ADMIN_API_KEY || true)"
if [[ -z "$admin_key" || "$admin_key" == "changeme" || "$admin_key" == "demo-admin-key" ]]; then
  fail "ADMIN_API_KEY is default/weak"
else
  pass "ADMIN_API_KEY is non-default"
fi

mock_gpio="$(get_env MOCK_GPIO || true)"
if [[ "$mock_gpio" == "false" ]]; then
  pass "MOCK_GPIO=false (hardware mode)"
else
  warn "MOCK_GPIO is not false (acceptable for dry-runs, not for real claw hardware)"
fi

cors_origins="$(get_env CORS_ALLOWED_ORIGINS || true)"
if [[ -z "$cors_origins" ]]; then
  warn "CORS_ALLOWED_ORIGINS not set (application default is localhost only)"
elif [[ "$cors_origins" == *"*"* ]]; then
  fail "CORS_ALLOWED_ORIGINS contains wildcard (*)"
else
  pass "CORS_ALLOWED_ORIGINS is explicit: $cors_origins"
fi

host="$(get_env HOST || true)"
if [[ "$host" == "127.0.0.1" || "$host" == "localhost" || -z "$host" ]]; then
  pass "HOST is loopback-only (expected when fronted by nginx)"
else
  warn "HOST is $host (ensure firewall and reverse-proxy rules are strict)"
fi

echo ""
echo -e "${BOLD}2. Nginx hardening${NC}"
if [[ -f "$NGINX_CONF" ]]; then
  pass "Nginx config present"
else
  fail "Nginx config missing ($NGINX_CONF)"
fi

if [[ -f "$NGINX_CONF" ]]; then
  if rg -q 'listen 443 ssl' "$NGINX_CONF"; then
    pass "TLS listener configured"
  else
    fail "No TLS listener (listen 443 ssl)"
  fi

  if rg -q 'return 301 https://' "$NGINX_CONF"; then
    pass "HTTP->HTTPS redirect configured"
  else
    warn "No explicit HTTP->HTTPS redirect found"
  fi

  if rg -q 'location /admin/' "$NGINX_CONF" && rg -q 'deny all;' "$NGINX_CONF"; then
    pass "Admin route appears IP-restricted"
  else
    warn "Admin route restrictions not detected"
  fi

  if rg -q 'limit_req zone=api' "$NGINX_CONF" && rg -q 'limit_req zone=join' "$NGINX_CONF"; then
    pass "Rate limiting for API and queue join detected"
  else
    warn "Rate limiting directives not fully detected"
  fi
fi

echo ""
echo "========================================"
echo -e "  ${GREEN}PASS: $PASS${NC}  ${RED}FAIL: $FAIL${NC}  ${YELLOW}WARN: $WARN${NC}"
echo "========================================"
echo ""

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
