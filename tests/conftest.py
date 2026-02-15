"""Test configuration — sets up mock GPIO and temp database."""

import os

# Force mock GPIO for all tests — must be set before any app imports
os.environ["MOCK_GPIO"] = "true"
os.environ["GPIOZERO_PIN_FACTORY"] = "mock"

import pytest  # noqa: E402

from app.config import Settings  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def mock_settings(tmp_path):
    """Create settings with temp database path."""
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    os.environ["MOCK_GPIO"] = "true"
    return Settings(
        database_path=db_path,
        mock_gpio=True,
    )
