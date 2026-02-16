"""Public REST API endpoints."""

import asyncio
import time
from collections import defaultdict

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.database import hash_token

router = APIRouter(prefix="/api")

# Background task set — prevents fire-and-forget tasks from being GC'd.
# Python's event loop only keeps weak references to tasks, so we must hold
# strong refs until they complete.
_background_tasks: set[asyncio.Task] = set()


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


class QueueEntryResponse(BaseModel):
    name: str
    state: str
    position: int | None
    wait_since: str | None


class QueueListResponse(BaseModel):
    entries: list[QueueEntryResponse]
    total: int
    current_player: str | None
    game_state: str | None


class HistoryEntry(BaseModel):
    name: str
    result: str
    tries_used: int | None
    completed_at: str | None


class HistoryResponse(BaseModel):
    entries: list[HistoryEntry]


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
    normalized_name = body.name.strip()
    normalized_email = body.email.strip().lower()

    ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"ip:{ip}", 30)
    check_rate_limit(f"email:{normalized_email}", 15)

    qm = request.app.state.queue_manager
    result = await qm.join(normalized_name, normalized_email, ip)

    # Broadcast updated queue to all viewers
    status = await qm.get_queue_status()
    entries = await qm.list_queue()
    queue_entries = [
        {"name": e["name"], "state": e["state"], "position": e["position"]}
        for e in entries
    ]
    await request.app.state.ws_hub.broadcast_queue_update(status, queue_entries)

    est_wait = result["position"] * settings.turn_time_seconds

    # Advance queue in background so the HTTP response returns immediately,
    # giving the client time to establish the control WebSocket first.
    task = asyncio.create_task(request.app.state.state_machine.advance_queue())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

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
    qm = request.app.state.queue_manager
    left = await qm.leave(hash_token(raw))
    if not left:
        raise HTTPException(404, "No active queue entry found for this token")
    status = await qm.get_queue_status()
    entries = await qm.list_queue()
    queue_entries = [
        {"name": e["name"], "state": e["state"], "position": e["position"]}
        for e in entries
    ]
    await request.app.state.ws_hub.broadcast_queue_update(status, queue_entries)
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


@router.get("/queue", response_model=QueueListResponse)
async def queue_list(request: Request):
    """Full queue listing — shows all waiting, ready, and active players."""
    qm = request.app.state.queue_manager
    sm = request.app.state.state_machine
    entries = await qm.list_queue()

    turn_time = settings.turn_time_seconds
    result_entries = []
    for i, entry in enumerate(entries):
        # Estimate wait: active player = 0, others = position in line * turn time
        result_entries.append(QueueEntryResponse(
            name=entry["name"],
            state=entry["state"],
            position=entry["position"],
            wait_since=entry["created_at"],
        ))

    current_player = None
    for e in entries:
        if e["state"] in ("active", "ready"):
            current_player = e["name"]
            break

    return QueueListResponse(
        entries=result_entries,
        total=len(entries),
        current_player=current_player,
        game_state=sm.state.value,
    )


@router.get("/history", response_model=HistoryResponse)
async def game_history(request: Request):
    """Recent game results — shows the last completed turns."""
    qm = request.app.state.queue_manager
    results = await qm.get_recent_results(limit=20)
    return HistoryResponse(
        entries=[
            HistoryEntry(
                name=r["name"],
                result=r["result"],
                tries_used=r["tries_used"],
                completed_at=r["completed_at"],
            )
            for r in results
        ]
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
