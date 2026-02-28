# WordPress and Iframe Embedding Guide

This guide documents the embed surfaces provided by the `web/embed/` frontend and the WordPress plugin.

## 1. Available Embed Endpoints

| Endpoint | Mode | Intended Use |
|---|---|---|
| `/embed/watch` | Spectator | Live stream + status HUD only |
| `/embed/play` | Interactive | Queue join, readiness, controls, and results |

Both endpoints include:
- **Play overlay** — shown when browser blocks autoplay (user taps to start video)
- **Mute/unmute toggle** — bottom-right corner, hover to reveal (always visible on touch)
- **Picture-in-Picture button** — pop out the video stream
- **Fullscreen button** — expand the embed to full screen

## 2. Basic Iframe Examples

### Watch embed

```html
<iframe
  src="https://claw.example.com/embed/watch"
  width="100%"
  height="360"
  style="border:0; border-radius:8px;"
  loading="lazy"
  allow="autoplay; encrypted-media; fullscreen; picture-in-picture"
  allowfullscreen
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
  allow="autoplay; encrypted-media; fullscreen; picture-in-picture"
  allowfullscreen
  title="Remote Claw Play">
</iframe>
```

### Responsive embed (16:9)

```html
<div style="position:relative;width:100%;height:0;padding-bottom:56.25%;overflow:hidden;">
  <iframe
    src="https://claw.example.com/embed/play"
    style="position:absolute;top:0;left:0;width:100%;height:100%;border:0;border-radius:8px;"
    allow="autoplay; encrypted-media; fullscreen; picture-in-picture"
    allowfullscreen
    loading="lazy"
    title="Remote Claw Play">
  </iframe>
</div>
```

## 3. Query Parameters

