"""Admin REST API endpoints — require X-Admin-Key header."""

import csv
import hmac
import io
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.config import Settings, _resolve_env_file, settings
from app.database import get_db

_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"

admin_router = APIRouter(prefix="/admin")
_admin_logger = logging.getLogger("admin")

# Range constraints for numeric settings. Values outside these bounds are
# rejected by the PUT /admin/config endpoint to prevent runtime errors
# (e.g. division by zero from command_rate_limit_hz=0) and unstable timing.
# Format: {field_name: (min_value, max_value)}  — use None for unbounded.
_RANGE_CONSTRAINTS: dict[str, tuple[float | int | None, float | int | None]] = {
    # Timing — must be positive integers
    "tries_per_player":             (1, 100),
    "turn_time_seconds":            (5, 3600),
    "try_move_seconds":             (5, 3600),
    "post_drop_wait_seconds":       (1, 300),
    "ready_prompt_seconds":         (5, 300),
    "queue_grace_period_seconds":   (0, 3600),

    # GPIO pulse/hold — milliseconds, must be positive
    "coin_pulse_ms":                (10, 5000),
    "drop_pulse_ms":                (10, 5000),
    "drop_hold_max_ms":             (100, 60000),
    "min_inter_pulse_ms":           (10, 5000),
    "direction_hold_max_ms":        (100, 120000),

    # Control — Hz must be >= 1 to avoid division by zero
    "command_rate_limit_hz":        (1, 1000),

    # WebSocket limits — must be positive
    "max_status_viewers":           (1, 10000),
    "status_send_timeout_s":        (0.1, 60),
    "status_keepalive_interval_s":  (5, 600),
    "max_control_connections":      (1, 10000),
    "control_send_timeout_s":       (0.1, 60),
    "control_ping_interval_s":      (5, 600),
    "control_liveness_timeout_s":   (10, 600),
    "control_pre_auth_timeout_s":   (0.5, 30),
    "control_max_message_bytes":    (64, 65536),

    # Camera
    "max_mjpeg_streams":            (1, 200),
    "mjpeg_fps":                    (1, 120),
    "camera_width":                 (160, 7680),
    "camera_height":                (120, 4320),
    "camera_fps":                   (1, 120),
    "camera_warmup_frames":         (0, 100),
    "camera_max_consecutive_failures": (1, 10000),
    "camera_jpeg_quality":          (1, 100),

    # Rate limiting
    "rate_limit_window_s":          (60, 86400),
    "rate_limit_sweep_interval_s":  (10, 86400),
    "join_rate_per_ip":             (1, 10000),
    "join_rate_per_email":          (1, 10000),
    "health_check_timeout_s":       (0.1, 60),
    "history_limit":                (1, 1000),

    # Database
    "db_busy_timeout_ms":           (100, 60000),
    "db_retention_hours":           (1, 8760),

    # Background task intervals — must be positive
    "db_prune_interval_s":          (60, 86400),
    "rate_limit_prune_age_s":       (60, 86400),
    "queue_check_interval_s":       (1, 300),

    # State machine internals
    "ghost_player_age_s":           (5, 600),
    "coin_post_pulse_delay_s":      (0.0, 10),
    "emergency_stop_timeout_s":     (1, 60),
    "turn_end_stuck_timeout_s":     (5, 300),

    # GPIO executor timeouts
    "gpio_op_timeout_s":            (0.5, 30),
    "gpio_pulse_timeout_s":         (0.5, 30),
    "gpio_init_timeout_s":          (1, 60),

    # GPIO executor circuit breaker
    "max_executor_replacements":    (1, 100),
    "executor_replacement_window_s":(10, 600),

    # Watchdog
    "watchdog_check_interval_s":    (1, 60),
    "watchdog_fail_threshold":      (1, 100),

    # Server
    "port":                         (1, 65535),
}

