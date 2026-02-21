# Brownrice TLS Front Door for Raspberry Pi Claw Server

This guide sets up `castlefuncenter.com` (hosted on the Brownrice server) as the TLS front door for your Pi-hosted claw app, so users stop seeing SSL warnings.

It is designed for this common situation:

- Raspberry Pi app is currently reachable at `http://66.109.44.77:8000`
- Port `8000` must stay in use for Grafana / existing services
- You want a proper HTTPS URL under your main domain

## Recommended topology

```text
Internet user
    -> https://claw.castlefuncenter.com  (valid Let's Encrypt cert)
    -> Brownrice nginx (443)
    -> reverse proxy to Pi app (LAN IP preferred, public IP fallback)
    -> Pi FastAPI app on :8000 (HTTP only, no direct internet exposure)
```

## One-command wizard (run on Pi)

If you prefer prompts over manual edits, run:

```bash
./scripts/setup_brownrice_tls_proxy.sh
```

The script can update Pi `.env`, apply UFW rules, generate Brownrice nginx config, and optionally SSH into Brownrice to install nginx and run certbot.

## 1) DNS: create a subdomain for the claw app

Create one DNS record:

- `claw.castlefuncenter.com` -> **A** record -> Brownrice server public IP

Do **not** point this DNS entry to the Pi directly if Brownrice is terminating TLS.

## 2) Brownrice nginx: terminate TLS and proxy to Pi

On Brownrice, create `/etc/nginx/sites-available/claw.castlefuncenter.com`:

```nginx
server {
    listen 80;
    server_name claw.castlefuncenter.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name claw.castlefuncenter.com;

    ssl_certificate /etc/letsencrypt/live/claw.castlefuncenter.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/claw.castlefuncenter.com/privkey.pem;

    # Basic hardening
    ssl_protocols TLSv1.2 TLSv1.3;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # Keep long-lived WebSocket sessions stable
    proxy_read_timeout 86400;
    proxy_send_timeout 86400;

    location / {
        # Prefer private connectivity if Brownrice can reach Pi over LAN/VPN
        proxy_pass http://PI_LAN_IP:8000;

        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        # WebSocket upgrade support
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

Then enable and reload:

```bash
sudo ln -s /etc/nginx/sites-available/claw.castlefuncenter.com /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## 3) Issue certificate on Brownrice

If certs are not already present:

```bash
sudo certbot --nginx -d claw.castlefuncenter.com
```

After this, `https://claw.castlefuncenter.com` should present a valid certificate with no browser warning.

## 4) Lock down the Pi so 8000 is not internet-exposed

Keep the app listening on `:8000` for proxying, but restrict inbound traffic to trusted sources (Brownrice IP and/or local network).

Example with UFW on Pi:

```bash
# Deny direct public access
sudo ufw deny 8000/tcp

# Allow Brownrice to proxy to Pi app
sudo ufw allow from <BROWNRICE_PUBLIC_OR_VPN_IP> to any port 8000 proto tcp
```

If Brownrice and Pi share a private network, allow only that private source range.

## 5) App config on Pi

Set origin/proxy settings in Pi `.env`:

```env
HOST=127.0.0.1
PORT=8000
CORS_ALLOWED_ORIGINS=https://claw.castlefuncenter.com
TRUSTED_PROXIES=127.0.0.1/32,::1/128,<BROWNRICE_PROXY_IP_OR_CIDR>
```

Then restart app services.

> If nginx runs on the same Pi as the app, keep only localhost proxy IPs. If Brownrice is the reverse proxy, include Brownrice's source IP/CIDR so forwarded client IPs are trusted.

## 6) Validation checklist

From any workstation:

```bash
curl -I https://claw.castlefuncenter.com
curl -I https://claw.castlefuncenter.com/api/health
```

Expect `HTTP/2 200` (or `301` then `200`) and a valid certificate chain.

## Why this removes SSL warnings

SSL warnings happen when users browse to a raw IP (`https://66.109.44.77:8000`) that does not match a certificate name, or when no trusted cert is installed. Using a domain-backed certificate on Brownrice fixes both:

- certificate common name/SAN matches `claw.castlefuncenter.com`
- browser trusts Let's Encrypt chain
- users access only HTTPS URL
