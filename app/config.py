"""Configuration via Pydantic Settings, loaded from .env file."""

import logging
import os
from functools import cached_property
from pathlib import Path
from pydantic_settings import BaseSettings

_cfg_logger = logging.getLogger("config")

_INSECURE_KEYS = {"changeme", "demo-admin-key", ""}

# Resolve .env path relative to the project root (parent of app/) so it
# works regardless of the working directory the process is launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_env_file() -> Path:
    """Return an absolute path to the .env file.

    If ``REMOTE_CLAW_ENV_FILE`` is set, use it (resolved relative to the project
    root when not absolute).  Otherwise default to ``<project_root>/.env``.
    """
    raw = os.environ.get("REMOTE_CLAW_ENV_FILE", "")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else _PROJECT_ROOT / p
    return _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    # Timing
    tries_per_player: int = 2
    turn_time_seconds: int = 90
    try_move_seconds: int = 30
    post_drop_wait_seconds: int = 8
    post_drop_wait_no_sensor_seconds: int = 3
    ready_prompt_seconds: int = 15
    queue_grace_period_seconds: int = 300

    # Win sensor
    win_sensor_enabled: bool = True

    # GPIO pulse/hold
    coin_pulses_per_credit: int = 2
    coin_pulse_ms: int = 150
    drop_pulse_ms: int = 200
    drop_hold_max_ms: int = 10000
    min_inter_pulse_ms: int = 500
    direction_hold_max_ms: int = 30000
    coin_each_try: bool = True

    # Control
    command_rate_limit_hz: int = 25
    direction_conflict_mode: str = "ignore_new"  # or "replace"

    # Pins (BCM numbering)
    pin_coin: int = 17
    pin_north: int = 27
    pin_south: int = 5
    pin_west: int = 6
    pin_east: int = 24
    pin_drop: int = 25
    pin_win: int = 16

    # Relay board polarity: most 8-channel relay modules are active-low
    # (relay engages when GPIO pin goes LOW). Set to true for those boards.
    relay_active_low: bool = True

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    database_path: str = "./data/claw.db"
    admin_api_key: str = "changeme"
    cors_allowed_origins: str = "http://localhost,http://127.0.0.1"

    # Watchdog
    watchdog_health_url: str = "http://127.0.0.1:8000/api/health"
    watchdog_check_interval_s: int = 2
    watchdog_fail_threshold: int = 3

    # Stream
    mediamtx_health_url: str = "http://127.0.0.1:8889/v3/paths/list"
    camera_device: int = 0  # /dev/videoN index for built-in MJPEG fallback
    camera_rtsp_url: str = "rtsp://127.0.0.1:8554/cam"  # RTSP fallback when device is locked by MediaMTX

    # Trusted reverse proxy CIDRs (comma-separated). Only trust
    # X-Forwarded-For when the direct connection is from a listed CIDR.
    # Empty = always use request.client.host (safe default).
    trusted_proxies: str = ""

    # Mock mode: set to true when running without real GPIO hardware
    mock_gpio: bool = False

    # -- Embed configuration ---------------------------------------------------
    embed_allowed_origins: str = ""  # Comma-separated origins allowed to frame embed pages

    # -- Player-facing UI text (editable via admin panel) ----------------------
    ui_queue_title: str = "You're In Line!"
    ui_queue_subtitle: str = "Hang tight — we'll pop your turn prompt automatically."
    ui_ready_title: str = "IT'S YOUR TURN!"
    ui_ready_subtitle: str = "Get ready to control the claw"
    ui_result_win_title: str = "YOU WON!"
    ui_result_win_message: str = "Congratulations! You grabbed a prize!"
    ui_result_loss_title: str = "😞"
    ui_result_loss_message: str = "Better luck next time!"
    ui_result_turn_over_title: str = "Turn Over"
    ui_result_turn_over_message: str = "Thanks for playing!"
    ui_result_expired_title: str = "Time's Up"
    ui_result_expired_message: str = "Your turn has ended."

    # -- WLED LED strip integration (optional) --------------------------------
    wled_enabled: bool = False
    wled_device_ip: str = ""
    wled_preset_win: int = 0
    wled_preset_loss: int = 0
    wled_preset_drop: int = 0
    wled_preset_start_turn: int = 0
    wled_preset_idle: int = 0
    wled_preset_expire: int = 0
    wled_preset_grab: int = 0           # Fired during POST_DROP when win sensor is off (claw returning)
    wled_result_display_seconds: float = 5.0  # How long win/loss/drop/expire/grab shows before reverting to idle

    # DB maintenance: hours to keep completed entries before pruning
    db_retention_hours: int = 48

    # -- WebSocket limits -----------------------------------------------------

    # Status hub: broadcast channel for all viewers
    max_status_viewers: int = 500
    status_send_timeout_s: float = 5.0
    status_keepalive_interval_s: int = 30

    # Control handler: authenticated per-player channel
    max_control_connections: int = 100
    control_send_timeout_s: float = 2.0
    control_ping_interval_s: int = 20
    control_liveness_timeout_s: int = 60
    control_pre_auth_timeout_s: float = 2.0
    control_max_message_bytes: int = 1024

    # -- MJPEG / Camera -------------------------------------------------------

    max_mjpeg_streams: int = 20
    mjpeg_fps: int = 30
    camera_width: int = 1280
    camera_height: int = 720
    camera_fps: int = 30
    camera_warmup_frames: int = 5
    camera_max_consecutive_failures: int = 100
    camera_jpeg_quality: int = 80

    # -- Profanity filter -----------------------------------------------------

    profanity_filter_enabled: bool = True

    # -- Rate limiting --------------------------------------------------------

    rate_limit_window_s: int = 3600
    rate_limit_sweep_interval_s: int = 600
    join_rate_per_ip: int = 30
    join_rate_per_email: int = 15
    health_check_timeout_s: float = 2.0
    history_limit: int = 20

    # -- Database tuning ------------------------------------------------------

    db_busy_timeout_ms: int = 5000

    # -- Background task intervals --------------------------------------------

    db_prune_interval_s: int = 3600
    rate_limit_prune_age_s: int = 3600
    queue_check_interval_s: int = 10

    # -- State machine internals ----------------------------------------------

    ghost_player_age_s: int = 30
    coin_post_pulse_delay_s: float = 0.5
    emergency_stop_timeout_s: float = 10.0
    turn_end_stuck_timeout_s: int = 30

    # -- GPIO executor timeouts -----------------------------------------------

    gpio_op_timeout_s: float = 2.0
    gpio_pulse_timeout_s: float = 5.0
    gpio_init_timeout_s: float = 10.0

    # -- GPIO executor circuit breaker ----------------------------------------

    max_executor_replacements: int = 5
    executor_replacement_window_s: int = 60

    model_config = {
        "env_file": str(_resolve_env_file()),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @cached_property
    def cors_origins(self) -> list[str]:
        origins = [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]
        return origins or ["http://localhost", "http://127.0.0.1"]

    def warn_insecure_defaults(self):
        """Log warnings about insecure defaults. Called once at startup."""
        if self.admin_api_key in _INSECURE_KEYS:
            _cfg_logger.warning(
                "ADMIN_API_KEY is set to an insecure default ('%s'). "
                "Change it before exposing to the internet!",
                self.admin_api_key,
            )
        if not self.trusted_proxies:
            _cfg_logger.info(
                "TRUSTED_PROXIES is empty — X-Forwarded-For headers will be "
                "ignored. Set TRUSTED_PROXIES if running behind a reverse proxy "
                "(e.g. TRUSTED_PROXIES=127.0.0.1/32,::1/128)."
            )


settings = Settings()