# Metadata for config fields: category, label, description, whether restart is needed.
# Fields not listed here still appear under "Other".
_CONFIG_META: dict[str, dict[str, Any]] = {
    # -- Timing --
    "tries_per_player":          {"cat": "Timing",       "label": "Tries Per Player",           "desc": "Number of claw drop attempts each player gets per turn."},
    "turn_time_seconds":         {"cat": "Timing",       "label": "Turn Time (seconds)",        "desc": "Hard time limit for an entire turn (all tries).", "restart": True},
    "try_move_seconds":          {"cat": "Timing",       "label": "Move Time (seconds)",        "desc": "Time allowed to move the claw before auto-drop."},
    "post_drop_wait_seconds":    {"cat": "Timing",       "label": "Post-Drop Wait (seconds)",   "desc": "Time to wait after drop for win sensor."},
    "ready_prompt_seconds":      {"cat": "Timing",       "label": "Ready Prompt (seconds)",     "desc": "Time the player has to press Ready.", "restart": True},
    "queue_grace_period_seconds":{"cat": "Timing",       "label": "Queue Grace Period (seconds)","desc": "Seconds before stale queue entries are cleaned on restart."},

    # -- GPIO Pulse/Hold --
    "coin_pulse_ms":             {"cat": "GPIO Pulse/Hold", "label": "Coin Pulse (ms)",         "desc": "Duration of the coin credit relay pulse."},
    "drop_pulse_ms":             {"cat": "GPIO Pulse/Hold", "label": "Drop Pulse (ms)",         "desc": "Duration of the drop relay pulse."},
    "drop_hold_max_ms":          {"cat": "GPIO Pulse/Hold", "label": "Drop Hold Max (ms)",      "desc": "Maximum time the drop relay can be held."},
    "min_inter_pulse_ms":        {"cat": "GPIO Pulse/Hold", "label": "Min Inter-Pulse (ms)",    "desc": "Minimum gap between consecutive relay pulses."},
    "direction_hold_max_ms":     {"cat": "GPIO Pulse/Hold", "label": "Direction Hold Max (ms)", "desc": "Maximum time a direction relay can be held."},
    "coin_each_try":             {"cat": "GPIO Pulse/Hold", "label": "Coin Each Try",           "desc": "Credit a coin at the start of each try (vs. only the first)."},

    # -- Control --
    "command_rate_limit_hz":     {"cat": "Control",      "label": "Command Rate Limit (Hz)",    "desc": "Max player control commands per second."},
    "direction_conflict_mode":   {"cat": "Control",      "label": "Direction Conflict Mode",    "desc": "How to handle opposing directions: ignore_new or replace.", "options": ["ignore_new", "replace"]},

    # -- GPIO Pins --
    "pin_coin":                  {"cat": "GPIO Pins",    "label": "PIN_COIN (BCM)",             "desc": "BCM pin number for the coin credit relay.", "restart": True},
    "pin_north":                 {"cat": "GPIO Pins",    "label": "PIN_NORTH (BCM)",            "desc": "BCM pin number for north direction relay.", "restart": True},
    "pin_south":                 {"cat": "GPIO Pins",    "label": "PIN_SOUTH (BCM)",            "desc": "BCM pin number for south direction relay.", "restart": True},
    "pin_west":                  {"cat": "GPIO Pins",    "label": "PIN_WEST (BCM)",             "desc": "BCM pin number for west direction relay.", "restart": True},
    "pin_east":                  {"cat": "GPIO Pins",    "label": "PIN_EAST (BCM)",             "desc": "BCM pin number for east direction relay.", "restart": True},
    "pin_drop":                  {"cat": "GPIO Pins",    "label": "PIN_DROP (BCM)",             "desc": "BCM pin number for the drop relay.", "restart": True},
    "pin_win":                   {"cat": "GPIO Pins",    "label": "PIN_WIN (BCM)",              "desc": "BCM pin number for win sensor input.", "restart": True},
    "relay_active_low":          {"cat": "GPIO Pins",    "label": "Relay Active Low",           "desc": "Set true for active-low relay boards (most 8-channel boards).", "restart": True},

    # -- Server --
    "host":                      {"cat": "Server",       "label": "Host",                       "desc": "Listen address (0.0.0.0 for all interfaces).", "restart": True},
    "port":                      {"cat": "Server",       "label": "Port",                       "desc": "Listen port.", "restart": True},
    "database_path":             {"cat": "Server",       "label": "Database Path",              "desc": "Path to the SQLite database file.", "restart": True},
    "admin_api_key":             {"cat": "Server",       "label": "Admin API Key",              "desc": "Secret key for admin endpoints. Change from default!", "sensitive": True},
    "cors_allowed_origins":      {"cat": "Server",       "label": "CORS Allowed Origins",       "desc": "Comma-separated list of allowed browser origins.", "restart": True},
    "mock_gpio":                 {"cat": "Server",       "label": "Mock GPIO",                  "desc": "Use mock GPIO (no real hardware). Set false for Pi 5.", "restart": True},

    # -- Watchdog --
    "watchdog_health_url":       {"cat": "Watchdog",     "label": "Health URL",                 "desc": "URL the watchdog pings for health checks."},
    "watchdog_check_interval_s": {"cat": "Watchdog",     "label": "Check Interval (seconds)",   "desc": "Seconds between watchdog health checks."},
    "watchdog_fail_threshold":   {"cat": "Watchdog",     "label": "Fail Threshold",             "desc": "Consecutive failures before watchdog intervenes."},

    # -- Stream --
    "mediamtx_health_url":       {"cat": "Stream",       "label": "MediaMTX Health URL",        "desc": "URL to check MediaMTX streaming health."},
    "camera_device":             {"cat": "Stream",       "label": "Camera Device Index",        "desc": "/dev/videoN index for the built-in MJPEG camera.", "restart": True},
    "camera_rtsp_url":           {"cat": "Stream",       "label": "Camera RTSP URL",            "desc": "RTSP fallback URL when device is locked by MediaMTX.", "restart": True},

    # -- DB Maintenance --
    "db_retention_hours":        {"cat": "Database",     "label": "Retention (hours)",           "desc": "Hours to keep completed entries before pruning."},

    # -- WebSocket Limits --
    "max_status_viewers":        {"cat": "WebSocket",    "label": "Max Status Viewers",         "desc": "Maximum concurrent WebSocket status viewers. Takes effect on new connections; existing viewers are not disconnected."},
    "status_send_timeout_s":     {"cat": "WebSocket",    "label": "Status Send Timeout (s)",    "desc": "Per-client send timeout for status broadcasts."},
    "status_keepalive_interval_s":{"cat": "WebSocket",   "label": "Status Keepalive (s)",       "desc": "Seconds between keepalive pings for viewers."},
    "max_control_connections":   {"cat": "WebSocket",    "label": "Max Control Connections",    "desc": "Maximum concurrent player control channels. Requires restart to resize.", "restart": True},
    "control_send_timeout_s":    {"cat": "WebSocket",    "label": "Control Send Timeout (s)",   "desc": "Per-client send timeout for control messages."},
    "control_ping_interval_s":   {"cat": "WebSocket",    "label": "Control Ping Interval (s)",  "desc": "Seconds between pings on control channels."},
    "control_liveness_timeout_s":{"cat": "WebSocket",    "label": "Control Liveness Timeout (s)","desc": "Seconds before an unresponsive player is disconnected."},
    "control_pre_auth_timeout_s":{"cat": "WebSocket",    "label": "Control Pre-Auth Timeout (s)","desc": "Seconds to wait for initial auth message after WS connect. Intentionally short to limit unauthenticated connection dwell time."},
    "control_max_message_bytes": {"cat": "WebSocket",    "label": "Control Max Message (bytes)","desc": "Maximum size of a single control message."},

    # -- MJPEG / Camera --
    "max_mjpeg_streams":         {"cat": "Camera",       "label": "Max MJPEG Streams",          "desc": "Maximum concurrent MJPEG fallback streams. Requires restart to resize.", "restart": True},
    "mjpeg_fps":                 {"cat": "Camera",       "label": "MJPEG FPS",                  "desc": "Frames per second for MJPEG stream."},
    "camera_width":              {"cat": "Camera",       "label": "Camera Width (px)",          "desc": "Camera capture width in pixels.", "restart": True},
    "camera_height":             {"cat": "Camera",       "label": "Camera Height (px)",         "desc": "Camera capture height in pixels.", "restart": True},
    "camera_fps":                {"cat": "Camera",       "label": "Camera FPS",                 "desc": "Camera capture framerate.", "restart": True},
    "camera_warmup_frames":      {"cat": "Camera",       "label": "Warmup Frames",              "desc": "Frames to discard on camera startup."},
    "camera_max_consecutive_failures":{"cat": "Camera",  "label": "Max Consecutive Failures",   "desc": "Camera failures before giving up."},
    "camera_jpeg_quality":       {"cat": "Camera",       "label": "JPEG Quality",               "desc": "JPEG encoding quality (0-100)."},

    # -- Rate Limiting --
    "rate_limit_window_s":       {"cat": "Rate Limiting","label": "Rate Window (seconds)",      "desc": "Time window for rate limit counters."},
    "rate_limit_sweep_interval_s":{"cat": "Rate Limiting","label": "Sweep Interval (seconds)",  "desc": "How often stale rate limit entries are cleaned."},
    "join_rate_per_ip":          {"cat": "Rate Limiting","label": "Joins Per IP (per window)",  "desc": "Max queue joins per IP per rate window."},
    "join_rate_per_email":       {"cat": "Rate Limiting","label": "Joins Per Email (per window)","desc": "Max queue joins per email per rate window."},
    "health_check_timeout_s":    {"cat": "Rate Limiting","label": "Health Check Timeout (s)",   "desc": "Timeout for internal health check requests."},
    "history_limit":             {"cat": "Rate Limiting","label": "History Limit",              "desc": "Max number of recent game results to return."},

    # -- Database Tuning --
    "db_busy_timeout_ms":        {"cat": "Database",     "label": "Busy Timeout (ms)",          "desc": "SQLite busy timeout in milliseconds.", "restart": True},

    # -- Background Tasks --
    "db_prune_interval_s":       {"cat": "Background",   "label": "DB Prune Interval (s)",      "desc": "Seconds between automatic DB prune runs."},
    "rate_limit_prune_age_s":    {"cat": "Background",   "label": "Rate Limit Prune Age (s)",   "desc": "Age in seconds after which rate limit entries are pruned."},
    "queue_check_interval_s":    {"cat": "Background",   "label": "Queue Check Interval (s)",   "desc": "Seconds between periodic queue safety checks."},

    # -- State Machine Internals --
    "ghost_player_age_s":        {"cat": "State Machine","label": "Ghost Player Age (s)",       "desc": "Seconds before a ghost player entry is cleaned up."},
    "coin_post_pulse_delay_s":   {"cat": "State Machine","label": "Coin Post-Pulse Delay (s)",  "desc": "Delay after coin pulse before proceeding."},
    "emergency_stop_timeout_s":  {"cat": "State Machine","label": "E-Stop Timeout (s)",         "desc": "Timeout for emergency stop GPIO operations."},
    "turn_end_stuck_timeout_s":  {"cat": "State Machine","label": "Turn End Stuck Timeout (s)", "desc": "Seconds before a stuck TURN_END state is force-recovered."},

    # -- GPIO Executor Timeouts --
    "gpio_op_timeout_s":         {"cat": "GPIO Timeouts","label": "GPIO Op Timeout (s)",        "desc": "Timeout for normal GPIO operations.", "restart": True},
    "gpio_pulse_timeout_s":      {"cat": "GPIO Timeouts","label": "GPIO Pulse Timeout (s)",     "desc": "Timeout for GPIO pulse operations.", "restart": True},
    "gpio_init_timeout_s":       {"cat": "GPIO Timeouts","label": "GPIO Init Timeout (s)",      "desc": "Timeout for GPIO initialization.", "restart": True},
}


