"""Public REST API endpoints."""

import time
from collections import defaultdict

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.database import hash_token

router = APIRouter(prefix="/api")


# -- Models ------------------------------------------------------------------

class JoinRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=50)
    email: str = Field(..., min_length=5, max_length=100)


class JoinResponse(BaseModel):
    token: str
    position: int
    estimated_wait_seconds: int


class QueueStatusResponse(BaseModel):
    current_player: str | None
    current_player_state: str | None
    queue_length: int


class SessionResponse(BaseModel):
    state: str
    position: int | None
    tries_left: int | None
    current_try: int | None


class HealthResponse(BaseModel):
    status: str
    gpio_locked: bool
    camera_ok: bool
    queue_length: int
    viewer_count: int
    uptime_seconds: float


# -- Rate Limiting (in-memory) -----------------------------------------------

_join_limits: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(key: str, max_per_hour: int):
    now = time.time()
    _join_limits[key] = [t for t in _join_limits[key] if now - t < 3600]
    if len(_join_limits[key]) >= max_per_hour:
        raise HTTPException(429, "Rate limit exceeded. Try again later.")
    _join_limits[key].append(now)


# -- Endpoints ---------------------------------------------------------------

@router.post("/queue/join", response_model=JoinResponse)
async def queue_join(body: JoinRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"ip:{ip}", 5)
    check_rate_limit(f"email:{body.email}", 3)

    qm = request.app.state.queue_manager
    result = await qm.join(body.name.strip(), body.email.strip(), ip)

    # Kick off queue advancement
    await request.app.state.state_machine.advance_queue()

    est_wait = result["position"] * settings.turn_time_seconds
    return JoinResponse(
        token=result["token"],
        position=result["position"],
        estimated_wait_seconds=est_wait,
    )


@router.delete("/queue/leave")
async def queue_leave(request: Request, authorization: str = Header(...)):
    raw = authorization.removeprefix("Bearer ").strip()
    if not raw:
        raise HTTPException(401, "Missing token")
    await request.app.state.queue_manager.leave(hash_token(raw))
    await request.app.state.ws_hub.broadcast_queue_update(
        await request.app.state.queue_manager.get_queue_status()
    )
    return {"ok": True}


@router.get("/queue/status", response_model=QueueStatusResponse)
async def queue_status(request: Request):
    status = await request.app.state.queue_manager.get_queue_status()
    return QueueStatusResponse(**status)


@router.get("/session/me", response_model=SessionResponse)
async def session_me(request: Request, authorization: str = Header(...)):
    raw = authorization.removeprefix("Bearer ").strip()
    if not raw:
        raise HTTPException(401, "Missing token")
    entry = await request.app.state.queue_manager.get_by_token(hash_token(raw))
    if not entry:
        raise HTTPException(401, "Invalid token")
    sm = request.app.state.state_machine
    is_active = entry["id"] == sm.active_entry_id
    return SessionResponse(
        state=entry["state"],
        position=entry.get("position"),
        tries_left=(
            settings.tries_per_player - (sm.current_try if is_active else entry.get("tries_used", 0))
        ),
        current_try=sm.current_try if is_active else None,
    )


@router.get("/health", response_model=HealthResponse)
async def health(request: Request):
    camera_ok = False
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get(settings.mediamtx_health_url, timeout=2)
            camera_ok = r.status_code == 200
    except Exception:
        pass

    sm = request.app.state.state_machine
    qm = request.app.state.queue_manager
    gpio = request.app.state.gpio_controller

    return HealthResponse(
        status="ok",
        gpio_locked=gpio.is_locked,
        camera_ok=camera_ok,
        queue_length=(await qm.get_queue_status())["queue_length"],
        viewer_count=request.app.state.ws_hub.viewer_count,
        uptime_seconds=time.time() - request.app.state.start_time,
    )
