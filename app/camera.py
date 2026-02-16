"""Built-in USB camera capture via OpenCV.

Provides an MJPEG fallback when MediaMTX is not running.
The camera runs in a background thread and shares the latest
JPEG-encoded frame with request handlers.
"""

import glob
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("camera")


def _find_camera_device(preferred: int = 0) -> Optional[int]:
    """Auto-detect a working camera device index.

    USB cameras often register multiple /dev/video* nodes (one for
    video capture, others for metadata).  We try the preferred index
    first, then scan even-numbered devices which are typically the
    actual capture interfaces.
    """
    try:
        import cv2
    except ImportError:
        return None

    # Try preferred device first
    candidates = [preferred]

    # Then scan even-numbered devices (0, 2, 4, ...) which are typically capture nodes
    for path in sorted(glob.glob("/dev/video*")):
        try:
            idx = int(path.replace("/dev/video", ""))
            if idx != preferred and idx % 2 == 0:
                candidates.append(idx)
        except ValueError:
            continue

    for idx in candidates:
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            # Verify it can actually produce a frame
            ret, _ = cap.read()
            cap.release()
            if ret:
                return idx
            logger.debug("Device %d opens but produces no frames", idx)
        else:
            cap.release()

    return None


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
            import cv2
        except ImportError:
            logger.warning("opencv-python-headless not installed; built-in camera disabled")
            return False

        # Auto-detect a working device if the preferred one doesn't work
        detected = _find_camera_device(self.device)
        if detected is None:
            logger.warning("No working camera found (tried /dev/video*)")
            return False

        if detected != self.device:
            logger.info("Preferred device %d unavailable, using /dev/video%d", self.device, detected)
        self.device = detected

        self._cap = cv2.VideoCapture(self.device)
        if not self._cap.isOpened():
            logger.warning("Cannot open camera device %d", self.device)
            return False

        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._cap.set(cv2.CAP_PROP_FPS, self.fps)

        # Warm-up: some USB cameras need a few frames before producing good output
        for _ in range(5):
            self._cap.read()

        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

        actual_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        logger.info("Camera started (device=/dev/video%d, %dx%d)", self.device, actual_w, actual_h)
        return True

    def _capture_loop(self):
        import cv2

        consecutive_failures = 0
        while self._running:
            ret, frame = self._cap.read()
            if ret:
                consecutive_failures = 0
                _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                with self._lock:
                    self._frame = jpeg.tobytes()
            else:
                consecutive_failures += 1
                if consecutive_failures > 100:
                    logger.error("Camera lost — too many consecutive read failures")
                    self._running = False
                    break
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
