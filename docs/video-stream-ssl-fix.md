# Fixing the Video Stream Black Screen (SSL / Reverse Proxy)

After setting up the TLS reverse proxy on the Grafana VM, the claw site loads
and works — except the video stream shows a **black screen**. This guide walks
through deploying the fix.

## What went wrong

Two things break the video when you put the claw behind an SSL reverse proxy:

1. **WHEP signaling can't reach MediaMTX.** The Grafana VM's nginx was
   configured to proxy `/stream/` directly to MediaMTX on `PI_LAN_IP:8889`.
   But MediaMTX binds to `127.0.0.1:8889` (localhost only), so the connection
   is refused.

2. **WebRTC ICE candidates are unreachable.** MediaMTX only advertised local
   IPs (`127.0.0.1`, LAN IP) in ICE candidates. Browsers on the internet can't
   reach those addresses, so the WebRTC data channel never connects.

## Prerequisites

- The Grafana VM TLS proxy is already set up (per `docs/brownrice-ssl-proxy.md`)
- SSH access to both the **Grafana VM** and the **Raspberry Pi**
- The updated config files from this repo (branch `claude/fix-video-stream-ssl-ikqMM` or merged to main)

## Step 1: Deploy the fixed nginx config (Grafana VM)

The key change: `/stream/` now proxies to FastAPI on port **8000** (which has a
built-in MediaMTX proxy) instead of directly to MediaMTX on port 8889.

**SSH into the Grafana VM** and run:

```bash
# Back up the current config
sudo cp /etc/nginx/sites-available/claw.thecastlefuncenter.com \
        /etc/nginx/sites-available/claw.thecastlefuncenter.com.bak

# Edit the config
sudo nano /etc/nginx/sites-available/claw.thecastlefuncenter.com
```

Find the `/stream/` location block and replace it with:

```nginx
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
```

> **Important:** Replace `PI_LAN_IP` with your Pi's actual LAN IP (e.g.
> `192.168.1.50`). Note there is **no trailing `/`** after `:8000` — this
> preserves the full URI so `/stream/cam/whep` reaches FastAPI correctly.

Alternatively, you can copy the full config from the repo:

```bash
# From your local machine (where you cloned the repo):
scp deploy/nginx/claw-proxy.conf USER@GRAFANA_VM_IP:/tmp/claw-proxy.conf

# On the Grafana VM:
sudo cp /tmp/claw-proxy.conf /etc/nginx/sites-available/claw.thecastlefuncenter.com
# Then edit the file to replace PI_LAN_IP with the actual Pi LAN IP
sudo sed -i 's/PI_LAN_IP/192.168.1.50/g' /etc/nginx/sites-available/claw.thecastlefuncenter.com
```

Test and reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## Step 2: Deploy the updated MediaMTX config (Raspberry Pi)

The updated MediaMTX config adds ICE NAT traversal so internet browsers can
establish WebRTC connections.

**SSH into the Raspberry Pi** and run:

```bash
# Back up current config
sudo cp /etc/mediamtx.yml /etc/mediamtx.yml.bak

# Detect your camera type first:
rpicam-hello --list-cameras 2>&1 | head -3   # RPi camera?
ls /dev/video*                                 # USB camera?

# Copy the correct config:
# For RPi Camera module:
sudo cp ~/ECLAW/deploy/mediamtx.yml /etc/mediamtx.yml

# For USB camera (most common — use this if unsure):
sudo cp ~/ECLAW/deploy/mediamtx-usb.yml /etc/mediamtx.yml
```

Or if you prefer to edit manually, add these lines to `/etc/mediamtx.yml`:

```yaml
# ICE connectivity for internet access through a reverse proxy.
#
# webrtcAdditionalHosts tells MediaMTX to include the public IP/domain
# in ICE host candidates so remote browsers can reach the WebRTC UDP
# endpoint.  Without this, ICE candidates only contain 127.0.0.1 and
# LAN IPs which are unreachable from the internet → black video.
#
# Set this to your public domain or IP address:
webrtcAdditionalHosts:
  - claw.thecastlefuncenter.com

# Pin all WebRTC UDP traffic to a single port instead of random
# ephemeral ports.  This allows a single port-forward rule on your
# router (UDP 8189 → Pi LAN IP) instead of requiring a full-cone NAT.
webrtcICEUDPMuxAddress: :8189
```

Restart MediaMTX:

```bash
sudo systemctl restart mediamtx
```

Verify it's running and the camera is detected:

