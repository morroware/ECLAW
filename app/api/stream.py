"""Built-in MJPEG streaming endpoints.

Serves frames captured by the built-in Camera when MediaMTX is not available.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.config import settings

router = APIRouter(prefix="/api")

# Lazy-initialized semaphore — created on first request so the value
# comes from settings (which may be overridden via .env).
_mjpeg_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _mjpeg_semaphore
    if _mjpeg_semaphore is None:
        _mjpeg_semaphore = asyncio.Semaphore(settings.max_mjpeg_streams)
    return _mjpeg_semaphore


@router.get("/stream/snapshot")
async def snapshot(request: Request):
    """Return a single JPEG frame from the camera."""
    camera = getattr(request.app.state, "camera", None)
    if not camera or not camera.is_running:
        raise HTTPException(503, "Camera not available")
    frame = camera.get_frame()
    if frame is None:
        raise HTTPException(503, "No frame available yet")
    return Response(content=frame, media_type="image/jpeg")


@router.get("/stream/mjpeg")
async def mjpeg_stream(request: Request):
    """Continuous MJPEG stream (multipart/x-mixed-replace)."""
    camera = getattr(request.app.state, "camera", None)
    if not camera or not camera.is_running:
        raise HTTPException(503, "Camera not available")

    sem = _get_semaphore()
    if sem.locked():
        raise HTTPException(503, "Too many active streams")

    async def generate():
        await sem.acquire()
        try:
            while True:
                if await request.is_disconnected():
                    break
                frame = camera.get_frame()
                if frame:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                        + frame
                        + b"\r\n"
                    )
                await asyncio.sleep(1 / settings.mjpeg_fps)
        finally:
            sem.release()

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
