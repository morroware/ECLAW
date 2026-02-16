"""Built-in MJPEG streaming endpoints.

Serves frames captured by the built-in Camera when MediaMTX is not available.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

router = APIRouter(prefix="/api")


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

    async def generate():
        while True:
            frame = camera.get_frame()
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
            await asyncio.sleep(1 / 30)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
