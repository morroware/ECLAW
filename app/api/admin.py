"""Admin REST API endpoints — require X-Admin-Key header."""

import hmac
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.config import settings

admin_router = APIRouter(prefix="/admin")


def require_admin(x_admin_key: str = Header(...)):
    if not hmac.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(403, "Forbidden")


@admin_router.post("/advance", dependencies=[Depends(require_admin)])
async def admin_advance(request: Request):
    """Force end the current player's turn."""
    sm = request.app.state.state_machine
    if sm.active_entry_id:
        await sm.force_end_turn("admin_skipped")
    return {"ok": True}


@admin_router.post("/emergency-stop", dependencies=[Depends(require_admin)])
async def admin_estop(request: Request):
    """Lock all GPIO controls immediately."""
    await request.app.state.gpio_controller.emergency_stop()
    return {"ok": True, "warning": "Controls locked. POST /admin/unlock to re-enable."}


@admin_router.post("/unlock", dependencies=[Depends(require_admin)])
async def admin_unlock(request: Request):
    """Unlock GPIO controls after emergency stop."""
    await request.app.state.gpio_controller.unlock()
    return {"ok": True}


@admin_router.post("/pause", dependencies=[Depends(require_admin)])
async def admin_pause(request: Request):
    """Pause queue advancement (no new players start)."""
    request.app.state.state_machine.pause()
    return {"ok": True}


@admin_router.post("/resume", dependencies=[Depends(require_admin)])
async def admin_resume(request: Request):
    """Resume queue advancement."""
    request.app.state.state_machine.resume()
    await request.app.state.state_machine.advance_queue()
    return {"ok": True}


@admin_router.get("/dashboard", dependencies=[Depends(require_admin)])
async def admin_dashboard(request: Request):
    """Comprehensive dashboard for the demo operator."""
    sm = request.app.state.state_machine
    qm = request.app.state.queue_manager
    gpio = request.app.state.gpio_controller
    hub = request.app.state.ws_hub

    stats = await qm.get_stats()
    queue_entries = await qm.list_queue()
    recent = await qm.get_recent_results(limit=10)

    uptime = time.time() - request.app.state.start_time

    return {
        "uptime_seconds": uptime,
        "game_state": sm.state.value,
        "paused": sm._paused,
        "gpio_locked": gpio.is_locked,
        "viewer_count": hub.viewer_count,
        "active_player": sm.active_entry_id,
        "current_try": sm.current_try,
        "max_tries": settings.tries_per_player,
        "stats": stats,
        "queue": [
            {
                "name": e["name"],
                "state": e["state"],
                "position": e["position"],
                "created_at": e["created_at"],
            }
            for e in queue_entries
        ],
        "recent_results": [
            {
                "name": r["name"],
                "result": r["result"],
                "tries_used": r["tries_used"],
                "completed_at": r["completed_at"],
            }
            for r in recent
        ],
    }
