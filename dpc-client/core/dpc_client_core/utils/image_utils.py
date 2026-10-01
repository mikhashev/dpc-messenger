"""
Image processing utilities for thumbnail generation and dimension extraction.

Uses Pillow (PIL) for cross-platform image handling.
"""

import asyncio
import hashlib
import logging
import math
import threading
import warnings
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image, ImageOps
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


# --- One decision about an image on its way to a model ----------------------
# Called from every function on llm_manager.IMAGE_ENTRY_POINTS of kind
# provider_call or chat_entry; the test beside that list checks it.

_MB = 1024 * 1024
_ORIENTATION_TAG = 0x0112
_JPEG_QUALITY = 95
_CACHE_MAX_ENTRIES = 16
_CACHE_MAX_BYTES = 64 * _MB


# Decode memory is guarded by pixels, not bytes: a 60 MP JPEG can weigh ~1 MB.
# Pillow's own default is Image.MAX_IMAGE_PIXELS = 89_478_485: a
# DecompressionBombWarning above it, a DecompressionBombError above twice that.
# Mike kept Pillow's value (2026-10-01): up to ~179 MP is processed, above it
# the image is refused from its header before any decode. We name the numbers
# here instead of leaning on a library default that can be changed under us.
PILLOW_WARN_PIXELS = 89_478_485
MAX_DECODE_PIXELS = 2 * PILLOW_WARN_PIXELS  # ~179 MP, refused above this


class ImageTooLarge(ValueError):
    """The image weighs more than `[vision] max_image_size_mb` allows."""


class ImageTooManyPixels(ImageTooLarge):
    """The image declares more pixels than `MAX_DECODE_PIXELS` (~179 MP).

    A sibling of `ImageTooLarge` (so one `except ImageTooLarge` covers both),
    raised from the header before any decode. Unlike a decode failure it is never
    passed through: sending the original would hand the bomb to the model."""


def exceeds_byte_cap(size_bytes: int, max_bytes: Optional[int]) -> bool:
    """The one comparison behind `[vision] max_image_size_mb`.

    The P2P door (`service.py`), the gateway door (`gateway.py`) and
    `normalise_image` all ask this, so the byte cap is decided in one place.
    `None` means no cap is asked for here."""
    return max_bytes is not None and size_bytes > max_bytes


def byte_cap_refusal(size_bytes: int, max_bytes: int) -> str:
    """The sentence a refusal over the byte cap carries."""
    return f"Image too large ({round(size_bytes / _MB, 2)}MB). Max: {max_bytes / _MB:g}MB"


def _eight_bit(img: Image.Image) -> Image.Image:
    """A 16-bit, integer or float greyscale image as 8-bit "L".

    16-bit data (I;16, and the I that a 16-bit PNG may open as) is divided by
    256, which is what the format means; anything else is stretched between its
    own extrema, because it has no fixed range to divide by."""
    lo, hi = img.getextrema()
    if img.mode.startswith("I") and lo >= 0 and hi <= 65535:
        scale, offset = 1 / 256, 0.0
    elif hi > lo:
        scale, offset = 255.0 / (hi - lo), -lo * 255.0 / (hi - lo)
    else:
        scale, offset = 0.0, 0.0
    return img.point(lambda i: i * scale + offset).convert("L")


