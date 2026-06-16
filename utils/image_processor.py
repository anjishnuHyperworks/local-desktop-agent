"""
Phase 4: Image Processor

Resizes raw screenshot bytes for API transmission and provides the coordinate
helper that maps the model's resized-image pixel coordinates back onto physical
display pixels (by undoing the resize scale).

Design decisions:
- Never upscale: if the image is already small, return it as-is.
- Aspect ratio is always preserved; the longer dimension is capped at MAX_IMAGE_SIZE.
- ProcessedImage carries both original and resized dimensions so the caller can
  compute scale factors without re-opening the buffer.
- Coordinate normalisation lives here (not in InputEmulator) so Phase 4 tests
  can validate the maths without instantiating Win32-dependent objects.
"""

import io
import logging
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, UnidentifiedImageError

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class ProcessedImage:
    """
    Holds the resized JPEG bytes together with both the original and resized
    dimensions, enabling callers to reconstruct scale factors on demand.
    """

    image_bytes: bytes
    original_width: int
    original_height: int
    resized_width: int
    resized_height: int

    @property
    def was_resized(self) -> bool:
        return (
            self.resized_width != self.original_width
            or self.resized_height != self.original_height
        )

    @property
    def scale_x(self) -> float:
        """Fraction: resized_width / original_width (≤ 1.0)."""
        if self.original_width == 0:
            return 1.0
        return self.resized_width / self.original_width

    @property
    def scale_y(self) -> float:
        """Fraction: resized_height / original_height (≤ 1.0)."""
        if self.original_height == 0:
            return 1.0
        return self.resized_height / self.original_height


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------

class ImageProcessor:
    """
    Stateless utility for preparing screenshots for API submission.

    Thread-safe: all methods are pure functions operating on their arguments.
    """

    def __init__(self, max_dimension: int = config.MAX_IMAGE_SIZE) -> None:
        """
        Args:
            max_dimension: Longest allowed side in pixels after resizing.
                           Defaults to config.MAX_IMAGE_SIZE.
        """
        self._max_dimension = max_dimension
        logger.debug(
            "ImageProcessor ready — max_dimension=%d", self._max_dimension
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resize_for_grok(self, image_bytes: bytes) -> ProcessedImage:
        """
        Decode, optionally downscale, and re-encode a screenshot as JPEG.

        - Preserves aspect ratio.
        - Never upscales (returns original dimensions if image is already small).
        - Raises ValueError on corrupt / unreadable input.

        Args:
            image_bytes: Raw image data (any PIL-supported format).

        Returns:
            ProcessedImage with JPEG bytes and both sets of dimensions.

        Raises:
            ValueError: If the bytes cannot be decoded as an image.
        """
        logger.debug(
            "resize_for_grok: input size=%d bytes", len(image_bytes)
        )

        img = self._decode(image_bytes)
        original_width, original_height = img.size
        logger.debug(
            "Decoded image: %dx%d", original_width, original_height
        )

        # Determine target size.
        target_width, target_height = self._compute_target_size(
            original_width, original_height
        )

        # Downscale only if necessary.
        if target_width < original_width or target_height < original_height:
            logger.info(
                "Resizing screenshot: %dx%d → %dx%d",
                original_width, original_height,
                target_width, target_height,
            )
            img = img.resize(
                (target_width, target_height),
                Image.Resampling.LANCZOS,
            )
        else:
            logger.debug(
                "Screenshot already within limits (%dx%d ≤ %d px); skipping resize.",
                original_width, original_height, self._max_dimension,
            )
            target_width, target_height = original_width, original_height

        # Ensure RGB (no alpha channel in JPEG).
        if img.mode != "RGB":
            logger.debug("Converting image from %s to RGB", img.mode)
            img = img.convert("RGB")

        jpeg_bytes = self._encode_jpeg(img)
        logger.debug(
            "Encoded JPEG: %d bytes (quality=%d)", len(jpeg_bytes), config.JPEG_QUALITY
        )

        return ProcessedImage(
            image_bytes=jpeg_bytes,
            original_width=original_width,
            original_height=original_height,
            resized_width=target_width,
            resized_height=target_height,
        )

    # ------------------------------------------------------------------
    # Coordinate normalisation (stateless helper, no Win32 dependency)
    # ------------------------------------------------------------------

    @staticmethod
    def image_to_physical(
        image_x: int,
        image_y: int,
        scale_x: float,
        scale_y: float,
    ) -> tuple[int, int]:
        """
        Convert a point expressed in *resized image* pixels back to physical
        screen pixels by undoing the resize.

        The model reasons over the resized image it was sent, so its coordinates
        live in that image's pixel space. Dividing by the resize scale recovers
        the original (physical, because the process is DPI aware) screen pixel.

            physical_x = image_x / scale_x      where scale_x = resized_w / original_w
            physical_y = image_y / scale_y

        When the screenshot was not resized, scale_x == scale_y == 1.0 and this
        is the identity mapping.

        Args:
            image_x: X in resized-image pixels (0 .. resized_width).
            image_y: Y in resized-image pixels (0 .. resized_height).
            scale_x: ProcessedImage.scale_x (resized_width / original_width).
            scale_y: ProcessedImage.scale_y (resized_height / original_height).

        Returns:
            (physical_x, physical_y), un-clamped. The caller (InputEmulator)
            clamps to the live display bounds.
        """
        if scale_x <= 0:
            scale_x = 1.0
        if scale_y <= 0:
            scale_y = 1.0

        physical_x = int(image_x / scale_x)
        physical_y = int(image_y / scale_y)

        logger.debug(
            "image_to_physical: image=(%d,%d) scale=(%.4f,%.4f) → physical=(%d,%d)",
            image_x, image_y, scale_x, scale_y, physical_x, physical_y,
        )
        return physical_x, physical_y

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _decode(self, image_bytes: bytes) -> Image.Image:
        """Decode raw bytes into a PIL Image, raising ValueError on failure."""
        try:
            buf = io.BytesIO(image_bytes)
            img = Image.open(buf)
            img.load()   # Force decode so corrupt images raise here.
            return img
        except UnidentifiedImageError as exc:
            logger.error("Cannot identify image format: %s", exc)
            raise ValueError(f"Unrecognised image format: {exc}") from exc
        except Exception as exc:
            logger.error("Failed to decode image bytes: %s", exc)
            raise ValueError(f"Image decode error: {exc}") from exc

    def _compute_target_size(
        self, width: int, height: int
    ) -> tuple[int, int]:
        """
        Return the target (width, height) that fits within max_dimension while
        preserving the original aspect ratio.  Never upscales.
        """
        max_dim = self._max_dimension
        if width <= max_dim and height <= max_dim:
            return width, height

        if width >= height:
            scale = max_dim / width
        else:
            scale = max_dim / height

        return int(width * scale), int(height * scale)

    @staticmethod
    def _encode_jpeg(img: Image.Image) -> bytes:
        """Encode a PIL Image to JPEG bytes using the configured quality."""
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=config.JPEG_QUALITY)
        return buf.getvalue()
