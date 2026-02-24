# WordPress and Iframe Embedding Guide

This guide documents the current embed surfaces provided by the `web/embed/` frontend.

## 1. Available Embed Endpoints

| Endpoint | Mode | Intended Use |
|---|---|---|
| `/embed/watch` | Spectator | Live stream + status HUD only |
| `/embed/play` | Interactive | Queue join, readiness, controls, and results |

## 2. Basic Iframe Examples

### Watch embed

```html
<iframe
  src="https://claw.example.com/embed/watch"
  width="100%"
  height="360"
  style="border:0; border-radius:8px;"
  loading="lazy"
  allow="autoplay; encrypted-media"
  title="Remote Claw Watch">
</iframe>
```

### Interactive embed

```html
<iframe
  src="https://claw.example.com/embed/play"
  width="100%"
  height="620"
  style="border:0; border-radius:8px;"
  loading="lazy"
  allow="autoplay; encrypted-media"
  title="Remote Claw Play">
</iframe>
```

## 3. Query Parameters

| Parameter | Values | Effect |
|---|---|---|
| `theme` | `dark` / `light` | Base color scheme |
| `accent` | hex color | Accent color override |
| `bg` | hex color | Background override |
| `footer` | `show` / `hide` | Footer visibility |
| `sounds` | `on` / `off` | Initial sound preference |

Example:

```text
/embed/play?theme=dark&accent=%23ff3366&footer=hide
```

## 4. Security Controls

Set `EMBED_ALLOWED_ORIGINS` in `.env` to restrict framing origins.

- Empty value: no origin restriction from app-level embed middleware.
- Non-empty value: app emits `Content-Security-Policy: frame-ancestors ...` for `/embed/*` routes.

For production, mirror the same policy in nginx headers.

## 5. postMessage Integration

Embed pages can send status messages to a parent frame, and parent pages can issue commands.

Recommended integration pattern:
1. Parent registers a strict origin check on `window.message`.
2. Parent only sends commands to the known embed origin.
3. Parent never stores admin secrets in client-side JavaScript.

## 6. WordPress Plugin

This repository includes `wordpress/eclaw-embed.php`.

Typical usage:
1. Install plugin in WordPress.
2. Configure base claw URL in plugin settings.
3. Insert shortcode on page/post for watch or play mode.
4. Confirm your WordPress origin is allowed by `EMBED_ALLOWED_ORIGINS`.

## 7. Troubleshooting

- Blank iframe: verify CSP `frame-ancestors` and nginx headers.
- No controls: interactive mode requires `/ws/control` and API reachability.
- No stream: verify MediaMTX path routing or MJPEG fallback availability.
