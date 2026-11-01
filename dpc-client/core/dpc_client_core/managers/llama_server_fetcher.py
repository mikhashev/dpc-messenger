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

import platform as _platform
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .local_engine_pin_fetcher import (
    DPC_HOME,
    install_with_staging_swap,
    marker_path,
    read_marker,
)

LLAMA_CPP_TAG = "b11669"
LLAMA_CPP_RELEASE_BASE = "https://github.com/ggml-org/llama.cpp/releases/download"
_INSTALL_LOCK = threading.Lock()

# The CUDA family for the Windows asset key, the two Windows asset names and
# `platform_tag()`'s return all name the same CUDA toolkit version; one
# constant keeps them from drifting apart on the next bump.
WIN_CUDA_FAMILY = "win-cuda-13.4-x64"

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
# b11669 is what v0.7.0 named (2026-10-15). Do not follow `releases/latest`: it
# now returns the versioned release, whose only asset is that text file.
PLATFORM_ASSETS: Dict[str, List[Dict[str, Any]]] = {
    WIN_CUDA_FAMILY: [
        {
            "name": f"llama-{LLAMA_CPP_TAG}-bin-{WIN_CUDA_FAMILY}.zip",
            "sha256": "3b92d5c064f5032e603e531d3ec8961f3bd6613e5dfad047399a92dec8e95dbc",
            "size": 154_582_227,
        },
        {
            "name": f"cudart-llama-bin-{WIN_CUDA_FAMILY}.zip",
            "sha256": "738f8c251ac22b70c3ae6f83a10cf222725df0395246a2cf58f32bdb85fbe668",
            "size": 423_535_356,
        },
    ],
    "macos-arm64": [
        {
            "name": f"llama-{LLAMA_CPP_TAG}-bin-macos-arm64.tar.gz",
            "sha256": "dbed55493d72bc1ba2babc8770d669c2289b5bc4ee9fb2a4dfc158d45b7d13fd",
            "size": 12_191_582,
        },
    ],
    "ubuntu-x64": [
        {
            "name": f"llama-{LLAMA_CPP_TAG}-bin-ubuntu-x64.tar.gz",
            "sha256": "cf07c1c112d709bd67e522017471de57ed0ec8bea031d732c5422351c07f65ac",
            "size": 17_851_611,
        },
    ],
}


def platform_tag() -> str:
    """The pin's asset family for this machine."""
    if sys.platform == "win32":
        return WIN_CUDA_FAMILY
    if sys.platform == "darwin":
        return "macos-arm64" if _platform.machine() == "arm64" else "ubuntu-x64"
    return "ubuntu-x64"


def server_binary_name() -> str:
    return "llama-server.exe" if sys.platform == "win32" else "llama-server"


def pins_dir() -> Path:
    """The folder every llama.cpp pin lives in: ~/.dpc/bin/llama.cpp/."""
    return DPC_HOME / "bin" / "llama.cpp"


def install_root(tag: str = LLAMA_CPP_TAG, plat: Optional[str] = None) -> Path:
    """Where a fetched pin lives: ~/.dpc/bin/llama.cpp/<tag>/<platform>/."""
    return pins_dir() / tag / (plat or platform_tag())


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
    if marker_path(root).is_file() and (root / server_binary_name()).is_file():
        pinned = read_marker(root)
        if pinned and pinned.get("tag") == LLAMA_CPP_TAG:
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


def install_pin(progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """Fetch and install the pinned release; atomic enough to leave no half state, one at a time."""
    with _INSTALL_LOCK:
        plat = platform_tag()
        assets = PLATFORM_ASSETS[plat]
        root = install_root(plat=plat)
        if (root / server_binary_name()).is_file() and marker_path(root).is_file():
            return root / server_binary_name()

        def _check(staging: Path) -> None:
            if not (staging / server_binary_name()).is_file():
                # Archives lay out files differently than this pin expects.
                found = sorted(staging.rglob(server_binary_name()))
                if not found:
                    raise ValueError(
                        f"{plat} assets of {LLAMA_CPP_TAG} contain no {server_binary_name()}"
                    )

        install_with_staging_swap(
            root,
            assets,
            f"{LLAMA_CPP_RELEASE_BASE}/{LLAMA_CPP_TAG}",
            {
                "tag": LLAMA_CPP_TAG,
                "platform": plat,
                "assets": {a["name"]: a["sha256"] for a in assets},
            },
            check=_check,
            progress=progress,
            temp_prefix="llama-pin-",
        )
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
