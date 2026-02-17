"""API integration tests — tests the full REST API + WebSocket flow."""

import asyncio
import os

import pytest
from httpx import ASGITransport, AsyncClient

import app.database as db_module
from app.database import close_db
from app.config import settings
from app.main import app
from app.api.routes import _join_limits


@pytest.fixture
async def api_client(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    # Ensure the settings and database module see the fresh path
    settings.database_path = db_path
    # Reset the database singleton so a fresh DB is created
    db_module._db = None
    db_module._db_lock = None
    db_module._write_lock = None
    _join_limits.clear()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    # Ensure db singleton is reset between tests
    await close_db()
    db_module._db = None


@pytest.mark.anyio
async def test_health_endpoint(api_client):
    res = await api_client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"


@pytest.mark.anyio
async def test_queue_join_and_status(api_client):
    # Join queue
    res = await api_client.post(
        "/api/queue/join",
        json={
            "name": "TestPlayer",
            "email": "test@example.com",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert "token" in data
    assert data["position"] >= 1
    token = data["token"]

    # Check queue status
    res = await api_client.get("/api/queue/status")
    assert res.status_code == 200

    # Check session
    res = await api_client.get(
        "/api/session/me",
        headers={
            "Authorization": f"Bearer {token}",
        },
    )
    assert res.status_code == 200
    session = res.json()
    assert session["state"] in ("waiting", "ready", "active")

    # Leave queue
    res = await api_client.delete(
        "/api/queue/leave",
        headers={
            "Authorization": f"Bearer {token}",
        },
    )
    assert res.status_code == 200


@pytest.mark.anyio
async def test_queue_full_listing(api_client):
    """Test the full queue listing endpoint returns structured data."""
    # Empty queue
    res = await api_client.get("/api/queue")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 0
    assert data["entries"] == []
    assert data["current_player"] is None

    # Add players
    tokens = []
    for i, name in enumerate(["Alice", "Bob", "Charlie"]):
        res = await api_client.post(
            "/api/queue/join",
            json={"name": name, "email": f"{name.lower()}@test.com"},
        )
        assert res.status_code == 200, res.text
        tokens.append(res.json()["token"])

    # Check listing
    res = await api_client.get("/api/queue")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] >= 2  # At least some entries (first may have advanced)
    assert data["game_state"] is not None

    # Entries should have name, state, position
    for entry in data["entries"]:
        assert "name" in entry
        assert "state" in entry
        assert entry["state"] in ("waiting", "ready", "active")

    # Leave one and verify count decreases
    res = await api_client.delete(
        "/api/queue/leave",
        headers={"Authorization": f"Bearer {tokens[2]}"},
    )
    assert res.status_code == 200

    res = await api_client.get("/api/queue")
    new_total = res.json()["total"]
    assert new_total < data["total"]


@pytest.mark.anyio
async def test_history_endpoint(api_client):
    """Test the history endpoint returns empty initially."""
    res = await api_client.get("/api/history")
    assert res.status_code == 200
    data = res.json()
    assert "entries" in data
    assert isinstance(data["entries"], list)


@pytest.mark.anyio
async def test_admin_dashboard(api_client):
    """Test the admin dashboard endpoint."""
    # Without admin key — should fail
    res = await api_client.get("/admin/dashboard")
    assert res.status_code == 422  # Missing header

    # With admin key
    res = await api_client.get(
        "/admin/dashboard",
        headers={"X-Admin-Key": "changeme"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "uptime_seconds" in data
    assert "game_state" in data
    assert "stats" in data
    assert "queue" in data
    assert "recent_results" in data
    assert isinstance(data["stats"]["waiting"], int)
    assert isinstance(data["stats"]["total_wins"], int)




@pytest.mark.anyio
async def test_join_rate_limit_normalizes_email(api_client):
    import time

    # Pre-fill the rate limiter to be 2 below the 15/hour email limit.
    # This way the 3 case-insensitive variants below will push it over.
    _join_limits["email:demo@example.com"] = [time.time()] * 12

    email_variants = [
        "Demo@Example.com",
        " demo@example.com ",
        "DEMO@example.com",
    ]

    for i, email in enumerate(email_variants):
        res = await api_client.post(
            "/api/queue/join",
            json={"name": f"Player{i}", "email": email},
        )
        assert res.status_code == 200
        # Leave queue so the next join with the same (normalized) email is allowed.
        # Allow background advance_queue task to settle before leaving.
        token = res.json()["token"]
        await asyncio.sleep(0.05)
        await api_client.delete(
            "/api/queue/leave",
            headers={"Authorization": f"Bearer {token}"},
        )
        # Let state machine finish cleanup (force_end_turn -> advance_queue)
        await asyncio.sleep(0.05)

    # 12 pre-filled + 3 joins = 15 total; next should be blocked
    blocked = await api_client.post(
        "/api/queue/join",
        json={"name": "Player3", "email": "demo@example.com"},
    )
    assert blocked.status_code == 429


@pytest.mark.anyio
async def test_duplicate_queue_entry_blocked(api_client):
    """Joining with the same email while already in the queue returns 409."""
    res = await api_client.post(
        "/api/queue/join",
        json={"name": "Alice", "email": "alice@example.com"},
    )
    assert res.status_code == 200

    # Second join with the same (normalized) email should be rejected
    dup = await api_client.post(
        "/api/queue/join",
        json={"name": "Alice2", "email": "Alice@Example.com"},
    )
    assert dup.status_code == 409



@pytest.mark.anyio
async def test_join_background_task_is_tracked(api_client):
    app = api_client._transport.app

    res = await api_client.post(
        "/api/queue/join",
        json={"name": "Tracked", "email": "tracked@example.com"},
    )
    assert res.status_code == 200

    # Task should be registered for lifecycle cleanup.
    await asyncio.sleep(0)
    assert len(app.state.background_tasks) >= 1

@pytest.mark.anyio
async def test_join_validation(api_client):
    # Name too short
    res = await api_client.post(
        "/api/queue/join",
        json={
            "name": "X",
            "email": "test@example.com",
        },
    )
    assert res.status_code == 422

    # Missing email
    res = await api_client.post(
        "/api/queue/join",
        json={
            "name": "TestPlayer",
        },
    )
    assert res.status_code == 422


@pytest.mark.anyio
async def test_leave_with_invalid_token(api_client):
    """Leaving with a bogus token should return 404."""
    res = await api_client.delete(
        "/api/queue/leave",
        headers={"Authorization": "Bearer bogus-token-12345"},
    )
    assert res.status_code == 404
