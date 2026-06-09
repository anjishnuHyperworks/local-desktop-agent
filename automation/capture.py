"""
Phase 3: Screen Capture

Captures the primary monitor using mss and exposes the result as a PIL Image,
JPEG bytes, or a Base64-encoded UTF-8 string ready for API payloads.
"""

import base64
import io
import logging
from typing import Optional

import mss
import mss.tools
from PIL import Image

import config

logger = logging.getLogger(__name__)


class ScreenCapture:
    """
    Thin wrapper around mss that always targets the primary monitor (index 1 in
    mss, which corresponds to physical Monitor 0).

    All public methods open a fresh mss context per call so the object is safe
    to instantiate once and reuse across threads (each call is independent).
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def capture_image(self) -> Image.Image:
        """Capture the primary monitor and return a PIL Image (RGB)."""
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]   # index 1 = primary monitor
                logger.debug(
                    "Capturing monitor: left=%d top=%d width=%d height=%d",
                    monitor["left"],
                    monitor["top"],
                    monitor["width"],
                    monitor["height"],
                )
                raw = sct.grab(monitor)
                img = Image.frombytes("RGB", raw.size, raw.rgb)
                logger.debug(
                    "Captured image size: %dx%d", img.width, img.height
                )
                return img
        except Exception:
            logger.exception("capture_image failed")
            raise

    def capture_jpeg_bytes(self) -> bytes:
        """Return the primary-monitor screenshot as JPEG bytes."""
        try:
            img = self.capture_image()
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=config.JPEG_QUALITY)
            data = buf.getvalue()
            logger.debug(
                "JPEG payload size: %d bytes (quality=%d)",
                len(data),
                config.JPEG_QUALITY,
            )
            return data
        except Exception:
            logger.exception("capture_jpeg_bytes failed")
            raise

    def capture_base64(self) -> str:
        """Return the primary-monitor screenshot as a Base64 UTF-8 string."""
        try:
            jpeg = self.capture_jpeg_bytes()
            encoded = base64.b64encode(jpeg).decode("utf-8")
            logger.debug("Base64 payload length: %d chars", len(encoded))
            return encoded
        except Exception:
            logger.exception("capture_base64 failed")
            raise

    def get_screen_dimensions(self) -> tuple[int, int]:
        """Return (width, height) of the primary monitor in physical pixels."""
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                width: int = monitor["width"]
                height: int = monitor["height"]
                logger.debug(
                    "Primary monitor dimensions: %dx%d", width, height
                )
                return width, height
        except Exception:
            logger.exception("get_screen_dimensions failed")
            raise
