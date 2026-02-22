# Embedding the Remote Claw Machine

Embed the live claw machine stream or full interactive player on your WordPress site (or any website).

## Quick Start — Raw iframe

No plugin needed. Paste this HTML into any page:

### Watch-Only (spectator stream)

```html
<iframe src="https://claw.yourdomain.com/embed/watch"
        width="100%" height="360" frameborder="0"
        allow="autoplay; encrypted-media" allowfullscreen
        style="border:0; border-radius:8px; max-width:100%;"
        loading="lazy" title="Remote Claw Machine"></iframe>
```

### Full Interactive (join queue + play)

```html
<iframe src="https://claw.yourdomain.com/embed/play"
        width="100%" height="600" frameborder="0"
        allow="autoplay; encrypted-media" allowfullscreen
        style="border:0; border-radius:8px; max-width:100%;"
        loading="lazy" title="Remote Claw Machine"></iframe>
```

Replace `claw.yourdomain.com` with your actual ECLAW server domain.

---

## WordPress Shortcode Plugin

For a nicer WordPress editing experience, install the shortcode plugin:

1. Copy `wordpress/eclaw-embed.php` to `wp-content/mu-plugins/eclaw-embed.php`
2. Use the `[eclaw]` shortcode in any post or page:

```
[eclaw url="https://claw.yourdomain.com"]
```

### Shortcode Attributes

| Attribute | Default | Description |
|-----------|---------|-------------|
| `url`     | *(required)* | Base URL of the ECLAW server |
| `mode`    | `watch`  | `watch` (spectator only) or `play` (full interactive) |
| `width`   | `100%`   | iframe width |
| `height`  | `480`    | iframe height in pixels |
| `theme`   | `dark`   | `dark` or `light` |
| `footer`  | `1`      | `0` to hide the footer bar (watch mode) |
| `sounds`  | `1`      | `0` to start muted (play mode) |
| `accent`  | *(default purple)* | Hex accent color without `#` (e.g. `ef4444` for red) |

### Examples

```
[eclaw mode="watch" url="https://claw.yourdomain.com" height="360"]

[eclaw mode="play" url="https://claw.yourdomain.com" height="600" theme="light"]

[eclaw url="https://claw.yourdomain.com" footer="0" accent="3b82f6"]
```

---

## Query Parameters

Both embed pages accept URL query parameters for customization:

| Parameter  | Values          | Default     | Applies To |
|-----------|----------------|-------------|------------|
| `theme`   | `dark`, `light` | `dark`      | Both       |
| `footer`  | `0`, `1`        | `1`         | Watch only |
| `playurl` | URL string      | ECLAW origin | Watch only (target for "Play Now" link) |
| `sounds`  | `0`, `1`        | `1`         | Both       |
| `accent`  | hex (no `#`)    | `8b5cf6`    | Both       |
| `bg`      | hex (no `#`)    | `0a0a0f`    | Both       |

Example:
```
https://claw.yourdomain.com/embed/watch?theme=light&footer=0&accent=ef4444
```

---

## postMessage API

The embed pages communicate with the parent page via `window.postMessage`. This lets your WordPress site react to game events.

### Events (embed → parent)

Listen for events on your page:

```javascript
window.addEventListener("message", function(event) {
  if (event.data && event.data.source === "eclaw-embed") {
    console.log("ECLAW event:", event.data.type, event.data);
  }
});
```

| Event Type      | Data Fields                          | Description |
|----------------|--------------------------------------|-------------|
| `queue_update` | `queue_length`, `viewer_count`       | Queue or viewer count changed |
| `state_update` | *(varies)*                           | Game state changed |
| `turn_end`     | `result` (`"win"` or `"loss"`)       | A player's turn ended |
| `joined`       | `position`                           | Player joined the queue (play embed) |
| `playing`      | —                                     | Player's active turn started (play embed) |

### Commands (parent → embed)

Send commands to the interactive embed:

```javascript
var iframe = document.querySelector("iframe");

// Programmatic join
iframe.contentWindow.postMessage({
  target: "eclaw-embed",
  action: "join",
  name: "PlayerName",
  email: "player@example.com"
}, "https://claw.yourdomain.com");

// Leave queue
iframe.contentWindow.postMessage({
  target: "eclaw-embed",
  action: "leave"
}, "https://claw.yourdomain.com");
```

---

## Server Configuration

### EMBED_ALLOWED_ORIGINS

By default, the embed pages allow framing from any origin (`frame-ancestors *`). To restrict which sites can embed:

```env
EMBED_ALLOWED_ORIGINS=https://mysite.com,https://www.mysite.com
```

### nginx

The `deploy/nginx/claw.conf` includes an `/embed/` location block that allows framing. If you use a custom nginx config, ensure the embed paths do NOT have `X-Frame-Options: DENY` and include a `Content-Security-Policy` header with `frame-ancestors`.

---

## Troubleshooting

### iframe blocked / blank

- Check browser console for `X-Frame-Options` or `frame-ancestors` errors
- Ensure your ECLAW nginx config has the `/embed/` location block (see `deploy/nginx/claw.conf`)
- If using `EMBED_ALLOWED_ORIGINS`, verify your WordPress domain is listed

### No video / black screen

- The iframe needs `allow="autoplay; encrypted-media"` — included in both the shortcode and example code
- Video is muted by default (required for autoplay to work in browsers)
- Check that MediaMTX is running and the `/stream/cam/whep` endpoint is reachable

### Mixed content (HTTP site embedding HTTPS stream)

- Your WordPress site should use HTTPS to avoid mixed-content blocking
- The ECLAW server must be HTTPS for WebRTC to work reliably

### Storage issues in iframes

- The play embed uses `sessionStorage` (not `localStorage`) to avoid cross-origin storage restrictions
- Session is not persisted across page reloads — this is by design for iframe contexts
