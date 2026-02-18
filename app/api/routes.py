"""Public REST API endpoints."""

import asyncio
import logging
import re
import time
from collections import defaultdict

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.database import hash_token

router = APIRouter(prefix="/api")

# Strip HTML-significant characters from player names to prevent stored XSS
_NAME_UNSAFE = re.compile(r"[<>&\"']")


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
    game_state: str
    gpio_locked: bool
    camera_ok: bool
    queue_length: int
    viewer_count: int
    uptime_seconds: float


# -- Rate Limiting -----------------------------------------------------------
#
# Primary rate limiter uses SQLite (durable across restarts, consistent
# across workers).  A fast in-memory cache is kept as a hot-path
# optimisation — the DB is the source of truth.

_join_limits: dict[str, list[float]] = defaultdict(list)
_last_rate_limit_sweep: float = 0.0
logger = logging.getLogger("api.routes")


def _get_client_ip(request: Request) -> str:
    """Extract real client IP, respecting X-Forwarded-For behind a reverse proxy."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(key: str, max_per_hour: int):
    """In-memory rate limiter (legacy, used as fast-path cache)."""
    global _last_rate_limit_sweep
    now = time.time()

    # Periodic sweep of stale entries to prevent unbounded memory growth
    if now - _last_rate_limit_sweep > settings.rate_limit_sweep_interval_s:
        _last_rate_limit_sweep = now
        stale = [k for k, v in _join_limits.items() if all(now - t >= settings.rate_limit_window_s for t in v)]
        for k in stale:
            del _join_limits[k]

    recent = [t for t in _join_limits[key] if now - t < settings.rate_limit_window_s]
    if not recent:
        _join_limits.pop(key, None)
    else:
        _join_limits[key] = recent
    if len(recent) >= max_per_hour:
        raise HTTPException(429, "Rate limit exceeded. Try again later.")
    _join_limits[key].append(now)


async def check_rate_limit_db(key: str, max_per_hour: int):
    """SQLite-backed rate limiter — durable across restarts.

    Counts rows for ``key`` created within the last hour.  If the count
    meets or exceeds ``max_per_hour``, raises HTTP 429.  Otherwise
    inserts a new timestamp row.
    """
    import app.database as _db_mod
    db = await _db_mod.get_db()
    _db_mod._ensure_locks()

    async with _db_mod._write_lock:
        async with db.execute(
            "SELECT COUNT(*) FROM rate_limits "
            "WHERE key = ? AND ts > datetime('now', '-1 hour')",
            (key,),
        ) as cur:
            row = await cur.fetchone()
            count = row[0] if row else 0

        if count >= max_per_hour:
            raise HTTPException(429, "Rate limit exceeded. Try again later.")

        await db.execute(
            "INSERT INTO rate_limits (key) VALUES (?)",
            (key,),
        )
        await db.commit()


async def prune_rate_limits(max_age_seconds: int = 3600):
    """Delete rate limit records older than max_age_seconds."""
    import app.database as _db_mod
    db = await _db_mod.get_db()
    _db_mod._ensure_locks()
    async with _db_mod._write_lock:
        await db.execute(
            "DELETE FROM rate_limits WHERE ts < datetime('now', ?)",
            (f"-{max_age_seconds} seconds",),
        )
        await db.commit()


# -- Endpoints ---------------------------------------------------------------

def _track_background_task(request: Request, task: asyncio.Task):
    tasks = request.app.state.background_tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _advance_queue_safe(request: Request):
    try:
        await request.app.state.state_machine.advance_queue()
    except Exception:
        logger.exception("Background advance_queue task failed")


@router.post("/queue/join", response_model=JoinResponse)
async def queue_join(body: JoinRequest, request: Request):
    normalized_name = _NAME_UNSAFE.sub("", body.name.strip())
    if len(normalized_name) < 2:
        raise HTTPException(400, "Name must be at least 2 characters (no HTML allowed)")
    normalized_email = body.email.strip().lower()

    ip = _get_client_ip(request)
    # Fast in-memory check first (hot path), then durable DB check
    check_rate_limit(f"ip:{ip}", settings.join_rate_per_ip)
    check_rate_limit(f"email:{normalized_email}", settings.join_rate_per_email)
    await check_rate_limit_db(f"ip:{ip}", settings.join_rate_per_ip)
    await check_rate_limit_db(f"email:{normalized_email}", settings.join_rate_per_email)

    qm = request.app.state.queue_manager
    try:
        result = await qm.join(normalized_name, normalized_email, ip)
    except ValueError as e:
        raise HTTPException(409, str(e))

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
    task = asyncio.create_task(_advance_queue_safe(request))
    _track_background_task(request, task)

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
    token_hash = hash_token(raw)

    # Look up entry first so we can route active vs waiting/ready correctly
    entry = await qm.get_by_token(token_hash)
    if not entry or entry["state"] in ("done", "cancelled"):
        raise HTTPException(404, "No active queue entry found for this token")

    sm = request.app.state.state_machine

    # If the player is the current active/ready entry, force-end their turn.
    # This MUST be checked before qm.leave() because leave() only handles
    # waiting/ready DB states — an active player would wrongly get 404.
    if entry["id"] == sm.active_entry_id:
        await sm.force_end_turn("cancelled")
    else:
        left = await qm.leave(token_hash)
        if not left:
            raise HTTPException(404, "No active queue entry found for this token")

        # Broadcast updated queue to all viewers
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

    result_entries = []
    for entry in entries:
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
    results = await qm.get_recent_results(limit=settings.history_limit)
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


# Shared httpx client for health-check probes.  Created lazily on first
# call and reused across requests so we don't spin up (and tear down) a
# new TCP connection pool on every /api/health hit — important when 50+
# viewers are polling or the watchdog checks every 2 s.
_health_http: object = None  # httpx.AsyncClient, lazily created


async def _get_health_http():
    global _health_http
    if _health_http is None:
        import httpx
        _health_http = httpx.AsyncClient(timeout=settings.health_check_timeout_s)
    return _health_http


@router.get("/health", response_model=HealthResponse)
async def health(request: Request):
    camera_ok = False
    try:
        client = await _get_health_http()
        r = await client.get(settings.mediamtx_health_url)
        camera_ok = r.status_code == 200
    except Exception:
        pass

    sm = request.app.state.state_machine
    qm = request.app.state.queue_manager
    gpio = request.app.state.gpio_controller

    return HealthResponse(
        status="ok",
        game_state=sm.state.value,
        gpio_locked=gpio.is_locked,
        camera_ok=camera_ok,
        queue_length=(await qm.get_queue_status())["queue_length"],
        viewer_count=request.app.state.ws_hub.viewer_count,
        uptime_seconds=time.time() - request.app.state.start_time,
    )
