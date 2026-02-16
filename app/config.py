"""Configuration via Pydantic Settings, loaded from .env file."""

import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Timing
    tries_per_player: int = 2
    turn_time_seconds: int = 90
    try_move_seconds: int = 30
    post_drop_wait_seconds: int = 8
    ready_prompt_seconds: int = 15
    queue_grace_period_seconds: int = 300

    # GPIO pulse/hold
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
    host: str = "127.0.0.1"
    port: int = 8000
    database_path: str = "./data/claw.db"
    admin_api_key: str = "changeme"

    # Watchdog
    watchdog_health_url: str = "http://127.0.0.1:8000/api/health"
    watchdog_check_interval_s: int = 2
    watchdog_fail_threshold: int = 3

    # Stream
    mediamtx_health_url: str = "http://127.0.0.1:8889/v3/paths/list"
    camera_device: int = 0  # /dev/videoN index for built-in MJPEG fallback

    # Mock mode: set to true when running without real GPIO hardware
    mock_gpio: bool = False

    model_config = {
        "env_file": os.environ.get("ECLAW_ENV_FILE", ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
