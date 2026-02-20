"""Reverse proxy for MediaMTX WHEP/WHIP endpoints.

When running without nginx (e.g. direct access on port 8000), the
frontend's WebRTC player POSTs to /stream/cam/whep which has no FastAPI
route — only nginx knew how to proxy that to MediaMTX.

This router proxies /stream/{path} requests to MediaMTX on port 8889 so
video streaming works when accessing the app directly on port 8000.
"""

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.config import settings

logger = logging.getLogger("stream_proxy")

router = APIRouter()

# Derive MediaMTX base URL from the health URL (strips /v3/paths/list).
_MEDIAMTX_BASE = settings.mediamtx_health_url.rsplit("/v3/", 1)[0]

# Shared async client — created lazily, closed at shutdown.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=_MEDIAMTX_BASE, timeout=10.0)
    return _client


async def close_proxy_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# Headers to forward from the upstream response.
_FORWARD_HEADERS = {
    "content-type",
    "location",
    "accept-patch",
    "access-control-allow-origin",
    "access-control-expose-headers",
    "link",
}


@router.api_route("/stream/{path:path}", methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"])
async def proxy_mediamtx(path: str, request: Request):
    """Proxy any /stream/* request to MediaMTX, rewriting the Location header."""
    client = _get_client()
    body = await request.body()

    # Forward relevant headers (Content-Type is critical for SDP exchange).
    upstream_headers = {}
    if "content-type" in request.headers:
        upstream_headers["Content-Type"] = request.headers["content-type"]

    try:
        upstream = await client.request(
            method=request.method,
            url=f"/{path}",
            content=body,
            headers=upstream_headers,
        )
    except httpx.ConnectError:
        return Response(
            content="MediaMTX not reachable — is it running?",
            status_code=502,
        )

    # Build response headers, rewriting Location to point back through us.
    resp_headers = {}
    for key in upstream.headers:
        if key.lower() in _FORWARD_HEADERS:
            value = upstream.headers[key]
            # Rewrite absolute Location URLs from MediaMTX back to /stream/
            if key.lower() == "location" and _MEDIAMTX_BASE in value:
                value = value.replace(_MEDIAMTX_BASE, "/stream")
            resp_headers[key] = value

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )
