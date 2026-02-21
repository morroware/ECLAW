#!/bin/bash
# Configure Brownrice TLS reverse proxy + Pi app settings for Remote Claw.
# Run on the Raspberry Pi host.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

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

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

prompt() {
    local var_name="$1"
    local label="$2"
    local default="${3:-}"
    local value

    if [ -n "$default" ]; then
        read -r -p "$label [$default]: " value
        value="${value:-$default}"
    else
        read -r -p "$label: " value
    fi

    printf -v "$var_name" '%s' "$value"
}

prompt_yes_no() {
    local var_name="$1"
    local label="$2"
    local default="${3:-N}"
    local answer

    read -r -p "$label [y/N]: " answer
    answer="${answer:-$default}"
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        printf -v "$var_name" '%s' "yes"
    else
        printf -v "$var_name" '%s' "no"
    fi
}

upsert_env() {
    local file="$1"
    local key="$2"
    local value="$3"

    if grep -Eq "^${key}=" "$file"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$file"
    else
        echo "${key}=${value}" >> "$file"
    fi
}

if [ "$(id -u)" -eq 0 ]; then
    die "Run as a normal user (not root). The script will use sudo when needed."
fi

for required_file in .env.example app/main.py deploy/nginx/claw.conf; do
    [ -f "$ROOT_DIR/$required_file" ] || die "Run from a full repository clone. Missing: $required_file"
done

require_cmd python3
require_cmd sudo

cat <<'BANNER'

===============================================
 Remote Claw - Brownrice TLS Proxy Setup Wizard
===============================================

This script helps you:
  1) Configure Pi app env for domain + trusted proxy
  2) Optionally lock down Pi firewall (UFW)
  3) Generate (or remotely install) Brownrice nginx config
  4) Optionally request Let's Encrypt cert on Brownrice

BANNER

prompt APP_DOMAIN "Domain to expose publicly (e.g. claw.castlefuncenter.com)"
[ -n "$APP_DOMAIN" ] || die "Domain is required"

prompt PI_APP_PORT "Pi app port" "8000"
prompt PI_ENV_FILE "Pi env file path" "/opt/claw/.env"
prompt BROWNRICE_PROXY_SOURCE "Brownrice source IP or CIDR for TRUSTED_PROXIES/allowlist"

prompt_yes_no SET_HOST_LOCAL "Set HOST=127.0.0.1 in Pi env (recommended if local nginx is used)" "N"
prompt_yes_no CONFIGURE_UFW "Configure UFW rules on Pi for app port" "Y"
prompt_yes_no PROVISION_BROWNRICE "SSH into Brownrice and install nginx config automatically" "N"

[ -f "$PI_ENV_FILE" ] || die "Env file not found: $PI_ENV_FILE"

info "Updating Pi app env: $PI_ENV_FILE"
sudo cp "$PI_ENV_FILE" "${PI_ENV_FILE}.bak.$(date +%Y%m%d%H%M%S)"
CURRENT_TRUSTED="$(grep -E '^TRUSTED_PROXIES=' "$PI_ENV_FILE" | cut -d= -f2- || true)"
BASE_TRUSTED="127.0.0.1/32,::1/128"
if [ -n "$CURRENT_TRUSTED" ]; then
    BASE_TRUSTED="$CURRENT_TRUSTED"
fi
if [[ ",$BASE_TRUSTED," != *",$BROWNRICE_PROXY_SOURCE,"* ]]; then
    BASE_TRUSTED="$BASE_TRUSTED,$BROWNRICE_PROXY_SOURCE"
fi

upsert_env "$PI_ENV_FILE" "CORS_ALLOWED_ORIGINS" "https://$APP_DOMAIN"
upsert_env "$PI_ENV_FILE" "TRUSTED_PROXIES" "$BASE_TRUSTED"
if [ "$SET_HOST_LOCAL" = "yes" ]; then
    upsert_env "$PI_ENV_FILE" "HOST" "127.0.0.1"
