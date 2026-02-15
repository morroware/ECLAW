"""API integration tests — tests the full REST API + WebSocket flow."""

import os
os.environ["MOCK_GPIO"] = "true"

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_health_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"


@pytest.mark.anyio
async def test_queue_join_and_status():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Join queue
        res = await client.post("/api/queue/join", json={
            "name": "TestPlayer",
            "email": "test@example.com",
        })
        assert res.status_code == 200
        data = res.json()
        assert "token" in data
        assert data["position"] >= 1
        token = data["token"]

        # Check queue status
        res = await client.get("/api/queue/status")
        assert res.status_code == 200

        # Check session
        res = await client.get("/api/session/me", headers={
            "Authorization": f"Bearer {token}",
        })
        assert res.status_code == 200
        session = res.json()
        assert session["state"] in ("waiting", "ready", "active")

        # Leave queue
        res = await client.delete("/api/queue/leave", headers={
            "Authorization": f"Bearer {token}",
        })
        assert res.status_code == 200


@pytest.mark.anyio
async def test_join_validation():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Name too short
        res = await client.post("/api/queue/join", json={
            "name": "X",
            "email": "test@example.com",
        })
        assert res.status_code == 422

        # Missing email
        res = await client.post("/api/queue/join", json={
            "name": "TestPlayer",
        })
        assert res.status_code == 422