@admin_router.get("/panel", response_class=HTMLResponse, include_in_schema=False)
async def admin_panel_page():
    """Serve the admin panel HTML page (no auth — page handles auth via JS)."""
    html_path = _WEB_DIR / "admin.html"
    if not html_path.exists():
        raise HTTPException(404, "Admin panel not found")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


def require_admin(x_admin_key: str = Header(...)):
    if not hmac.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(403, "Forbidden")


@admin_router.post("/advance", dependencies=[Depends(require_admin)])
async def admin_advance(request: Request):
    """Force end the current player's turn."""
    sm = request.app.state.state_machine
    if sm.active_entry_id:
        await sm.force_end_turn("admin_skipped")
    return {"ok": True}


@admin_router.post("/emergency-stop", dependencies=[Depends(require_admin)])
async def admin_estop(request: Request):
    """Lock all GPIO controls immediately."""
    await request.app.state.gpio_controller.emergency_stop()
    return {"ok": True, "warning": "Controls locked. POST /admin/unlock to re-enable."}


@admin_router.post("/unlock", dependencies=[Depends(require_admin)])
async def admin_unlock(request: Request):
    """Unlock GPIO controls after emergency stop."""
    await request.app.state.gpio_controller.unlock()
    return {"ok": True}


@admin_router.post("/pause", dependencies=[Depends(require_admin)])
async def admin_pause(request: Request):
    """Pause queue advancement (no new players start)."""
    request.app.state.state_machine.pause()
    return {"ok": True}


