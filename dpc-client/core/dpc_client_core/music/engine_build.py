"""`build.json`: what an engine binary was built from, read for the provenance record's `build` field.

The engines report no build on `/health` or `/props`, and DPC cannot read a compiler or a CUDA
architecture out of a binary, so the person who builds the engine writes a small file next to it
(`tools/music/write_engine_build_json.py <build_dir> <source_dir>`; Mike 2026-11-26). The file is
`dpc.engine-build/1`:

    {"schema": "dpc.engine-build/1", "engine": "acestep.cpp", "source_commit": "973517be...",
     "source_dirty": false, "cuda_architectures": "120a-real", "cuda_version": "13.3",
     "compiler": "MSVC 19.44.35228", "build_type": "Release", "built_at": "2026-11-26T10:00:00Z"}

`cuda_architectures`, `cuda_version`, `compiler` and `build_type` may be null (a CPU build has no
CUDA). `read_engine_build` finds the file beside the binary (or in one of the two folders above it,
where a multi-config generator keeps `bin/` or `Release/`), reads at most `MAX_FILE_BYTES`, and
returns the **validated** fields only: unknown keys are dropped, every string is checked against a
strict shape (no path separators, so rule 3 of the record holds: no local path inside a file that
may be shared), and the whole is small. A file that is missing or does not validate gives no build
and a note that names the file; the record then keeps `build` as `not_stored`.

`build` is not part of any id (`SUBSTRATE_KEYS` does not name it), so filling it moves no
`genome_sha256` or `render_sha256`.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA = "dpc.engine-build/1"
FILE_NAME = "build.json"
MAX_FILE_BYTES = 8 * 1024
MAX_BUILD_BYTES = 2 * 1024      # the validated object, serialized

_ENGINE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_ARCH = re.compile(r"^[0-9A-Za-z;,._-]{1,64}$")
_VERSION = re.compile(r"^[0-9A-Za-z._+-]{1,32}$")
_TYPE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")
_COMPILER = re.compile(r"^[^\\/\x00-\x1f\x7f]{1,120}$")   # a name and a version, never a path

_REQUIRED = ("engine", "source_commit", "source_dirty", "built_at")
_OPTIONAL = {"cuda_architectures": _ARCH, "cuda_version": _VERSION, "compiler": _COMPILER, "build_type": _TYPE}


def validate_build(raw: Any, *, engine: Optional[str] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """`(build, None)` for a valid `dpc.engine-build/1` object, else `(None, why)`. `engine`, when
    given, must equal the file's `engine`."""
    if not isinstance(raw, Mapping):
        return None, "not a JSON object"
    if raw.get("schema") != SCHEMA:
        return None, f"schema is not {SCHEMA}"
    out: Dict[str, Any] = {"schema": SCHEMA}
    for key in _REQUIRED:
        if key not in raw:
            return None, f"{key} is missing"
    if not isinstance(raw["engine"], str) or not _ENGINE.match(raw["engine"]):
        return None, "engine is not a plain name"
    if engine is not None and raw["engine"] != engine:
        return None, f"the file is for {raw['engine']}, not {engine}"
    if not isinstance(raw["source_commit"], str) or not _COMMIT.match(raw["source_commit"]):
        return None, "source_commit is not a lowercase hex commit id"
    if not isinstance(raw["source_dirty"], bool):
        return None, "source_dirty is not true or false"
    if not isinstance(raw["built_at"], str) or not _TIME.match(raw["built_at"]):
        return None, "built_at is not an ISO 8601 UTC time"
    out.update(engine=raw["engine"], source_commit=raw["source_commit"], source_dirty=raw["source_dirty"],
               built_at=raw["built_at"])
    for key, shape in _OPTIONAL.items():
        value = raw.get(key)
        if value is None:
            out[key] = None
        elif isinstance(value, str) and shape.match(value):
            out[key] = value
        else:
            return None, f"{key} is not a plain value"
    if len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_BUILD_BYTES:
        return None, "too big"
    return out, None


def _candidates(binary: Path):
    for folder in binary.parents[:3]:
        yield folder / FILE_NAME


def read_engine_build(binary: Path, *, engine: Optional[str] = None) -> Dict[str, Any]:
    """`{"build": {...}}` when a valid `build.json` sits beside `binary`, else
    `{"build_note": "<why, naming the file>"}`. Never raises; blocking (a few `stat`s and one small read)."""
    binary = Path(binary)
    for path in _candidates(binary):
        try:
            with open(path, "rb") as f:
                data = f.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return {"build_note": f"{FILE_NAME} next to {binary.name} could not be read: {type(exc).__name__}"}
        if len(data) > MAX_FILE_BYTES:
            return {"build_note": f"{FILE_NAME} next to {binary.name} is over {MAX_FILE_BYTES} bytes; ignored"}
        try:
            raw = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"build_note": f"{FILE_NAME} next to {binary.name} is not valid JSON; ignored"}
        build, why = validate_build(raw, engine=engine)
        if build is None:
            logger.warning("music engine: %s next to %s ignored: %s", FILE_NAME, binary.name, why)
            return {"build_note": f"{FILE_NAME} next to {binary.name} is not a valid {SCHEMA} ({why}); ignored"}
        return {"build": build}
    return {"build_note": (f"no {FILE_NAME} next to the engine binary {binary.name}: the engine answers /health and "
                           f"/props with no build, compiler or CUDA architecture and DPC does not read them from the "
                           f"binary, so the build is recorded only when tools/music/write_engine_build_json.py wrote "
                           f"{FILE_NAME} after building; the binary is named by sha256 in engine_binaries")}