```bash
sudo systemctl status mediamtx
# Should show "active (running)"

# Check for camera errors:
sudo journalctl -u mediamtx --no-pager -n 20
# If you see "camera_create(): selected camera is not available" you
# deployed the WRONG config (RPi camera config but USB camera connected,
# or vice versa). Switch to the correct one and restart.
```

## Step 3: Forward UDP port on the router

WebRTC streams video over UDP. The browser needs to reach the Pi's MediaMTX
UDP port through your router.

**On your router's admin page**, add a port forwarding rule:

| Protocol | External Port | Internal IP | Internal Port |
|----------|---------------|-------------|---------------|
| UDP      | 8189          | Pi LAN IP   | 8189          |

Replace "Pi LAN IP" with your Pi's actual LAN address (e.g. `192.168.1.50`).

## Step 4: Open the firewall on the Pi

```bash
sudo ufw allow 8189/udp
sudo ufw status
# Should show 8189/udp ALLOW Anywhere
```

## Step 5: Verify it works

### Test WHEP signaling (from any machine)

```bash
curl -v -X POST https://claw.thecastlefuncenter.com/stream/cam/whep \
  -H "Content-Type: application/sdp" \
  -d "v=0"
```

**Expected responses:**
- **201** — Working! WHEP session created successfully.
- **405** — MediaMTX has no active camera source. Wrong mediamtx config
  for your camera type, or camera not connected. Check `sudo journalctl -u mediamtx`.
- **502** — FastAPI can't reach MediaMTX. Is mediamtx running?
- **Connection refused / timeout** — nginx can't reach FastAPI. Check Pi IP and port.

### Test in the browser

1. Open `https://claw.thecastlefuncenter.com` in Chrome or Firefox
2. The video stream should appear (not a black screen)
3. Add `?debug` to the URL to see the stream status overlay with connection details

### Test from LAN (should still work)

```bash
# Direct Pi access should still work for LAN users
curl http://PI_LAN_IP:8000/stream/cam/whep \
  -X POST -H "Content-Type: application/sdp" -d "v=0"
```

## Troubleshooting

### Still getting a black screen?

**Check the proxy chain step by step:**

```bash
# 1. Is MediaMTX running on the Pi?
ssh PI "sudo systemctl status mediamtx"

# 2. Can FastAPI reach MediaMTX? (run on the Pi)
ssh PI "curl -X POST http://127.0.0.1:8889/cam/whep -H 'Content-Type: application/sdp' -d 'v=0'"

# 3. Can FastAPI's stream proxy handle the request? (run on the Pi)
ssh PI "curl -X POST http://127.0.0.1:8000/stream/cam/whep -H 'Content-Type: application/sdp' -d 'v=0'"

# 4. Can the Grafana VM reach FastAPI? (run on the Grafana VM)
ssh GRAFANA "curl -X POST http://PI_LAN_IP:8000/stream/cam/whep -H 'Content-Type: application/sdp' -d 'v=0'"

# 5. Does the public URL work?
curl -X POST https://claw.thecastlefuncenter.com/stream/cam/whep -H "Content-Type: application/sdp" -d "v=0"
```

Whichever step fails is where the problem is.

### WHEP works but video is still black?

The signaling (HTTP) works but WebRTC (UDP) is blocked. Check:

- **Router port forwarding**: Is UDP 8189 forwarded to the Pi?
- **Firewall**: Is `ufw` allowing UDP 8189? (`sudo ufw status`)
- **MediaMTX config**: Does `webrtcAdditionalHosts` contain the public domain?
- **MediaMTX config**: Is `webrtcICEUDPMuxAddress` set to `:8189`?
- **ISP/NAT issues**: Some ISPs block inbound UDP. Try from a different network.

### Browser console errors?

Open DevTools (F12) → Console tab. Look for:
- `ICE connection state: failed` → UDP path is blocked (router/firewall)
- `WHEP POST failed` or `502` → Proxy chain broken (check steps above)
- `net::ERR_CONNECTION_REFUSED` → nginx not proxying `/stream/` correctly

## Architecture diagram

```
Internet Browser
    │
    │ HTTPS (port 443)
    ▼
Grafana VM (nginx - TLS termination)
    │
    │ HTTP (port 8000, LAN)
    ▼
Raspberry Pi (FastAPI)
    │
    │ HTTP (localhost:8889)
    ▼
MediaMTX (WHEP signaling)
    │
    │ WebRTC (UDP 8189, via router port forward)
    ▼
Internet Browser (direct UDP to Pi)
```

The HTTP signaling goes through the full proxy chain. Once the WebRTC
connection is negotiated, video data flows directly over UDP between the
browser and MediaMTX on the Pi.