@admin_router.post("/resume", dependencies=[Depends(require_admin)])
async def admin_resume(request: Request):
    """Resume queue advancement."""
    request.app.state.state_machine.resume()
    await request.app.state.state_machine.advance_queue()
    return {"ok": True}


@admin_router.get("/dashboard", dependencies=[Depends(require_admin)])
async def admin_dashboard(request: Request):
    """Comprehensive dashboard for the demo operator."""
    sm = request.app.state.state_machine
    qm = request.app.state.queue_manager
    gpio = request.app.state.gpio_controller
    hub = request.app.state.ws_hub

    stats = await qm.get_stats()
    queue_entries = await qm.list_queue()
    recent = await qm.get_recent_results(limit=10)

    uptime = time.time() - request.app.state.start_time

    return {
        "uptime_seconds": uptime,
        "game_state": sm.state.value,
        "paused": sm._paused,
        "gpio_locked": gpio.is_locked,
        "viewer_count": hub.viewer_count,
        "active_player": sm.active_entry_id,
        "current_try": sm.current_try,
        "max_tries": settings.tries_per_player,
        "stats": stats,
        "queue": [
            {
                "name": e["name"],
                "state": e["state"],
                "position": e["position"],
                "created_at": e["created_at"],
            }
            for e in queue_entries
        ],
        "recent_results": [
            {
                "name": r["name"],
                "result": r["result"],
                "tries_used": r["tries_used"],
                "completed_at": r["completed_at"],
            }
            for r in recent
        ],
    }


