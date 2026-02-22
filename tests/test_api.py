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
async def test_admin_config_rejects_zero_rate_limit_hz(api_client):
    """command_rate_limit_hz=0 would cause division-by-zero at runtime."""
    res = await api_client.put(
        "/admin/config",
        json={"changes": {"command_rate_limit_hz": 0}},
        headers={"X-Admin-Key": "changeme"},
    )
    assert res.status_code == 400
    assert "must be >= 1" in res.json()["detail"]


@pytest.mark.anyio
async def test_admin_config_rejects_negative_timeout(api_client):
    """Negative timeout values should be rejected."""
    res = await api_client.put(
        "/admin/config",
        json={"changes": {"control_liveness_timeout_s": -5}},
        headers={"X-Admin-Key": "changeme"},
    )
    assert res.status_code == 400
    assert "must be >= " in res.json()["detail"]


@pytest.mark.anyio
async def test_admin_config_rejects_exceeding_max(api_client):
    """Values above the max bound should be rejected."""
    res = await api_client.put(
        "/admin/config",
        json={"changes": {"camera_jpeg_quality": 200}},
        headers={"X-Admin-Key": "changeme"},
    )
    assert res.status_code == 400
    assert "must be <= 100" in res.json()["detail"]


@pytest.mark.anyio
async def test_admin_config_accepts_valid_values(api_client):
    """Valid values within range should be accepted."""
    original = settings.command_rate_limit_hz
    try:
        res = await api_client.put(
            "/admin/config",
            json={"changes": {"command_rate_limit_hz": 50}},
            headers={"X-Admin-Key": "changeme"},
        )
        assert res.status_code == 200
        assert res.json()["ok"] is True
    finally:
        settings.command_rate_limit_hz = original


@pytest.mark.anyio
async def test_admin_config_rejects_unknown_control_auth_timeout(api_client):
    """control_auth_timeout_s was removed; submitting it should fail."""
    res = await api_client.put(
        "/admin/config",
        json={"changes": {"control_auth_timeout_s": 10}},
        headers={"X-Admin-Key": "changeme"},
    )
    assert res.status_code == 400
    assert "Unknown config keys" in res.json()["detail"]


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


# ===========================================================================
# Proxy-IP / Rate-Limit Regression Tests
# ===========================================================================


@pytest.mark.anyio
async def test_xff_ignored_when_trusted_proxies_empty(api_client):
    """When TRUSTED_PROXIES is empty, X-Forwarded-For should be ignored
    and rate limiting should use the direct client IP."""
    import time as _time
    import app.api.routes as routes_mod

    original_tp = settings.trusted_proxies
    settings.trusted_proxies = ""
    routes_mod._proxy_header_warned = False
    try:
        # Pre-fill rate limit for the spoofed IP — should have NO effect
        _join_limits["ip:10.99.99.99"] = [_time.time()] * 100

        # Request with X-Forwarded-For pointing to the spoofed IP
        res = await api_client.post(
            "/api/queue/join",
            json={"name": "ProxyTest", "email": "proxy1@example.com"},
            headers={"X-Forwarded-For": "10.99.99.99"},
        )
        # Should succeed because rate limit is checked against direct IP
        # (127.0.0.1), not the spoofed X-Forwarded-For IP
        assert res.status_code == 200, (
            f"Expected 200 but got {res.status_code}: XFF should be ignored "
            f"when TRUSTED_PROXIES is empty"
        )

        token = res.json()["token"]
        await asyncio.sleep(0.05)
        await api_client.delete(
            "/api/queue/leave",
            headers={"Authorization": f"Bearer {token}"},
        )
        await asyncio.sleep(0.05)
    finally:
        settings.trusted_proxies = original_tp


# ===========================================================================
# Profanity Filter Tests
# ===========================================================================


@pytest.mark.anyio
async def test_profanity_name_rejected(api_client):
    """Names containing profanity should be rejected with 400."""
    res = await api_client.post(
        "/api/queue/join",
        json={"name": "damn player", "email": "profane@example.com"},
    )
    assert res.status_code == 400
    assert "inappropriate language" in res.json()["detail"]


