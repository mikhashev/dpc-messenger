"""Cache-presence check for the embedding model (ADR-010, MEM-3.9).

The download-consent event family and per-model status live in
`model_download_service.ModelDownloadService`; the cache check itself lives
in `providers/model_sizes.is_model_cached` (S148-style finding, 2026-09-24:
two modules ran the same `try_to_load_from_cache` check and could drift).
This module keeps only a thin re-export under the name callers here already
use, so `agent.py` and `agent_manager.py` do not need to change their import.
"""

from __future__ import annotations

from ..providers import model_sizes

DEFAULT_MODEL = "BAAI/bge-m3"


def is_model_downloaded(model_name: str = DEFAULT_MODEL) -> bool:
    """Check if the embedding model is already cached locally.

    Calls through the module rather than importing the function by name, so a
    test (or a future caller) that patches `model_sizes.is_model_cached` sees
    the same effect here as everywhere else that checks the cache.
    """
    return model_sizes.is_model_cached(model_name)