# ---------------------------------------------------------------------------
# Configuration management endpoints
# ---------------------------------------------------------------------------

def _get_field_type(field_name: str) -> str:
    """Return the JSON-friendly type string for a Settings field."""
    field_info = Settings.model_fields.get(field_name)
    if not field_info:
        return "string"
    annotation = field_info.annotation
    if annotation is bool:
        return "boolean"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    return "string"


@admin_router.get("/config", dependencies=[Depends(require_admin)])
async def admin_get_config():
    """Return all configuration values with metadata for the admin UI."""
    fields = []
    for name, field_info in Settings.model_fields.items():
        meta = _CONFIG_META.get(name, {})
        value = getattr(settings, name)
        # Mask sensitive fields — don't leak secrets in API responses
        if meta.get("sensitive") and value:
            sv = str(value)
            display_value = ("*" * (len(sv) - 4) + sv[-4:]) if len(sv) > 4 else "****"
        else:
            display_value = value

        field_data = {
            "key": name,
            "env_key": name.upper(),
            "value": display_value,
            "default": field_info.default,
            "type": _get_field_type(name),
            "category": meta.get("cat", "Other"),
            "label": meta.get("label", name.replace("_", " ").title()),
            "description": meta.get("desc", ""),
            "restart_required": meta.get("restart", False),
        }
        if "options" in meta:
            field_data["options"] = meta["options"]
        fields.append(field_data)

    return {"fields": fields}