fi
upsert_env "$PI_ENV_FILE" "PORT" "$PI_APP_PORT"
ok "Updated Pi env values"

if [ "$CONFIGURE_UFW" = "yes" ]; then
    require_cmd ufw
    info "Applying UFW rules for port $PI_APP_PORT"
    sudo ufw deny "$PI_APP_PORT/tcp" >/dev/null || true
    sudo ufw allow from "$BROWNRICE_PROXY_SOURCE" to any port "$PI_APP_PORT" proto tcp >/dev/null
    ok "UFW rules updated"
fi

GENERATED_CONF="$ROOT_DIR/deploy/nginx/${APP_DOMAIN}.brownrice.conf"
cat > "$GENERATED_CONF" <<NGINX
server {
    listen 80;
    server_name $APP_DOMAIN;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name $APP_DOMAIN;

    ssl_certificate /etc/letsencrypt/live/$APP_DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$APP_DOMAIN/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    proxy_read_timeout 86400;
    proxy_send_timeout 86400;

    location / {
        proxy_pass http://REPLACE_WITH_PI_REACHABLE_IP:$PI_APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
NGINX
ok "Generated Brownrice nginx template: $GENERATED_CONF"

if [ "$PROVISION_BROWNRICE" = "yes" ]; then
    require_cmd ssh
    require_cmd scp

    prompt BROWNRICE_SSH_USER "Brownrice SSH user"
    prompt BROWNRICE_SSH_HOST "Brownrice SSH host/IP"
    prompt PI_REACHABLE_IP "Pi IP reachable from Brownrice (LAN/VPN preferred)"
    prompt_yes_no RUN_CERTBOT "Run certbot --nginx on Brownrice now" "Y"

    TMP_CONF="/tmp/${APP_DOMAIN}.conf"
    sed "s|REPLACE_WITH_PI_REACHABLE_IP|$PI_REACHABLE_IP|g" "$GENERATED_CONF" > /tmp/brownrice_conf_final.conf

    info "Copying nginx config to Brownrice"
    scp /tmp/brownrice_conf_final.conf "${BROWNRICE_SSH_USER}@${BROWNRICE_SSH_HOST}:${TMP_CONF}"

    info "Installing nginx site on Brownrice"
    ssh "${BROWNRICE_SSH_USER}@${BROWNRICE_SSH_HOST}" "sudo mv ${TMP_CONF} /etc/nginx/sites-available/${APP_DOMAIN} && sudo ln -sf /etc/nginx/sites-available/${APP_DOMAIN} /etc/nginx/sites-enabled/${APP_DOMAIN} && sudo nginx -t && sudo systemctl reload nginx"

    if [ "$RUN_CERTBOT" = "yes" ]; then
        info "Requesting certificate on Brownrice"
        ssh "${BROWNRICE_SSH_USER}@${BROWNRICE_SSH_HOST}" "sudo certbot --nginx -d ${APP_DOMAIN}"
    else
        warn "Skipped certbot. Run manually on Brownrice: sudo certbot --nginx -d ${APP_DOMAIN}"
    fi

    rm -f /tmp/brownrice_conf_final.conf
    ok "Brownrice remote provisioning completed"
else
    warn "Auto-provisioning skipped. Install this config on Brownrice manually: $GENERATED_CONF"
fi

cat <<EOF_SUMMARY

${BOLD}Done.${NC}
Next checks:
  - Ensure DNS A record points ${APP_DOMAIN} -> Brownrice public IP
  - Restart Pi app service after env change (example: sudo systemctl restart claw-server)
  - Test:
      curl -I https://${APP_DOMAIN}
      curl -I https://${APP_DOMAIN}/api/health

If SSL warnings persist, users are likely still visiting the raw IP instead of https://${APP_DOMAIN}.
EOF_SUMMARY
