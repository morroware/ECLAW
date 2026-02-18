"""Built-in USB camera capture via OpenCV.

Provides an MJPEG fallback when WebRTC video fails on certain devices.
Tries to open the camera device directly first; if that fails (e.g.
MediaMTX already has exclusive access), falls back to reading from
MediaMTX's RTSP output.
"""

import glob
import logging
import threading
import time
from typing import Optional

from app.config import settings

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
    """Captures frames from a V4L2/USB camera or MediaMTX RTSP stream."""

    def __init__(self, device: int = 0, width: int | None = None,
                 height: int | None = None, fps: int | None = None,
                 rtsp_url: str | None = None):
        self.device = device
        self.width = width if width is not None else settings.camera_width
        self.height = height if height is not None else settings.camera_height
        self.fps = fps if fps is not None else settings.camera_fps
        self.rtsp_url = rtsp_url
        self._cap = None
        self._lock = threading.Lock()
        self._frame: Optional[bytes] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._source_name: str = "none"

    def start(self) -> bool:
        """Open the camera and begin capturing. Returns False on failure."""
        try:
            import cv2
        except ImportError:
            logger.warning("opencv-python-headless not installed; built-in camera disabled")
            return False

        # Strategy 1: Try direct device access
        detected = _find_camera_device(self.device)
        if detected is not None:
            if detected != self.device:
                logger.info("Preferred device %d unavailable, using /dev/video%d", self.device, detected)
            self.device = detected
            self._cap = cv2.VideoCapture(self.device)
            if self._cap.isOpened():
                self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                self._cap.set(cv2.CAP_PROP_FPS, self.fps)

                # Warm-up: some USB cameras need a few frames before producing good output
                for _ in range(settings.camera_warmup_frames):
                    self._cap.read()

                self._source_name = f"/dev/video{self.device}"
                self._start_thread()
                actual_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                actual_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                logger.info("Camera started (device=%s, %dx%d)", self._source_name, actual_w, actual_h)
                return True
            else:
                self._cap.release()
                self._cap = None

        # Strategy 2: Read from MediaMTX RTSP output
        if self.rtsp_url:
            logger.info("Direct camera unavailable, trying RTSP: %s", self.rtsp_url)
            self._cap = cv2.VideoCapture(self.rtsp_url)
            if self._cap.isOpened():
                # Verify we can read a frame
                ret, _ = self._cap.read()
                if ret:
                    self._source_name = self.rtsp_url
                    self._start_thread()
                    actual_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    actual_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    logger.info("Camera started via RTSP (%s, %dx%d)", self.rtsp_url, actual_w, actual_h)
                    return True
                else:
                    logger.warning("RTSP opened but no frames from %s", self.rtsp_url)
                    self._cap.release()
                    self._cap = None
            else:
                logger.warning("Cannot open RTSP stream %s", self.rtsp_url)
                self._cap.release()
                self._cap = None

        logger.warning("No working camera source found (tried device + RTSP)")
        return False

    def _start_thread(self):
        """Start the background capture thread."""
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def _capture_loop(self):
        import cv2

        consecutive_failures = 0
        while self._running:
            ret, frame = self._cap.read()
            if ret:
                consecutive_failures = 0
                _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, settings.camera_jpeg_quality])
                with self._lock:
                    self._frame = jpeg.tobytes()
            else:
                consecutive_failures += 1
                if consecutive_failures > settings.camera_max_consecutive_failures:
                    logger.error("Camera lost — too many consecutive read failures (%s)", self._source_name)
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
        logger.info("Camera stopped (%s)", self._source_name)
