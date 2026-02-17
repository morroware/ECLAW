"""FastAPI application entrypoint — lifespan, routes, and WebSocket endpoints."""

import logging
import os
import time
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

from app.api.admin import admin_router
from app.api.routes import router as api_router
from app.api.stream import router as stream_router
from app.camera import Camera
from app.config import settings
from app.database import close_db, get_db, prune_old_entries
from app.game.queue_manager import QueueManager
from app.game.state_machine import StateMachine
from app.gpio.controller import GPIOController
from app.ws.control_handler import ControlHandler
from app.ws.status_hub import StatusHub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("main")


async def _periodic_db_prune(interval_seconds: int = 3600):
    """Background task that prunes old DB entries periodically."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await prune_old_entries(settings.db_retention_hours)
        except Exception:
            logger.exception("Periodic DB prune failed")


async def _periodic_queue_check(sm, interval_seconds: int = 10):
    """Safety net: periodically check if the state machine is IDLE with
    waiting players and kick-start the queue if so.  Also detects stuck
    states where the active entry was cancelled/completed externally,
    and any state that has been stuck for longer than the hard maximum."""
    from app.game.state_machine import TurnState
    import time as _time

    # Hard maximum time a non-IDLE state can persist before forced recovery.
    # This catches any edge case where timers are silently lost (GC, task
    # cancellation, unhandled exception).  The budget is generous to avoid
    # false positives: full turn time + ready prompt + 60 s buffer.
    max_non_idle_seconds = (
        settings.turn_time_seconds + settings.ready_prompt_seconds + 60
    )
    # TURN_END should never last more than a few seconds (GPIO cleanup).
    max_turn_end_seconds = 30

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            if sm.state == TurnState.IDLE and sm.active_entry_id is None:
                waiting = await sm.queue.get_waiting_count()
                if waiting > 0:
                    logger.info("Periodic queue check: IDLE with %d waiting, advancing", waiting)
                    await sm.advance_queue()
            elif sm.state == TurnState.IDLE and sm.active_entry_id is not None:
                # Stuck state: SM is IDLE but active_entry_id wasn't cleared.
                # This happens if advance_queue() partially executed (set
                # active_entry_id) but crashed before entering READY_PROMPT.
                logger.warning(
                    "Periodic queue check: IDLE but active_entry_id=%s still set, clearing",
                    sm.active_entry_id,
                )
                sm.active_entry_id = None
                sm.current_try = 0
                await sm.advance_queue()
            elif sm.state == TurnState.TURN_END:
                # TURN_END should resolve in seconds.  If _end_turn is stuck
                # (e.g. GPIO hang), force the state machine back to IDLE.
                stuck_seconds = _time.monotonic() - sm._last_state_change
                if stuck_seconds > max_turn_end_seconds:
                    logger.error(
                        "Periodic queue check: stuck in TURN_END for %.0fs "
                        "(entry=%s), forcing IDLE",
                        stuck_seconds, sm.active_entry_id,
                    )
                    # Complete the entry in DB if still pending
                    if sm.active_entry_id:
                        try:
                            await sm.queue.complete_entry(
                                sm.active_entry_id, "error", sm.current_try
                            )
                        except Exception:
                            logger.exception("Failed to complete stuck entry (non-fatal)")
                    sm.state = TurnState.IDLE
                    sm._last_state_change = _time.monotonic()
                    sm.active_entry_id = None
                    sm.current_try = 0
                    sm._state_deadline = 0.0
                    sm._turn_deadline = 0.0
                    await sm.advance_queue()
            elif sm.state not in (TurnState.IDLE, TurnState.TURN_END):
                if not sm.active_entry_id:
                    logger.error(
                        "Periodic queue check: non-idle state %s without active entry, recovering",
                        sm.state,
                    )
                    await sm._force_recover()
                    continue

                # Check if the active entry has been externally terminated
                # (e.g. cancelled via leave, or completed by a race condition).
                # If so, the state machine is stuck — force recovery.
                entry = await sm.queue.get_by_id(sm.active_entry_id)
                if entry is None or entry["state"] in ("done", "cancelled"):
                    logger.warning(
                        "Periodic queue check: active entry %s is %s in DB but SM is in %s, recovering",
                        sm.active_entry_id,
                        entry["state"] if entry else "MISSING",
                        sm.state,
                    )
                    await sm._force_recover()
                else:
                    # General stuck-state detector: if ANY non-IDLE state has
                    # persisted longer than the hard maximum, all timers have
                    # failed — force recovery.
                    stuck_seconds = _time.monotonic() - sm._last_state_change
                    if stuck_seconds > max_non_idle_seconds:
                        logger.error(
                            "Periodic queue check: state %s stuck for %.0fs "
                            "(entry=%s), exceeds hard max of %ds — forcing recovery",
                            sm.state, stuck_seconds,
                            sm.active_entry_id, max_non_idle_seconds,
                        )
                        await sm._force_recover()
        except Exception:
            logger.exception("Periodic queue check failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    logger.info("Starting claw machine server")
    settings.warn_insecure_defaults()
    app.state.start_time = time.time()
    app.state.background_tasks = set()

    # Init database
    await get_db()
    logger.info("Database initialized")

    # Init GPIO
    gpio = GPIOController()
    await gpio.initialize()
    app.state.gpio_controller = gpio

    # Init built-in camera (MJPEG fallback when MediaMTX is not running)
    camera = Camera(device=settings.camera_device)
    if camera.start():
        app.state.camera = camera
    else:
        app.state.camera = None

    # Init managers
    qm = QueueManager()
    # Use the grace period setting (not turn time) so players who disconnected
    # within their reconnection window aren't unfairly expired on restart.
    await qm.cleanup_stale(settings.queue_grace_period_seconds)
    app.state.queue_manager = qm

    ws_hub = StatusHub()
    app.state.ws_hub = ws_hub

    # Create state machine and control handler (circular ref resolved via late binding)
    ctrl = ControlHandler(None, qm, gpio, settings)  # sm set below
    sm = StateMachine(gpio, qm, ws_hub, ctrl, settings)
    ctrl.sm = sm  # wire up the reference

    app.state.state_machine = sm
    app.state.control_handler = ctrl

    # Start periodic DB cleanup task
    prune_task = asyncio.create_task(_periodic_db_prune())
    app.state.background_tasks.add(prune_task)
    prune_task.add_done_callback(app.state.background_tasks.discard)

    # Start periodic queue advancement safety net
    queue_check_task = asyncio.create_task(_periodic_queue_check(sm))
    app.state.background_tasks.add(queue_check_task)
    queue_check_task.add_done_callback(app.state.background_tasks.discard)

    # Resume queue if entries exist
    await sm.advance_queue()

    logger.info("Server ready (mock_gpio=%s)", settings.mock_gpio)
    yield

    # Shutdown
    logger.info("Shutting down")
    tasks = list(app.state.background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    if app.state.camera:
        app.state.camera.stop()
    await gpio.cleanup()
    await close_db()
    logger.info("Shutdown complete")


app = FastAPI(
    title="ECLAW Remote Claw Machine",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.mock_gpio else None,
    redoc_url=None,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Key"],
)

# REST routes
app.include_router(api_router)
app.include_router(admin_router)
app.include_router(stream_router)


# WebSocket routes
@app.websocket("/ws/status")
async def ws_status(ws: WebSocket):
    hub = app.state.ws_hub
    connected = await hub.connect(ws)
    if not connected:
        return

    async def _keepalive():
        """Send periodic pings so intermediate proxies and firewalls
        don't kill idle viewer connections (important for internet users
        behind corporate NATs or CDN edge nodes)."""
        try:
            while True:
                await asyncio.sleep(30)
                await ws.send_text('{"type":"ping"}')
        except Exception:
            pass

    ping_task = asyncio.create_task(_keepalive())
    try:
        while True:
            await ws.receive_text()  # Keep alive; ignore messages
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception as e:
        logger.warning("Status WS error: %s", e)
        hub.disconnect(ws)
    finally:
        ping_task.cancel()


@app.websocket("/ws/control")
async def ws_control(ws: WebSocket):
    await app.state.control_handler.handle_connection(ws)


# Static files (served by nginx in prod, useful in dev)
static_dir = os.path.join(os.path.dirname(__file__), "..", "web")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
