"""Built-in USB camera capture via OpenCV.

Provides an MJPEG fallback when MediaMTX is not running.
The camera runs in a background thread and shares the latest
JPEG-encoded frame with request handlers.
"""

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("camera")


class Camera:
    """Captures frames from a V4L2/USB camera using OpenCV."""

    def __init__(self, device: int = 0, width: int = 1280, height: int = 720, fps: int = 30):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self._cap = None
        self._lock = threading.Lock()
        self._frame: Optional[bytes] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """Open the camera and begin capturing. Returns False on failure."""
        try:
            import cv2  # noqa: F811
        except ImportError:
            logger.warning("opencv-python-headless not installed; built-in camera disabled")
            return False

        self._cap = cv2.VideoCapture(self.device)
        if not self._cap.isOpened():
            logger.warning("Cannot open camera device %d", self.device)
            return False

        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._cap.set(cv2.CAP_PROP_FPS, self.fps)

        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

        actual_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        logger.info("Camera started (device=%d, %dx%d)", self.device, actual_w, actual_h)
        return True

    def _capture_loop(self):
        import cv2

        while self._running:
            ret, frame = self._cap.read()
            if ret:
                _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                with self._lock:
                    self._frame = jpeg.tobytes()
            else:
                time.sleep(0.01)

    def get_frame(self) -> Optional[bytes]:
        """Return the latest JPEG-encoded frame, or None."""
        with self._lock:
            return self._frame

    @property
    def is_running(self) -> bool:
        return self._running

    def stop(self):
        """Release the camera and stop the capture thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self._cap:
            self._cap.release()
            self._cap = None
        self._frame = None
        logger.info("Camera stopped")
