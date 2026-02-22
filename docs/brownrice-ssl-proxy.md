# TLS Front Door for Raspberry Pi Claw Server

This guide sets up `claw.thecastlefuncenter.com` as the HTTPS front door for
the Pi-hosted claw app so users stop seeing SSL warnings.

## Three-server architecture

| Server | Location | Role |
|---|---|---|
| **Brownrice** | Remote VPS | Hosts `thecastlefuncenter.com` main site; manages DNS |
| **Grafana VM** | Local Arch Linux VM on R640 | Owns ports 80/443 at public IP `66.109.44.77`; runs nginx as TLS proxy |
| **Raspberry Pi** | Local | Runs the claw app (FastAPI) on port 8000 |

The Grafana VM and Pi share the same public IP (`66.109.44.77`) via router
port forwarding. Brownrice is a completely separate server — it only manages
the DNS records.

## Network topology

```text
Brownrice VPS (separate server)
  └── DNS: claw.thecastlefuncenter.com  ->  A  ->  66.109.44.77

Router at 66.109.44.77 (public IP)
  ├── Port 80/443  ->  Grafana VM (Arch Linux, nginx)
  └── Port 8000    ->  Raspberry Pi

Internet user
    -> https://claw.thecastlefuncenter.com   (DNS resolves to 66.109.44.77)
    -> Router forwards :443 to Grafana VM
    -> Nginx sees SNI = "claw.thecastlefuncenter.com"
    -> Reverse proxy to Pi LAN IP:8000       (< 1ms LAN hop)
    -> Pi FastAPI app responds (plain HTTP)
```

> **Key insight:** Because the Pi and Grafana VM share a public IP, port 443
> is already taken by the Grafana VM's nginx. The solution is to add a second
> `server {}` block to that same nginx for `claw.thecastlefuncenter.com`,
> reverse-proxying traffic to the Pi over the LAN. Grafana traffic continues
> to route normally via its own `server_name`.

## Quick setup

### Automated wizard (run on the Grafana Arch VM)

```bash
# Copy setup_grafana_tls_proxy.sh to the Grafana VM, then:
./setup_grafana_tls_proxy.sh
```

The script installs nginx + certbot (if needed), deploys the claw proxy
config, requests a Let's Encrypt certificate, and prints Pi-side `.env`
changes to apply.

### Manual setup (step by step)

#### 1) DNS: create the subdomain (on Brownrice / your registrar)

Add one A record:

```
claw.thecastlefuncenter.com  ->  A  ->  66.109.44.77
```

This points at the same public IP the Grafana VM already uses. SNI-based
virtual hosting in nginx will route claw traffic separately from Grafana
traffic.

#### 2) Nginx on the Grafana VM: terminate TLS and proxy to Pi

On the Grafana Arch VM, create `/etc/nginx/sites-available/claw.thecastlefuncenter.com`:

```nginx
# Rate & connection limiting
limit_req_zone  $binary_remote_addr zone=claw_api:10m   rate=10r/s;
limit_req_zone  $binary_remote_addr zone=claw_join:10m  rate=3r/m;
limit_conn_zone $binary_remote_addr zone=claw_perip:10m;

server {
    listen 80;
    server_name claw.thecastlefuncenter.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name claw.thecastlefuncenter.com;
    server_tokens off;
    client_max_body_size 1m;

    limit_conn claw_perip 30;

    # Certs managed by certbot
    ssl_certificate /etc/letsencrypt/live/claw.thecastlefuncenter.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/claw.thecastlefuncenter.com/privkey.pem;

    # TLS hardening
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:claw_ssl:10m;
    ssl_session_timeout 1d;
    ssl_stapling on;
    ssl_stapling_verify on;

    # Security headers
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    add_header Content-Security-Policy "default-src 'self'; connect-src 'self' wss: ws:; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;

    # WebSocket timeout (24h for persistent connections)
    proxy_read_timeout 86400;
    proxy_send_timeout 86400;

    # -- Embed pages: allow framing from external sites --
    # Must appear before the catch-all / location so nginx matches it first.
    location /embed/ {
        proxy_pass http://PI_LAN_IP:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        # Override server-level headers: allow framing via CSP frame-ancestors,
        # and permit Google Fonts used by the embed pages.
        add_header Content-Security-Policy "default-src 'self'; connect-src 'self' wss: ws:; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; script-src 'self'; frame-ancestors *" always;
        add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
        add_header X-Content-Type-Options nosniff always;
        add_header Referrer-Policy strict-origin-when-cross-origin always;
        add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
    }

    # -- App: API + static frontend --
    location / {
        proxy_pass http://PI_LAN_IP:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # -- API rate limiting --
    location /api/ {
        limit_req zone=claw_api burst=20 nodelay;
        proxy_pass http://PI_LAN_IP:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    # -- Queue join: strict rate limit --
    location = /api/queue/join {
        limit_req zone=claw_join burst=2 nodelay;
        proxy_pass http://PI_LAN_IP:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    # -- WebSocket: status broadcast --
    location /ws/status {
        proxy_pass http://PI_LAN_IP:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_read_timeout 86400;
    }

    # -- WebSocket: player control --
    location /ws/control {
        proxy_pass http://PI_LAN_IP:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_read_timeout 86400;
    }

    # -- MediaMTX WebRTC (WHEP signaling) --
    # Route through FastAPI's built-in stream proxy (stream_proxy.py)
    # which forwards to MediaMTX on localhost:8889.  We cannot hit
    # MediaMTX directly because it binds to 127.0.0.1.
    location /stream/ {
        proxy_pass http://PI_LAN_IP:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 86400;
    }

    # -- Admin: LAN only --
    location /admin/ {
        allow 192.168.0.0/16;
        allow 10.0.0.0/8;
        allow 172.16.0.0/12;
        allow 127.0.0.0/8;
        deny all;
        proxy_pass http://PI_LAN_IP:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Replace `PI_LAN_IP` with the Pi's private IP (e.g. `192.168.1.x`).

On Arch Linux, nginx does not use `sites-available`/`sites-enabled` by
default. The setup script creates this directory structure automatically. To
do it manually:

```bash
sudo mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled

