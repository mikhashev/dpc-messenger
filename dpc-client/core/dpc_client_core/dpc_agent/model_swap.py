"""Model swap detection and rebuild UX (ADR-010, MEM-X.2).

Detects embedding model mismatch at startup and prompts for rebuild.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Optional

from .faiss_index import FaissIndex

log = logging.getLogger(__name__)


def detect_model_mismatch(
    index_dir: pathlib.Path,
    current_model: str,
) -> Optional[dict]:
    """Check if saved index was built with a different model.

    Returns mismatch info dict if rebuild needed, None if OK.
    """
    idx = FaissIndex(index_dir)
    if not idx.load():
        return None

    if not idx.needs_rebuild(current_model):
        return None

    return {
        "needs_rebuild": True,
        "saved_model": idx._header.model_name,
        "current_model": current_model,
        "chunk_count": idx._header.chunk_count,
        "message": (
            f"Embedding model changed: {idx._header.model_name} → {current_model}. "
            f"Index has {idx._header.chunk_count} chunks. Rebuild required."
        ),
    }


def rebuild_prompt_message(mismatch: dict) -> str:
    """Format user-facing prompt for model swap rebuild."""
    return (
        f"Model changed from {mismatch['saved_model']} to {mismatch['current_model']}. "
        f"Rebuild index? ({mismatch['chunk_count']} chunks to re-embed)"
    )