def _open_guarded(data: bytes) -> Image.Image:
    """Image.open, with the decode-memory guard read from the header.

    Image.open is lazy, so `.size` is known before any pixel is decoded. The
    89-179 MP band that makes Pillow warn is processed on purpose (Mike's call),
    so the warning is silenced here rather than left to spam the log
    (`catch_warnings` is process-wide, a brief race with another thread's
    warnings is accepted). Above `MAX_DECODE_PIXELS`, or when Pillow's own
    check raises DecompressionBombError, `ImageTooManyPixels` is raised."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            img = Image.open(BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise ImageTooManyPixels(
            f"Image has too many pixels (more than {MAX_DECODE_PIXELS / 1_000_000:.0f} MP): {exc}") from exc
    pixels = img.size[0] * img.size[1]
    if pixels > MAX_DECODE_PIXELS:
        raise ImageTooManyPixels(
            f"Image has too many pixels ({img.size[0]}x{img.size[1]} = {pixels / 1_000_000:.0f} MP; "
            f"max {MAX_DECODE_PIXELS / 1_000_000:.0f} MP)")
    return img


def _normalise(data: bytes, mime: str, pixel_limit: Optional[int]) -> Tuple[bytes, str, Optional[str]]:
    img = _open_guarded(data)
    source_format = (img.format or "").upper()
    frames = getattr(img, "n_frames", 1) or 1
    notes: List[str] = []

    orientation = img.getexif().get(_ORIENTATION_TAG, 1)
    turned = orientation in (2, 3, 4, 5, 6, 7, 8)
    width, height = img.size
    if orientation in (5, 6, 7, 8):
        width, height = height, width  # the limit is read on the upright picture
    needs_resize = bool(pixel_limit) and width * height > pixel_limit
    mode_fix = img.mode in ("CMYK", "I", "F", "P", "PA", "1") or img.mode.startswith("I;")
    if not (frames > 1 or turned or mode_fix or needs_resize):
        return data, mime, None

    if frames > 1:
        notes.append(f"frame 1 of {frames}")
    img.seek(0)
    img.load()
    if turned:
        img = ImageOps.exif_transpose(img)
        notes.append("turned upright by its EXIF orientation")
    mode = img.mode
    has_alpha = mode in ("RGBA", "LA", "PA") or (mode == "P" and "transparency" in img.info)
    if mode == "CMYK":
        img = img.convert("RGB")
        notes.append("CMYK converted to RGB")
    elif mode in ("I", "F") or mode.startswith("I;"):
        img = _eight_bit(img)
        notes.append("16-bit pixels reduced to 8-bit")
    elif mode in ("P", "PA"):
        img = img.convert("RGBA" if has_alpha else "RGB")
        notes.append(f"palette image converted to {img.mode}")
    elif mode == "1":
        img = img.convert("L")
        notes.append("1-bit image converted to greyscale")
    elif mode not in ("L", "LA", "RGB", "RGBA"):
        img = img.convert("RGBA" if has_alpha else "RGB")
        notes.append(f"{mode} converted to {img.mode}")

    if needs_resize:
        scale = math.sqrt(pixel_limit / (width * height))
        new_w, new_h = max(1, int(width * scale)), max(1, int(height * scale))
        old = img.size
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        notes.append(
            f"downscaled from {old[0]}x{old[1]} to {new_w}x{new_h} to fit the route's "
            f"{pixel_limit / 1_000_000:.1f} MP image limit"
        )

    buffer = BytesIO()
    if source_format == "JPEG" and img.mode in ("L", "RGB"):
        img.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
        out_mime = "image/jpeg"
    else:
        img.save(buffer, format="PNG")  # takes L/LA/RGB/RGBA and keeps alpha
        out_mime = "image/png"
    return buffer.getvalue(), out_mime, "; ".join(notes) or None


def normalise_image(
    data: bytes, mime: str, pixel_limit: Optional[int] = None, max_bytes: Optional[int] = None,
) -> Tuple[bytes, str, Optional[str]]:
    """(bytes, mime, note) for an image about to reach a model; note is None when untouched.

    Order: decode; EXIF orientation applied before any size is read; mode fixes
    (CMYK, 16-bit and palette; alpha kept only in PNG output); an animated
    image is cut to frame 1 and the note says so; then, when `pixel_limit` is
    set and the picture is larger, a LANCZOS downscale to fit with the aspect
    kept. `pixel_limit=None` means the limit is unknown: nothing is
    downscaled, and an ordinary JPEG/PNG leaves byte-identical (a picture that
    needs no change is not decoded past its header).

    The byte cap is judged last, on what would be sent: an image the pixel
    limit brought under it passes, one still over raises `ImageTooLarge`. A
    picture Pillow cannot decode comes back as it was, with a note saying so.
    One refusal is not passed through: a picture declaring more than
    `MAX_DECODE_PIXELS` raises `ImageTooManyPixels` (an `ImageTooLarge`)."""
    try:
        out, out_mime, note = _normalise(data, mime, pixel_limit)
    except ImageTooManyPixels:
        raise
    except Exception as exc:
        out, out_mime = data, mime
        note = f"could not be normalised ({type(exc).__name__}: {exc}); sent as it was"
    if exceeds_byte_cap(len(out), max_bytes):
        raise ImageTooLarge(byte_cap_refusal(len(out), max_bytes))
    return out, out_mime, note


_cache: "OrderedDict[Tuple[str, Optional[int], Optional[int]], Tuple[bytes, str, Optional[str]]]" = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()


def _normalise_cached(
    data: bytes, mime: str, pixel_limit: Optional[int], max_bytes: Optional[int],
) -> Tuple[bytes, str, Optional[str]]:
    """`normalise_image` behind a small LRU keyed by the sha256 of the original
    bytes and the limits: an agent's image block is sent again every tool round."""
    global _cache_bytes
    key = (hashlib.sha256(data).hexdigest(), pixel_limit, max_bytes)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    result = normalise_image(data, mime, pixel_limit, max_bytes)
    with _cache_lock:
        if key not in _cache:
            _cache[key] = result
            _cache_bytes += len(result[0])
            while _cache and (len(_cache) > _CACHE_MAX_ENTRIES or _cache_bytes > _CACHE_MAX_BYTES):
                _, evicted = _cache.popitem(last=False)
                _cache_bytes -= len(evicted[0])
    return result


