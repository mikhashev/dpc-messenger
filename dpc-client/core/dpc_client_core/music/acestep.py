"""The acestep.cpp server, started, watched and stopped by DPC (ADR-051 D30; SPEC §12
decisions 31, 34).

One `ace-server` child per backend, on loopback only. It follows the local-engine
supervisors (`managers/llama_server_supervisor.py`): the child gets a process group of its
own and streams its output to a log file under `<DPC home>/logs/`; it is entered in the
process registry at its birth (ADR-046), so the orphan sweep and the backend's stop see it;
it is asked to stop first (CTRL_BREAK_EVENT on Windows, SIGTERM elsewhere) and killed when
it has not gone after `STOP_GRACE_S`. `terminate()` is not used on Windows, for the reason
that file gives (it loses the child's unflushed output).

Since 2026-11-21 acestep.cpp is the second music engine: yue2.cpp is the default (Mike
#198, SPEC §12 decision 40, `music/yue2.py`). Only one of them is on the card at a time
(`music/engines.py`).

**Settings** (`[music]`, read on every start, so a change needs no restart):

- `acestep_binary`: the server binary. Empty means `<DPC home>/engines/acestep.cpp/build/
  ace-server(.exe)`, or the first `ace-server(.exe)` a short walk of that build folder finds
  (multi-config generators put it in `bin/` or `Release/`).
- `acestep_models_dir`: the GGUF folder, default `<DPC home>/models/music/ace-step` (D14).
- `acestep_port`: the loopback port, default 9995 (9996 is the track stream, 9997 the
  gateway, 9998 the file server, 9999 local_api, 9994 yue2.cpp; llama-server children take
  free ephemeral ports). 0 picks a free port at each start.
- `acestep_idle_stop_s`: stop the server after this many idle seconds to free the card
  (default 300; 0 stops it as soon as a job ends).
- `acestep_device`: passed as `--device` when set (`CUDA0`, `Vulkan0`, `MTL0`, `CPU`); empty
  lets the engine pick the best device.

The host is always 127.0.0.1; there is no setting for it (decision 31: loopback only).

**Models.** Slice 1 runs one model set (decision 34): the 4B LM Q8, the turbo DiT Q8, the
Qwen3 text encoder Q8 and the BF16 VAE. The server scans the whole folder (other DiTs may sit
beside them), so every request names its LM, DiT and VAE by file name (`MODEL_FILES`); the
text encoder is the folder's only one. A missing one makes the engine `not_installed`.

**States** (`EngineStatus`): `not_installed` (binary or a model file missing, named),
`stopped`, `starting`, `running`, `error`. `error` is what the brief calls "failed": a start
that did not reach a healthy server, or a server that exited on its own. It stays until a
start succeeds (`start_music_engine`, or the runner's one start for a job it holds: "restart
on crash only when a job needs it"), as Strata keeps a recorded engine death until
`strata_reset_failure`. Nothing restarts the server by itself.

**Identity** (PROVENANCE §2): the binary and the ggml libraries beside it by file name,
size and sha256; the four model files by name and size, their sha256 computed on the first
job that needs them; both cached in `<DPC home>/music/acestep_identity_cache.json` by path,
size and mtime, so a 4.4 GB model is hashed once. The version is `/props` `version`, or
`ACE_VERSION` from the `version.h` the build writes beside the binary. Records carry file
names only, never local paths (PROVENANCE rule 3).

**Platforms.** The binary name is `ace-server.exe` on Windows and `ace-server` elsewhere.
Only a Windows build exists today (ai-studio `experiments/music-player/engine/acestep-cpp/
BUILD.md`); on Linux and macOS the engine reports `not_installed` and says that the build is
the person's until a recipe exists.

**Shared parts.** The child, its states, the idle stop, the HTTP job calls and the file
identity are `engine_base.SupervisedEngine`, which yue2.cpp's supervisor shares; this module
holds what is acestep.cpp's own: the binary, the model set, the command line and the
`/props` check.

**Threading.** Every coroutine runs on the service loop. `status()` is synchronous and
cheap (a few `stat` calls), for the capability probe. `identity()` hashes files: call it
through `asyncio.to_thread`.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .capabilities import ENGINE_ID, ENGINE_NAME
from .engine_base import (  # noqa: F401  (re-exported: older callers import them from here)
    LOOPBACK, START_TIMEOUT_S, STOP_GRACE_S, HashCache as _HashCache, MusicEngineError, SettingsReader,
    SupervisedEngine, exe_name, find_binary_in, free_port as _free_port, port_free as _port_free,
    sha256_file as _sha256_file,
)

logger = logging.getLogger(__name__)

# acestep.cpp's refusals and failures are the shared engine error, one class, so a caller
# catches either engine's with one `except`.
AceStepError = MusicEngineError

KEY = "acestep"
PROCESS_LAUNCHER = "acestep_server"
BINARY_NAME = exe_name("ace-server")
DEFAULT_PORT = 9995
DEFAULT_IDLE_STOP_S = 300.0
# `--max-batch`, acestep.cpp's LM batch limit (default 1). The trial ran with 2 (BUILD.md
# §4); it sizes the LM's KV cache. `synth_batch_size` is not bound by it (the DiT takes up
# to 9), and the LM pass of a job is one request, so 2 changes nothing a take records.
MAX_BATCH = 2
LOG_NAME = "acestep-server.log"
IDENTITY_CACHE = "acestep_identity_cache.json"

# The slice-1 model set (SPEC decision 34; BUILD.md §3), by role.
MODEL_FILES: Dict[str, str] = {
    "lm": "acestep-5Hz-lm-4B-Q8_0.gguf",
    "dit": "acestep-v15-turbo-Q8_0.gguf",
    "text_encoder": "Qwen3-Embedding-0.6B-Q8_0.gguf",
    "vae": "vae-BF16.gguf",
}

_NO_LINUX_RECIPE = ("DPC has a build recipe for Windows only (ai-studio experiments/music-player/engine/"
                    "acestep-cpp/BUILD.md); on Linux and macOS build acestep.cpp from source and set "
                    "[music] acestep_binary")


# -- settings ----------------------------------------------------------------------


@dataclass(frozen=True)
class AceStepConfig:
    binary: Path
    models_dir: Path
    port: int = DEFAULT_PORT
    idle_stop_s: float = DEFAULT_IDLE_STOP_S
    device: Optional[str] = None
    log_path: Optional[Path] = None
    start_timeout_s: float = START_TIMEOUT_S
    # The build folder searched when `acestep_binary` was empty and nothing was found, for
    # the `not_installed` reason.
    searched: Optional[Path] = None


def default_build_dir(dpc_home: Path) -> Path:
    return Path(dpc_home) / "engines" / "acestep.cpp" / "build"


def find_binary(build_dir: Path) -> Optional[Path]:
    """`ace-server(.exe)` at the top of `build_dir`, else the first one a short walk of
    that folder finds (shallowest first), else None."""
    return find_binary_in(build_dir, BINARY_NAME)


def read_config(get: Callable[[str, str, str], Any], dpc_home: Path) -> AceStepConfig:
    """The `[music]` acestep settings through `get(section, key, fallback)` (a `Settings.get`).
    A value that does not parse is logged and replaced by its default."""
    home = Path(dpc_home)
    s = SettingsReader(get)
    searched = None
    binary = s.path("acestep_binary")
    if binary is None:
        build = default_build_dir(home)
        found = find_binary(build)
        binary = found or build / BINARY_NAME
        searched = None if found else build
    models_dir = s.path("acestep_models_dir") or home / "models" / "music" / "ace-step"
    return AceStepConfig(binary=binary, models_dir=models_dir, port=s.port("acestep_port", DEFAULT_PORT),
                         idle_stop_s=s.seconds("acestep_idle_stop_s", DEFAULT_IDLE_STOP_S),
                         device=s.text("acestep_device") or None, log_path=home / "logs" / LOG_NAME,
                         searched=searched)


def _read_version_h(binary: Path) -> Optional[str]:
    """`ACE_VERSION` from the `version.h` the CMake build writes into the build folder."""
    for folder in (binary.parent, *binary.parents[:2]):
        path = folder / "version.h"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'#define\s+ACE_VERSION\s+"([^"]+)"', text)
        if m:
            return m.group(1)
    return None


# -- the supervisor ----------------------------------------------------------------


class AceStepServer(SupervisedEngine):
    """The DPC-owned `ace-server` child. One instance per backend."""

    KEY = KEY
    ENGINE = ENGINE_ID
    DISPLAY = "acestep.cpp"
    NAME = ENGINE_NAME
    PROCESS_LAUNCHER = PROCESS_LAUNCHER
    BINARY_NAME = BINARY_NAME
    LOG_NAME = LOG_NAME
    IDENTITY_CACHE = IDENTITY_CACHE
    PORT_SETTING = "acestep_port"
    # ADR-052 L11 (slice S4): "about 9.3 GB" peak, mostly the 4B LM and its 4.6 GB KV cache (ai-studio
    # engine/acestep-cpp trial notes), read as 9300 MiB, the larger reading of the two units.
    VRAM_ESTIMATE_MIB = 9300

    def build_command(self, port: int) -> List[str]:
        cmd = [str(self.config.binary), "--host", LOOPBACK, "--port", str(port),
               "--models", str(self.config.models_dir), "--max-batch", str(MAX_BATCH)]
        if self.config.device:
            cmd += ["--device", self.config.device]
        return cmd

    def installed_problem(self) -> Optional[str]:
        """Why the engine cannot be started, naming what is missing; None when it can."""
        problems = []
        binary = self.config.binary
        if not binary.is_file():
            where = (f"no {BINARY_NAME} in {self.config.searched}" if self.config.searched
                     else f"{binary} does not exist")
            msg = f"the acestep.cpp server binary is missing ({where})"
            if sys.platform != "win32":
                msg += f"; {_NO_LINUX_RECIPE}"
            problems.append(msg)
        elif os.name != "nt" and not os.access(binary, os.X_OK):
            problems.append(f"{binary} is not executable")
        missing = [name for name in MODEL_FILES.values() if not (self.config.models_dir / name).is_file()]
        if missing:
            problems.append(f"model file(s) missing in {self.config.models_dir}: {', '.join(missing)}")
        return "; ".join(problems) if problems else None

    def status_models(self) -> Dict[str, str]:
        return dict(MODEL_FILES)

    def server_args(self) -> Dict[str, Any]:
        return {"max_batch": MAX_BATCH, "device": self.config.device}

    def _read_version_file(self) -> Optional[str]:
        return _read_version_h(self.config.binary)

    def _check_props(self, props: Any) -> None:
        super()._check_props(props)
        models = props.get("models") if isinstance(props.get("models"), dict) else {}
        wanted = {"lm": ("lm", MODEL_FILES["lm"]), "dit": ("dit", MODEL_FILES["dit"]),
                  "embedding": ("text_encoder", MODEL_FILES["text_encoder"]), "vae": ("vae", MODEL_FILES["vae"])}
        missing = [name for bucket, (_role, name) in wanted.items() if name not in (models.get(bucket) or [])]
        if missing:
            raise AceStepError("start_failed", f"the server did not load model(s): {', '.join(missing)}")

    def identity(self, *, model_sha256: bool = True) -> Dict[str, Any]:
        """Engine and model identity for a take's record (PROVENANCE §2): file names, sizes
        and sha256, no paths. Blocking: hashes files the cache does not hold."""
        models = {role: self._file_identity(self.config.models_dir / name, with_sha=model_sha256)
                  for role, name in MODEL_FILES.items()}
        return {
            "engine": ENGINE_ID,
            "version": self.version,
            "binaries": self._binaries_identity(),
            "server_args": self.server_args(),
            "models": models,
        }
