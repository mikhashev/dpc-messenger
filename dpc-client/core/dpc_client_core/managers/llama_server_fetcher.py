"""
Llama Server Fetcher — pinned llama.cpp binaries, fetched on first use.

ADR-040 route (b2): the provider runs a DPC-owned `llama-server` child process,
and the binary for it comes from a pinned upstream release — never «latest»,
never an Ollama install. First use downloads the release assets into
`~/.dpc/bin/llama.cpp/<tag>/<platform>/`, verifying each against a sha256
pinned in the table below; the digests are the `digest` field of the GitHub
release API for the tag. A configured `binary_path` always wins over the
fetch, so an operator-supplied build is never silently replaced.

The pattern follows `voice_service.download_whisper_model` (first-use fetch,
progress events) minus the HuggingFace delegation: these are plain zip and
tar.gz assets with known digests, so the fetch streams, hashes while writing,
and refuses to leave a partial install behind.
"""

import hashlib
import json
import logging
import os
import platform as _platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

LLAMA_CPP_TAG = "b10566"
LLAMA_CPP_RELEASE_BASE = "https://github.com/ggml-org/llama.cpp/releases/download"

DPC_HOME = Path(os.environ.get("DPC_HOME", Path.home() / ".dpc"))

# One row per asset of the pin. `sha256` comes from the release API `digest`
# field; `size` is the byte length the progress report is computed against.
#
# The tag is chosen from upstream's stable channel rather than by hand. From
# 2026-08-21 llama.cpp publishes `vX.Y.Z` releases beside the `b[NUM]` ones and
# says which is which: `vX.Y.Z` is "stable, slower release cadence, recommended
# for downstream distribution", `b[NUM]` is "bleeding edge ... recommended for
# developers". We are downstream, and until now we sat on a nightly tag picked
# by hand.
#
# The versioned release carries no binaries — its only asset is
# `nightly-tag.txt`, holding the build tag it corresponds to. So the assets below
# still come from a `b` tag; the tag is just no longer ours to guess. To move the
# pin: read `nightly-tag.txt` from the newest `vX.Y.Z`, then take that tag's
# `digest` and `size` from the release API.
#
# b10566 is what v0.2.0 named (2026-08-21). Do not follow `releases/latest`: it
# now returns the versioned release, whose only asset is that text file.
PLATFORM_ASSETS: Dict[str, List[Dict[str, Any]]] = {
    "win-cuda-13.3-x64": [
        {
            "name": "llama-b10566-bin-win-cuda-13.3-x64.zip",
            "sha256": "c3e2336c1427e8bd7b5beb3c8618d2f7a268bc5fb6ec3f28c1e06cdb78d2e80a",
            "size": 146_890_631,
        },
        {
            "name": "cudart-llama-bin-win-cuda-13.3-x64.zip",
            "sha256": "1462a050eb4c684921ba51dcc4cc488a036674c3e73e9945ee705b854808d03e",
            "size": 390_970_417,
        },
    ],
    "macos-arm64": [
        {
            "name": "llama-b10566-bin-macos-arm64.tar.gz",
            "sha256": "533f546dab2ce2f8e29ce3070f26acc55acc59528e177f2cd0d52b7f69b44f50",
            "size": 11_095_544,
        },
    ],
    "ubuntu-x64": [
        {
            "name": "llama-b10566-bin-ubuntu-x64.tar.gz",
            "sha256": "0c34561d623299c113e46f9fdd97bff5b219b25554243a21a243aebc81253ea1",
            "size": 16_677_356,
        },
    ],
}


def platform_tag() -> str:
    """The pin's asset family for this machine."""
    if sys.platform == "win32":
        return "win-cuda-13.3-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if _platform.machine() == "arm64" else "ubuntu-x64"
    return "ubuntu-x64"


def server_binary_name() -> str:
    return "llama-server.exe" if sys.platform == "win32" else "llama-server"


def install_root(tag: str = LLAMA_CPP_TAG, plat: Optional[str] = None) -> Path:
    """Where a fetched pin lives: ~/.dpc/bin/llama.cpp/<tag>/<platform>/."""
    return DPC_HOME / "bin" / "llama.cpp" / tag / (plat or platform_tag())


def _marker_path(root: Path) -> Path:
    return root / ".dpc-pin.json"


