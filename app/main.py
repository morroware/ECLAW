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
    waiting players and kick-start the queue if so.  This catches edge
    cases where advance_queue was missed (e.g. exception in a timer task)."""
    from app.game.state_machine import TurnState
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            if sm.state == TurnState.IDLE and sm.active_entry_id is None:
                waiting = await sm.queue.get_waiting_count()
                if waiting > 0:
                    logger.info("Periodic queue check: IDLE with %d waiting, advancing", waiting)
                    await sm.advance_queue()
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
    await qm.cleanup_stale(settings.turn_time_seconds * 2)
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
    try:
        while True:
            await ws.receive_text()  # Keep alive; ignore messages
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception as e:
        logger.warning("Status WS error: %s", e)
        hub.disconnect(ws)


@app.websocket("/ws/control")
async def ws_control(ws: WebSocket):
    await app.state.control_handler.handle_connection(ws)


# Static files (served by nginx in prod, useful in dev)
static_dir = os.path.join(os.path.dirname(__file__), "..", "web")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
