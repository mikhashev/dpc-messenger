"""Text extraction from files for embedding pipeline (ADR-010, MEM-3.2)."""

from __future__ import annotations

import json
import pathlib
from typing import Optional

_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp",
    ".pdf", ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".mp3", ".mp4", ".wav", ".webm", ".ogg", ".flac",
    ".woff", ".woff2", ".ttf", ".eot",
    ".faiss", ".npy", ".npz", ".pt", ".onnx",
})


def is_binary(path: pathlib.Path) -> bool:
    return path.suffix.lower() in _BINARY_EXTENSIONS


def extract_text(path: pathlib.Path, max_chars: int = 100_000) -> Optional[str]:
    """Extract text content from a file. Returns None for binary/unreadable files."""
    if is_binary(path):
        return None

    suffix = path.suffix.lower()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        return None

    if suffix == ".json":
        return _extract_json_values(raw)

    return raw


def _extract_json_values(raw: str) -> str:
    """Walk JSON and concatenate string values."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw

    parts: list[str] = []
    _walk_json(data, parts)
    return "\n".join(parts)


def _walk_json(obj, parts: list[str]) -> None:
    if isinstance(obj, str) and len(obj) > 2:
        parts.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_json(v, parts)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json(item, parts)