def resolve_binary(config: Dict[str, Any]) -> Optional[Path]:
    """Find a usable llama-server without touching the network.

    A configured `binary_path` wins; if it is set but missing that is an
    error the operator must see, not a reason to fetch something else.
    Otherwise an installed pin counts only when its marker matches the tag
    and platform this build pins.
    """
    configured = config.get("binary_path")
    if configured:
        path = Path(configured)
        if not path.is_file():
            raise FileNotFoundError(
                f"binary_path is set to {path} but there is no file there; "
                "fix the alias config or clear binary_path to fetch the pin"
            )
        return path

    root = install_root()
    marker = _marker_path(root)
    if marker.is_file() and (root / server_binary_name()).is_file():
        try:
            pinned = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if pinned.get("tag") == LLAMA_CPP_TAG:
            return root / server_binary_name()
    return None


def find_cuda_backend(binary_dir: Path) -> Optional[Path]:
    """Locate ggml-cuda.dll / libggml-cuda.so for GGML_BACKEND_PATH.

    The 2026-08-19 incident: a standalone server silently ran on the CPU for
    ten minutes because the binary could not find its CUDA backend. Pointing
    GGML_BACKEND_PATH at the backend that ships beside the fetched binary
    removes that failure mode.
    """
    if sys.platform == "win32":
        candidates = sorted(binary_dir.rglob("ggml-cuda.dll"))
    else:
        candidates = sorted(
            list(binary_dir.rglob("libggml-cuda.so")) + list(binary_dir.rglob("*ggml-cuda*"))
        )
    return candidates[0] if candidates else None


def _download_asset(
    asset: Dict[str, Any], dest: Path, progress: Optional[Callable[[int, int], None]]
) -> None:
    """Stream one asset to dest.part, hashing as it lands; verify before rename."""
    url = f"{LLAMA_CPP_RELEASE_BASE}/{LLAMA_CPP_TAG}/{asset['name']}"
    part = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha256()
    received = 0
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(part, "wb") as out:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress:
                    progress(received, asset["size"])
        actual = digest.hexdigest()
        if actual != asset["sha256"]:
            raise ValueError(
                f"{asset['name']}: sha256 mismatch — expected {asset['sha256']}, got {actual}; "
                "the download is not the bytes we pinned"
            )
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def _extract(archive: Path, dest: Path) -> None:
    """Extract with zip-slip / tar-slip refused, not just hoped away."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                target = (dest / member.filename).resolve()
                if not target.is_relative_to(dest.resolve()):
                    raise ValueError(f"archive member escapes install dir: {member.filename}")
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")


def install_pin(progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Fetch and install the pinned release; atomic enough to leave no half state."""
    plat = platform_tag()
    assets = PLATFORM_ASSETS[plat]
    root = install_root(plat=plat)
    if (root / server_binary_name()).is_file() and _marker_path(root).is_file():
        return root / server_binary_name()

    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="llama-pin-") as tmp:
        tmp_dir = Path(tmp)
        staging = root.with_name(root.name + ".staging")
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            for asset in assets:
                archive = tmp_dir / asset["name"]
                logger.info("Fetching %s (%d bytes)", asset["name"], asset["size"])
                _download_asset(asset, archive, progress)
                _extract(archive, staging)
            if not (staging / server_binary_name()).is_file():
                # Archives lay out files differently than this pin expects.
                found = sorted(staging.rglob(server_binary_name()))
                if not found:
                    raise ValueError(
                        f"{plat} assets of {LLAMA_CPP_TAG} contain no {server_binary_name()}"
                    )
            marker = {
                "tag": LLAMA_CPP_TAG,
                "platform": plat,
                "assets": {a["name"]: a["sha256"] for a in assets},
            }
            _marker_path(staging).write_text(json.dumps(marker, indent=2), encoding="utf-8")
            shutil.rmtree(root, ignore_errors=True)
            staging.replace(root)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return root / server_binary_name()


def ensure_binary(
    config: Optional[Dict[str, Any]] = None, progress: Optional[Callable[[int, int], None]] = None
) -> Path:
    """The path to a usable llama-server, fetching the pin only if nothing resolves."""
    config = config or {}
    resolved = resolve_binary(config)
    if resolved:
        return resolved
    return install_pin(progress)
