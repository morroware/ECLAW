"""Test configuration — sets up mock GPIO and temp database."""

import os

# Force mock GPIO for tests
os.environ["MOCK_GPIO"] = "true"
os.environ["GPIOZERO_PIN_FACTORY"] = "mock"

import pytest
import pytest_asyncio

from app.config import Settings


@pytest.fixture
def mock_settings(tmp_path):
    """Create settings with temp database path."""
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    os.environ["MOCK_GPIO"] = "true"
    settings = Settings(
        database_path=str(tmp_path / "test.db"),
        mock_gpio=True,
    )
    return settings
