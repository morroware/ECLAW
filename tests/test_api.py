"""API integration tests — tests the full REST API + WebSocket flow."""

import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import close_db
from app.main import app


@pytest.fixture
async def api_client(tmp_path):
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    # Ensure db singleton is reset between tests
    await close_db()


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