async def normalise_image_async(
    data: bytes, mime: str, pixel_limit: Optional[int] = None, max_bytes: Optional[int] = None,
) -> Tuple[bytes, str, Optional[str]]:
    """`normalise_image` off the event loop (a 60 MP decode is 0.66 s of CPU), cached."""
    return await asyncio.to_thread(_normalise_cached, data, mime, pixel_limit, max_bytes)


def _raw_base64(value: str) -> bytes:
    return base64.b64decode(value.split(",", 1)[1] if value.startswith("data:") else value)


async def normalise_flat_images(
    images: List[Dict[str, Any]], pixel_limit: Optional[int], max_bytes: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The flat `images` list (`{base64, mime_type, ...}`) a vision call takes,
    and the notes: the same dicts where nothing changed, a copy where something
    did. An entry with no readable base64 is left for the provider to refuse."""
    out: List[Dict[str, Any]] = []
    notes: List[str] = []
    for index, img in enumerate(images):
        raw = img.get("base64") if isinstance(img, dict) else None
        try:
            data = _raw_base64(raw) if isinstance(raw, str) and raw else None
        except Exception:
            data = None
        if data is None:
            out.append(img)
            continue
        new, mime, note = await normalise_image_async(
            data, img.get("mime_type") or "image/png", pixel_limit, max_bytes)
        if note is None:
            out.append(img)
            continue
        out.append({**img, "base64": base64.b64encode(new).decode("ascii"), "mime_type": mime})
        notes.append(f"image {index + 1}: {note}" if len(images) > 1 else note)
    return out, notes


async def normalise_turn_images(
    messages: List[Dict[str, Any]], pixel_limit: Optional[int], max_bytes: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Image blocks in Anthropic-shaped turns, in a turn and inside a
    `tool_result`, normalised; the notes. `messages` itself is not mutated."""
    notes: List[str] = []

    async def block(b: Any) -> Any:
        if not isinstance(b, dict):
            return b
        if b.get("type") == "image":
            source = b.get("source")
            if isinstance(source, dict) and source.get("type") == "base64" \
                    and isinstance(source.get("data"), str):
                try:
                    data = _raw_base64(source["data"])
                except Exception:
                    return b
                new, mime, note = await normalise_image_async(
                    data, source.get("media_type") or "image/png", pixel_limit, max_bytes)
                if note is not None:
                    notes.append(note)
                    return {**b, "source": {**source, "data": base64.b64encode(new).decode("ascii"),
                                            "media_type": mime}}
            return b
        inner = b.get("content") if b.get("type") == "tool_result" else None
        if isinstance(inner, list):
            fixed = [await block(x) for x in inner]
            if any(f is not o for f, o in zip(fixed, inner)):
                return {**b, "content": fixed}
        return b

    out: List[Dict[str, Any]] = []
    for turn in messages:
        content = turn.get("content") if isinstance(turn, dict) else None
        if isinstance(content, list):
            fixed = [await block(x) for x in content]
            if any(f is not o for f, o in zip(fixed, content)):
                turn = {**turn, "content": fixed}
        out.append(turn)
    return out, notes