@pytest.mark.anyio
async def test_clean_name_accepted(api_client):
    """Normal, clean names should be accepted."""
    res = await api_client.post(
        "/api/queue/join",
        json={"name": "FriendlyPlayer", "email": "clean@example.com"},
    )
    assert res.status_code == 200


@pytest.mark.anyio
async def test_profanity_filter_disabled(api_client):
    """When profanity_filter_enabled=False, profane names should be allowed."""
    original = settings.profanity_filter_enabled
    settings.profanity_filter_enabled = False
    try:
        res = await api_client.post(
            "/api/queue/join",
            json={"name": "damn player", "email": "nofilter@example.com"},
        )
        assert res.status_code == 200
    finally:
        settings.profanity_filter_enabled = original

@pytest.mark.anyio
async def test_xff_used_when_trusted_proxies_match(api_client):
    """When TRUSTED_PROXIES matches the direct connection IP,
    X-Forwarded-For SHOULD be used for rate limiting."""
    import time as _time
    import app.api.routes as routes_mod

    original_tp = settings.trusted_proxies
    # The test client connects from 127.0.0.1 (httpx ASGITransport default)
    settings.trusted_proxies = "127.0.0.1/32"
    routes_mod._proxy_header_warned = False
    try:
        # Pre-fill rate limit for the forwarded IP to just below the limit
        forwarded_ip = "203.0.113.50"
        _join_limits[f"ip:{forwarded_ip}"] = [_time.time()] * (settings.join_rate_per_ip - 1)

        # This request should use the X-Forwarded-For IP for rate limiting
        res = await api_client.post(
            "/api/queue/join",
            json={"name": "TrustedProxy", "email": "trusted1@example.com"},
            headers={"X-Forwarded-For": forwarded_ip},
        )
        assert res.status_code == 200, (
            f"Expected 200 but got {res.status_code}: should succeed (at limit - 1)"
        )
        token = res.json()["token"]
        await asyncio.sleep(0.05)
        await api_client.delete(
            "/api/queue/leave",
            headers={"Authorization": f"Bearer {token}"},
        )
        await asyncio.sleep(0.05)

        # Now the forwarded IP should be at the limit — next request should be blocked
        blocked = await api_client.post(
            "/api/queue/join",
            json={"name": "TrustedProxy2", "email": "trusted2@example.com"},
            headers={"X-Forwarded-For": forwarded_ip},
        )
        assert blocked.status_code == 429, (
            f"Expected 429 but got {blocked.status_code}: forwarded IP should be "
            f"rate-limited when proxy is trusted"
        )
    finally:
        settings.trusted_proxies = original_tp


@pytest.mark.anyio
async def test_different_xff_ips_get_independent_limits(api_client):
    """Different X-Forwarded-For IPs should get independent rate limits
    when the proxy is trusted."""
    import time as _time
    import app.api.routes as routes_mod

    original_tp = settings.trusted_proxies
    settings.trusted_proxies = "127.0.0.1/32"
    routes_mod._proxy_header_warned = False
    try:
        ip_a = "198.51.100.10"
        ip_b = "198.51.100.20"

        # Exhaust rate limit for IP A
        _join_limits[f"ip:{ip_a}"] = [_time.time()] * settings.join_rate_per_ip

        # IP A should be blocked
        res_a = await api_client.post(
            "/api/queue/join",
            json={"name": "PlayerA", "email": "a@example.com"},
            headers={"X-Forwarded-For": ip_a},
        )
        assert res_a.status_code == 429, (
            f"Expected 429 for IP A but got {res_a.status_code}"
        )

        # IP B should NOT be blocked (independent limit)
        res_b = await api_client.post(
            "/api/queue/join",
            json={"name": "PlayerB", "email": "b@example.com"},
            headers={"X-Forwarded-For": ip_b},
        )
        assert res_b.status_code == 200, (
            f"Expected 200 for IP B but got {res_b.status_code}: "
            f"different XFF IPs should have independent rate limits"
        )

        token = res_b.json()["token"]
        await asyncio.sleep(0.05)
        await api_client.delete(
            "/api/queue/leave",
            headers={"Authorization": f"Bearer {token}"},
        )
        await asyncio.sleep(0.05)
    finally:
        settings.trusted_proxies = original_tp
