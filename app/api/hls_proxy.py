"""Reverse proxy for MediaMTX HLS endpoints.

Proxies /hls/{path} to MediaMTX's HLS server (port 8888) so iOS Safari
can fall back to native HLS when WebRTC UDP is unreachable.

Only needed when nginx cannot reach MediaMTX directly (e.g. the
claw-proxy deployment where nginx runs on a separate VM).
"""

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter()

_HLS_BASE = "http://127.0.0.1:8888"
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=_HLS_BASE, timeout=10.0)
    return _client


async def close_hls_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@router.get("/hls/{path:path}")
async def proxy_hls(path: str, request: Request):
    """Proxy HLS playlist and segment requests to MediaMTX."""
    client = _get_client()
    try:
        upstream = await client.get(f"/{path}")
    except httpx.ConnectError:
        return Response(content="HLS not available", status_code=502)

    resp_headers = {}
    for key in ("content-type", "cache-control"):
        if key in upstream.headers:
            resp_headers[key] = upstream.headers[key]

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )
