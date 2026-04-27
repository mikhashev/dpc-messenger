"""First-use embedding model download UX (ADR-010, MEM-3.9).

Detects if model is not yet downloaded and provides notification.
Follows Whisper download pattern from voice_service.py.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_MODEL = "aapot/bge-m3-onnx"
ESTIMATED_SIZE_MB = 1100


def is_model_downloaded(model_name: str = DEFAULT_MODEL) -> bool:
    """Check if the embedding model is already cached locally."""
    try:
        from huggingface_hub import try_to_load_from_cache
        result = try_to_load_from_cache(model_name, "config.json")
        return result is not None and not isinstance(result, type(None))
    except ImportError:
        cache_path = _default_cache_path() / f"models--{model_name.replace('/', '--')}"
        return cache_path.exists()
    except Exception:
        return False


def _default_cache_path() -> pathlib.Path:
    import os
    return pathlib.Path(os.environ.get("HF_HOME", pathlib.Path.home() / ".cache" / "huggingface")) / "hub"


def download_status_message(model_name: str = DEFAULT_MODEL) -> Optional[str]:
    """Return a user-facing message if model needs downloading, or None if ready."""
    if is_model_downloaded(model_name):
        return None
    return (
        f"DPC needs embedding model ({ESTIMATED_SIZE_MB}MB) for Active Recall memory. "
        f"Without it, memory search is unavailable. "
        f"Model will download on first use."
    )


def notify_download_needed(model_name: str = DEFAULT_MODEL) -> dict:
    """Return structured notification for UI/WebSocket broadcast."""
    msg = download_status_message(model_name)
    if msg is None:
        return {"needed": False}
    return {
        "needed": True,
        "model_name": model_name,
        "estimated_size_mb": ESTIMATED_SIZE_MB,
        "message": msg,
    }