| Parameter | Values | Effect | Default |
|---|---|---|---|
| `theme` | `dark` / `light` | Base color scheme | `dark` |
| `accent` | hex color (no #) | Accent/primary color override | `8b5cf6` |
| `bg` | hex color (no #) | Background color override | `0a0a0f` |
| `footer` | `0` / `1` | Footer visibility (watch mode) | `1` |
| `sounds` | `0` / `1` | Sound effects (play mode) | `1` |

Example:

```text
/embed/play?theme=light&accent=ff3366&sounds=0
```

## 4. Security Controls

Set `EMBED_ALLOWED_ORIGINS` in `.env` to restrict framing origins.

- Empty value: no origin restriction from app-level embed middleware.
- Non-empty value: app emits `Content-Security-Policy: frame-ancestors ...` for `/embed/*` routes.

For production, mirror the same policy in nginx headers.

## 5. postMessage Integration

Embed pages can send status messages to a parent frame, and parent pages can issue commands.

### Embed to parent

```javascript
// Events sent by the embed
{
  source: "eclaw-embed",
  type: "joined" | "queue_update" | "playing" | "turn_end",
  position: 5,           // queue position (joined)
  viewer_count: 42,      // viewer count (queue_update)
  result: "win"          // turn result (turn_end)
}
```

### Parent to embed

```javascript
// Auto-join a player
iframe.contentWindow.postMessage({
  target: "eclaw-embed",
  action: "join",
  name: "Player Name",
  email: "player@example.com"
}, "https://claw.example.com");

// Leave queue
iframe.contentWindow.postMessage({
  target: "eclaw-embed",
  action: "leave"
}, "https://claw.example.com");
```

## 6. WordPress Plugin (v2.0)

The repository includes `wordpress/eclaw-embed.php` — a full-featured WordPress plugin.

### Installation

1. Copy `wordpress/eclaw-embed.php` to `wp-content/mu-plugins/eclaw-embed.php`
   OR create `wp-content/plugins/eclaw-embed/` and place the file inside
2. Activate the plugin in WordPress admin
3. Go to **Settings > Remote Claw** and configure your server URL
4. Add your WordPress domain to `EMBED_ALLOWED_ORIGINS` in your ECLAW `.env` file

### Admin Settings

The plugin provides a settings page at **Settings > Remote Claw** where you can configure:

- Default server URL
- Default mode (watch/play)
- Default theme (dark/light)
- Default dimensions
- Accent and background colors
- Responsive mode and aspect ratio
- Footer and sound defaults
- Loading strategy (lazy/eager)

All settings act as defaults and can be overridden per-shortcode.

### Shortcode Reference

```
[eclaw]                                              Watch mode, default settings
[eclaw mode="play"]                                  Interactive play mode
[eclaw url="https://claw.example.com"]               Custom server URL
[eclaw mode="play" height="620"]                     Custom height
[eclaw responsive="1"]                               Responsive 16:9 sizing
[eclaw responsive="1" aspect_ratio="4:3"]            Responsive with 4:3 ratio
[eclaw theme="light" accent="ff3366"]                Light theme + pink accent
[eclaw bg="1a1a2e" border_radius="12"]               Custom background + rounding
[eclaw footer="0" sounds="0"]                        Hide footer, mute sounds
[eclaw loading="eager"]                              Load immediately (above fold)
[eclaw class="my-widget" title="Play Claw!"]         Extra CSS class + title
```

| Attribute | Values | Default | Description |
|---|---|---|---|
| `url` | URL | (from settings) | ECLAW server base URL |
| `mode` | `watch`, `play` | `watch` | Spectator or interactive |
| `theme` | `dark`, `light` | `dark` | Color scheme |
| `width` | CSS value | `100%` | Iframe width |
| `height` | Pixels | `480` | Iframe height (ignored if responsive) |
| `responsive` | `0`, `1` | `0` | Use aspect-ratio responsive sizing |
| `aspect_ratio` | e.g. `16:9` | `16:9` | Aspect ratio for responsive mode |
| `accent` | Hex (no #) | — | Accent/primary color |
| `bg` | Hex (no #) | — | Background color |
| `border_radius` | Pixels | `8` | Corner rounding |
| `footer` | `0`, `1` | `1` | Show footer (watch mode) |
| `sounds` | `0`, `1` | `1` | Sound effects (play mode) |
| `loading` | `lazy`, `eager` | `lazy` | Iframe loading strategy |
| `title` | Text | Remote Claw Machine | Iframe title (accessibility) |
| `class` | CSS classes | — | Extra CSS classes on wrapper |

### Elementor Usage

In Elementor, add an **HTML widget** or **Shortcode widget** and paste:

```
[eclaw mode="play" responsive="1"]
```

The `responsive="1"` option ensures the embed scales properly within Elementor columns and sections.

For a fixed-height embed in a specific Elementor column:

```
[eclaw mode="play" height="600"]
```

### Gutenberg Block

The plugin registers a **Remote Claw Machine** block in the block editor. Search for "claw" or "arcade" in the block inserter. The block provides sidebar controls for:

- Server URL
- Mode (watch/play)
- Theme (dark/light)
- Responsive sizing toggle
- Aspect ratio and height
- Accent and background colors
- Footer and sound toggles

The block renders a live preview in the editor.

## 7. Troubleshooting

| Problem | Solution |
|---|---|
| Blank/white iframe | Verify CSP `frame-ancestors` includes your WordPress domain. Check `EMBED_ALLOWED_ORIGINS` in `.env` |
| Video shows but won't play | A "Tap to Watch Live" overlay should appear. If not, ensure iframe has `allow="autoplay"`. In Elementor, use the shortcode widget instead of raw HTML |
| No controls visible | Interactive mode requires `/ws/control` WebSocket. Verify server is reachable from the browser |
| No video stream | Check MediaMTX is running and the stream path is configured. Verify SSL/TLS if using HTTPS |
| Embed too small/large | Use `responsive="1"` for automatic sizing, or set explicit `height` |
| Sound not working | Browsers require user interaction before audio. Click unmute button in the bottom-right corner |
| Video not autoplaying | Most browsers block unmuted autoplay in iframes. Video starts muted; users can unmute via the control button |
| PiP not available | Picture-in-Picture requires a secure context (HTTPS) and browser support |
