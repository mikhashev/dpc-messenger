"""
Image processing utilities for thumbnail generation and dimension extraction.

Uses Pillow (PIL) for cross-platform image handling.
"""

import logging
from pathlib import Path
from typing import Dict
from PIL import Image
from io import BytesIO
import base64

logger = logging.getLogger(__name__)

MAX_THUMBNAIL_SIZE = (200, 200)  # Max dimensions for thumbnails
THUMBNAIL_QUALITY = 85  # JPEG quality (1-100)
MAX_THUMBNAIL_BYTES = 50 * 1024  # 50KB max thumbnail size


def generate_thumbnail(image_path: Path) -> str:
    """
    Generate base64-encoded JPEG thumbnail (max 200x200, 50KB).

    Args:
        image_path: Path to source image

    Returns:
        str: Data URL (data:image/jpeg;base64,...)

    Raises:
        ValueError: If image cannot be processed
    """
    try:
        with Image.open(image_path) as img:
            # Convert to RGB (handle RGBA, grayscale, etc.)
            if img.mode in ("RGBA", "LA", "P"):
                # Create white background for transparency
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            # Resize to thumbnail (preserving aspect ratio)
            img.thumbnail(MAX_THUMBNAIL_SIZE, Image.Resampling.LANCZOS)

            # Encode to JPEG in memory
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
            thumbnail_bytes = buffer.getvalue()

            # Check size (reduce quality if too large)
            if len(thumbnail_bytes) > MAX_THUMBNAIL_BYTES:
                logger.warning(f"Thumbnail too large ({len(thumbnail_bytes)} bytes), reducing quality")
                buffer = BytesIO()
                img.save(buffer, format="JPEG", quality=70, optimize=True)
                thumbnail_bytes = buffer.getvalue()

            # Encode to base64 data URL
            base64_data = base64.b64encode(thumbnail_bytes).decode("utf-8")
            return f"data:image/jpeg;base64,{base64_data}"

    except Exception as e:
        logger.error(f"Thumbnail generation failed for {image_path}: {e}", exc_info=True)
        raise ValueError(f"Cannot generate thumbnail: {e}")


def get_image_dimensions(image_path: Path) -> Dict[str, int]:
    """
    Extract image dimensions (width, height).

    Args:
        image_path: Path to source image

    Returns:
        dict: {"width": 1920, "height": 1080}
    """
    try:
        with Image.open(image_path) as img:
            return {"width": img.width, "height": img.height}
    except Exception as e:
        logger.error(f"Cannot read dimensions for {image_path}: {e}")
        return {"width": 0, "height": 0}


def validate_image_format(image_path: Path) -> bool:
    """
    Validate image format (PNG, JPEG, WebP, GIF).

    Args:
        image_path: Path to source image

    Returns:
        bool: True if valid image format
    """
    try:
        with Image.open(image_path) as img:
            return img.format.lower() in ("png", "jpeg", "jpg", "webp", "gif")
    except Exception:
        return False
