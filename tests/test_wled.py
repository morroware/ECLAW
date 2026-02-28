import asyncio

import httpx
import pytest

from app import wled as wled_mod
from app.wled import WLEDClient


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeClient:
    def __init__(self, fail_timeouts=0):
        self.fail_timeouts = fail_timeouts
        self.posts = []

    async def post(self, url, json):
        self.posts.append((url, json))
        if self.fail_timeouts > 0:
            self.fail_timeouts -= 1
            raise httpx.TimeoutException("timeout")
        return _FakeResponse(200)

    async def get(self, _url):
        return _FakeResponse(200)

    async def aclose(self):
        return None


@pytest.fixture
def _wled_settings(monkeypatch):
    defaults = {
        "wled_enabled": True,
        "wled_device_ip": "127.0.0.1",
        "wled_preset_win": 11,
        "wled_preset_loss": 12,
        "wled_preset_drop": 13,
        "wled_preset_start_turn": 14,
        "wled_preset_idle": 15,
        "wled_preset_expire": 16,
        "wled_preset_grab": 17,
        "wled_preset_result": 99,
        "wled_result_display_seconds": 0.01,
        "wled_http_retries": 1,
        "wled_retry_backoff_seconds": 0.0,
        "wled_connect_timeout_s": 2.0,
        "wled_read_timeout_s": 3.0,
        "wled_write_timeout_s": 2.0,
        "wled_pool_timeout_s": 2.0,
    }
    for k, v in defaults.items():
        monkeypatch.setattr(wled_mod.settings, k, v)


@pytest.mark.anyio
async def test_transient_event_reverts_to_idle(_wled_settings):
    client = WLEDClient()
    await client.start()
    fake = _FakeClient()
    client._client = fake

    await client.on_event("win")
    await asyncio.sleep(0.05)

    presets = [payload["ps"] for _, payload in fake.posts]
    assert presets == [11, 15]
    await client.close()


@pytest.mark.anyio
async def test_transient_uses_fallback_result_preset_when_event_missing(_wled_settings, monkeypatch):
    monkeypatch.setattr(wled_mod.settings, "wled_preset_win", 0)
    client = WLEDClient()
    await client.start()
    fake = _FakeClient()
    client._client = fake

    await client.on_event("win")
    await asyncio.sleep(0.05)

    presets = [payload["ps"] for _, payload in fake.posts]
    assert presets == [99, 15]
    await client.close()


@pytest.mark.anyio
async def test_timeout_retry_succeeds(_wled_settings):
    client = WLEDClient()
    await client.start()
    fake = _FakeClient(fail_timeouts=1)
    client._client = fake

    ok = await client.trigger_preset(50)

    assert ok is True
    assert len(fake.posts) == 2
    await client.close()