@admin_router.put("/config", dependencies=[Depends(require_admin)])
async def admin_update_config(request: Request):
    """Update configuration values and persist to .env file.

    Accepts a JSON body: {"changes": {"KEY": "value", ...}}
    Values are written to the .env file. Settings that can be applied
    at runtime are updated immediately; others are flagged as needing
    a restart.
    """
    body = await request.json()
    changes: dict[str, Any] = body.get("changes", {})
    if not changes:
        raise HTTPException(400, "No changes provided")

    # Validate keys exist in Settings
    valid_keys = set(Settings.model_fields.keys())
    invalid = [k for k in changes if k not in valid_keys]
    if invalid:
        raise HTTPException(400, f"Unknown config keys: {', '.join(invalid)}")

    # Validate and coerce types
    coerced: dict[str, Any] = {}
    for key, raw_value in changes.items():
        field_type = _get_field_type(key)
        try:
            if field_type == "boolean":
                if isinstance(raw_value, bool):
                    coerced[key] = raw_value
                elif isinstance(raw_value, str):
                    coerced[key] = raw_value.lower() in ("true", "1", "yes")
                else:
                    coerced[key] = bool(raw_value)
            elif field_type == "integer":
                coerced[key] = int(raw_value)
            elif field_type == "number":
                coerced[key] = float(raw_value)
            else:
                coerced[key] = str(raw_value)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, f"Invalid value for {key}: {e}")

    # Reject values containing control characters (prevents .env injection)
    for key, value in coerced.items():
        if isinstance(value, str) and any(c in value for c in ("\n", "\r", "\x00")):
            raise HTTPException(
                400,
                f"Invalid value for {key}: must not contain newlines or control characters",
            )

    # Validate numeric ranges to prevent runtime errors (e.g. division by
    # zero from command_rate_limit_hz=0) and unstable timing behaviour.
    for key, value in coerced.items():
        if key not in _RANGE_CONSTRAINTS:
            continue
        lo, hi = _RANGE_CONSTRAINTS[key]
        if lo is not None and value < lo:
            raise HTTPException(
                400,
                f"Invalid value for {key}: must be >= {lo} (got {value})",
            )
        if hi is not None and value > hi:
            raise HTTPException(
                400,
                f"Invalid value for {key}: must be <= {hi} (got {value})",
            )

    # Write changes to .env file
    env_path = _resolve_env_file()
    _write_env_changes(env_path, coerced)

    # Apply runtime changes where possible
    restart_needed = []
    applied = []
    for key, value in coerced.items():
        meta = _CONFIG_META.get(key, {})
        # Update the live settings object
        try:
            object.__setattr__(settings, key, value)
            # Clear cached_property if cors_allowed_origins changed
            if key == "cors_allowed_origins" and "cors_origins" in settings.__dict__:
                del settings.__dict__["cors_origins"]
        except Exception:
            pass

        if meta.get("restart"):
            restart_needed.append(key)
        else:
            applied.append(key)

    _admin_logger.info(
        "Config updated via admin panel: applied=%s, restart_needed=%s",
        applied, restart_needed,
    )

    return {
        "ok": True,
        "applied": applied,
        "restart_needed": restart_needed,
        "message": (
            "All changes saved to .env. "
            + (f"{len(applied)} setting(s) applied immediately. " if applied else "")
            + (f"{len(restart_needed)} setting(s) require a server restart to take effect." if restart_needed else "")
        ),
    }


