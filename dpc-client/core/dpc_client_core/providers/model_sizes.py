# dpc_client_core/providers/model_sizes.py
"""Measured download sizes for models this app may ask the user to fetch.

Lives beside `providers/base.py` (the home of `ModelNotCachedError`, which
carries the number this module produces) rather than under `dpc_agent/` —
Whisper is not an agent concern, and one lookup serves both.

`_MEASURED_SIZES` rows are hand-measured and dated; `model_size_bytes` falls
back to a live HfApi file-metadata sum, and to `(None, None)` — "size
unknown" — rather than raising, because a download-consent dialog needs an
answer, not a traceback, when huggingface_hub is offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SizeRow:
    """One measured row: the byte count and the date it was taken (S148-style
    finding, 2026-09-24) — a number with no date attached cannot be told apart
    from one nobody has re-checked since the model itself changed shape."""
    size_bytes: int
    measured_on: Optional[str]  # ISO date, or None for a row with no date on record


# (model_name, revision) -> row. A caller that pins no revision gets the
# measured row for the name: the loaders here do not pin one either.
_MEASURED_SIZES: Dict[Tuple[str, Optional[str]], _SizeRow] = {
    # Measured 2026-09-24: fresh cache, DISABLE_SAFETENSORS_CONVERSION=true
    # (see dpc_agent/memory.py), pytorch_model.bin + tokenizer files — the
    # files EmbeddingProvider actually loads.
    ("BAAI/bge-m3", "5617a9f61b028005a4858fdac845db406aefb181"):
        _SizeRow(2_293_331_663, "2026-09-24"),

    # Summed 2026-09-24 via HfApi().model_info(repo, revision, files_metadata=True)
    # over the files present in this machine's cached snapshot dir
    # (~/.cache/huggingface/hub/models--openai--whisper-large-v3-turbo/snapshots/…):
    # model.safetensors + config/generation_config/preprocessor/tokenizer/vocab/
    # merges/normalizer/special_tokens files (what AutoModelForSpeechSeq2Seq +
    # AutoProcessor pull) — not README.md or .gitattributes. Not a fresh-download
    # measurement, a sum of the API's reported file sizes.
    ("openai/whisper-large-v3-turbo", "41f01f3fe87f28c78e2fbf8b568835947dd65ed9"):
        _SizeRow(1_622_443_339, "2026-09-24"),
}


def hf_cache_path() -> str:
    """The OS-correct HF hub cache dir (`HF_HOME`/`HF_HUB_CACHE`-aware)."""
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        return HF_HUB_CACHE
    except Exception:
        import os
        return os.path.expanduser("~/.cache/huggingface/hub")


def is_model_cached(model_name: str) -> bool:
    """Best-effort cache check: is `model_name` already on disk?"""
    try:
        from huggingface_hub import try_to_load_from_cache
        return try_to_load_from_cache(model_name, "config.json") is not None
    except Exception:
        import os
        cache_dir = hf_cache_path()
        return os.path.isdir(os.path.join(cache_dir, f"models--{model_name.replace('/', '--')}"))


def model_size_bytes(
    model_name: str, revision: Optional[str] = None
) -> Tuple[Optional[int], Optional[str]]:
    """`(size_bytes, source)` for `model_name`, `source` one of "measured" /
    "hf_api" / None.

    A table row wins (by exact revision, or by name when the caller gives
    none). A miss falls back to the HfApi sum of every file in the repo, which
    overstates a model that ships more than one format; offline, `(None, None)`
    — the dialog payload must still be buildable.
    """
    key = (model_name, revision)
    if key in _MEASURED_SIZES:
        return _MEASURED_SIZES[key].size_bytes, "measured"
    if revision is None:
        for (name, _rev), row in _MEASURED_SIZES.items():
            if name == model_name:
                return row.size_bytes, "measured"
    try:
        from huggingface_hub import HfApi
        api = HfApi()
        info = api.model_info(model_name, revision=revision, files_metadata=True)
        total = sum((f.size or 0) for f in (info.siblings or []))
        if total:
            return total, "hf_api"
    except Exception as e:
        logger.debug("model_size_bytes: HfApi lookup failed for %s: %s", model_name, e)
    return None, None


def model_size_measured_on(
    model_name: str, revision: Optional[str] = None
) -> Optional[str]:
    """The ISO date the table row for `model_name` was measured, or None.

    None covers two cases a caller does not need to tell apart: no table row
    (the size came from the live HfApi sum, or is unknown), and a table row
    that carries no date. Mirrors `model_size_bytes`'s own lookup order —
    exact revision first, then the row for the name — so the two never
    disagree about which row answered.
    """
    key = (model_name, revision)
    if key in _MEASURED_SIZES:
        return _MEASURED_SIZES[key].measured_on
    if revision is None:
        for (name, _rev), row in _MEASURED_SIZES.items():
            if name == model_name:
                return row.measured_on
    return None


def known_model_names() -> Tuple[str, ...]:
    """Every model name this table has a measured row for, name only (no
    revision) — the allow-list `ModelDownloadService.download_model` checks
    against needs the name a caller passes, not the pinned revision the table
    also carries.
    """
    return tuple(sorted({name for name, _rev in _MEASURED_SIZES}))
