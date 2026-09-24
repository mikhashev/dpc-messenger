"""Cache-presence check for the embedding model (ADR-010, MEM-3.9).

The download-consent event family and per-model status live in
`model_download_service.ModelDownloadService`; this module keeps only the
cheap cache check every caller here needs before deciding whether to ask.
"""

from __future__ import annotations

import pathlib

DEFAULT_MODEL = "BAAI/bge-m3"


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