def _write_env_changes(env_path, changes: dict[str, Any]):
    """Merge changes into the .env file, preserving comments and order.

    Uses atomic write (tempfile + os.replace) so a crash mid-write
    cannot leave a corrupted .env file.
    """
    lines = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)

    # Track which keys we've updated
    updated_keys = set()
    new_lines = []

    for line in lines:
        stripped = line.strip()
        # Skip empty lines and comments — keep as-is
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue

        # Parse KEY=VALUE
        if "=" in stripped:
            env_key = stripped.split("=", 1)[0].strip()
            setting_key = env_key.lower()
            if setting_key in changes:
                value = changes[setting_key]
                # Format booleans as lowercase
                if isinstance(value, bool):
                    value = "true" if value else "false"
                new_lines.append(f"{env_key}={value}\n")
                updated_keys.add(setting_key)
                continue

        new_lines.append(line)

    # Append any new keys that weren't in the file
    for key, value in changes.items():
        if key not in updated_keys:
            env_key = key.upper()
            if isinstance(value, bool):
                value = "true" if value else "false"
            new_lines.append(f"{env_key}={value}\n")

    # Atomic write: write to temp file in the same directory, then rename.
    # os.replace() is atomic on POSIX, so a crash during write leaves the
    # original .env intact.
    content = "".join(new_lines)
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(env_path.parent), prefix=".env.tmp."
        )
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = None  # Mark as closed
        os.replace(tmp_path, str(env_path))
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


@admin_router.post("/kick/{entry_id}", dependencies=[Depends(require_admin)])
async def admin_kick_player(entry_id: str, request: Request):
    """Remove a player from the queue by entry ID."""
    qm = request.app.state.queue_manager
    sm = request.app.state.state_machine

    entry = await qm.get_by_id(entry_id)
    if not entry:
        raise HTTPException(404, "Entry not found")

    if entry["state"] in ("done", "cancelled"):
        raise HTTPException(409, f"Entry already {entry['state']} — cannot kick")

    need_broadcast = False

    if entry["state"] in ("active", "ready"):
        # Force end the active player's turn
        if sm.active_entry_id == entry_id:
            await sm.force_end_turn("admin_skipped")
        else:
            # Ready but not the active entry (stale state) — complete directly
            await qm.complete_entry(entry_id, "admin_skipped", 0)
            need_broadcast = True
    elif entry["state"] == "waiting":
        await qm.complete_entry(entry_id, "admin_skipped", 0)
        need_broadcast = True

    if need_broadcast:
        # Broadcast updated queue so viewer/admin dashboards stay in sync
        status = await qm.get_queue_status()
        entries = await qm.list_queue()
        queue_entries = [
            {"name": e["name"], "state": e["state"], "position": e["position"]}
            for e in entries
        ]
        await request.app.state.ws_hub.broadcast_queue_update(status, queue_entries)

        # Advance queue in case a waiting player should now be promoted
        try:
            await sm.advance_queue()
        except Exception:
            _admin_logger.exception("advance_queue after kick failed")

    return {"ok": True, "name": entry["name"], "previous_state": entry["state"]}


@admin_router.get("/queue-details", dependencies=[Depends(require_admin)])
async def admin_queue_details(request: Request):
    """Return detailed queue entries including IDs for admin actions."""
    qm = request.app.state.queue_manager
    entries = await qm.list_queue_admin()
    return {
        "entries": [
            {
                "id": e["id"],
                "name": e["name"],
                "email": e.get("email", ""),
                "state": e["state"],
                "position": e["position"],
                "ip_address": e.get("ip_address", ""),
                "created_at": e["created_at"],
            }
            for e in entries
        ],
    }


@admin_router.get("/contacts/csv", dependencies=[Depends(require_admin)])
async def admin_contacts_csv():
    """Download all contacts as a CSV file."""
    db = await get_db()
    async with db.execute(
        "SELECT first_name, last_name, email FROM contacts ORDER BY created_at ASC"
    ) as cur:
        rows = await cur.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["First Name", "Last Name", "Email", "SMS ON", "EMAIL ON", "Event"])
    for row in rows:
        writer.writerow([row[0], row[1], row[2], "Yes", "Yes", "Remote Claw"])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="remote_claw_contacts.csv"'
        },
    )