# Add to /etc/nginx/nginx.conf inside the http {} block (if not already there):
#   include /etc/nginx/sites-enabled/*;

sudo ln -s /etc/nginx/sites-available/claw.thecastlefuncenter.com \
           /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

#### 3) Issue certificate on the Grafana VM

```bash
sudo pacman -S --needed certbot certbot-nginx
sudo certbot --nginx -d claw.thecastlefuncenter.com
```

Certbot will modify the nginx config to fill in cert paths. After this,
`https://claw.thecastlefuncenter.com` should present a valid certificate.

> **Note:** For certbot to succeed, port 80 must be reachable from the
> internet and the DNS A record must already resolve to `66.109.44.77`.

#### 4) Lock down the Pi so port 8000 is not internet-exposed

The Pi app should only accept connections from the Grafana VM, not from the
open internet.

```bash
# On the Pi:
sudo ufw default deny incoming
sudo ufw allow ssh
# Allow Grafana VM LAN IP to reach the app
sudo ufw allow from GRAFANA_VM_LAN_IP to any port 8000 proto tcp
# Allow LAN access for admin panel
sudo ufw allow from 192.168.0.0/16 to any port 8000 proto tcp
sudo ufw enable
```

#### 5) App config on Pi

Edit the Pi's `.env` (typically `/opt/claw/.env`):

```env
HOST=0.0.0.0
PORT=8000
CORS_ALLOWED_ORIGINS=https://claw.thecastlefuncenter.com
TRUSTED_PROXIES=127.0.0.1/32,::1/128,GRAFANA_VM_LAN_IP/32
```

Then restart:

```bash
sudo systemctl restart claw-server
```

#### 6) Validation checklist

From any workstation with internet access:

```bash
# Should return HTTP/2 200 with valid cert
curl -I https://claw.thecastlefuncenter.com
curl -I https://claw.thecastlefuncenter.com/api/health

# Test WebSocket (requires wscat: npm install -g wscat)
wscat -c wss://claw.thecastlefuncenter.com/ws/status
```

## Why this removes SSL warnings

SSL warnings happen when users browse to a raw IP (`https://66.109.44.77:8000`)
that does not match a certificate name, or when no trusted cert is installed.
Using a domain-backed certificate on the Grafana VM fixes both:

- Certificate SAN matches `claw.thecastlefuncenter.com`
- Browser trusts the Let's Encrypt chain
- Users access only the HTTPS URL, never the raw IP

## Shared-IP architecture notes

Because the Pi and Grafana VM share public IP `66.109.44.77`:

- **Grafana** continues to serve on ports 80/443 for its own domain(s)
- **Claw app** gets its own `server_name` block on the same nginx, so SNI
  (Server Name Indication) routes `claw.thecastlefuncenter.com` traffic to
  the Pi while other domains go to Grafana
- **No port conflicts** — the Pi app stays on port 8000 (HTTP, LAN only)
- **One public IP, multiple HTTPS sites** — this is standard nginx virtual
  hosting and works with any number of additional subdomains
- **Brownrice stays separate** — Brownrice only manages DNS records; it does
  not proxy or serve claw traffic
