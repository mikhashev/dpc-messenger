"""The yue2.cpp server, started, watched and stopped by DPC: the default music engine (Mike
#198, 2026-11-21; ADR-051 D35; SPEC §12 decision 40).

One `yue-server` child per backend, on loopback only, supervised exactly as acestep.cpp's
`ace-server` (`engine_base.SupervisedEngine`: its own process group, a log under
`<DPC home>/logs/`, the process registry, CTRL_BREAK_EVENT / SIGTERM then kill, idle stop,
one instance). yue2.cpp is by the same author and speaks the same job protocol; this module
holds what is its own.

**Settings** (`[music]`, read on every start, so a change needs no restart):

- `yue2_binary`: the server binary. Empty means `<DPC home>/engines/yue2.cpp/build/
  yue-server(.exe)`, or the first one a short walk of that build folder finds.
- `yue2_models_dir`: the weights folder, default `<DPC home>/models/music/yue2`.
- `yue2_backbone`: `stock` (default) or `rs-tw`, the backbone a start by hand loads; a job
  names its own (`params.backbone`), and a server running another one is restarted for it
  while idle.
- `yue2_port`: the loopback port, default 9994 (9995 acestep.cpp, 9996 the track stream,
  9997 the gateway, 9998 the file server, 9999 local_api; not 8080, 8443 or 8888-8890, and
  not yue-server's own 8087, which a server run by hand would hold). 0 picks a free port at
  each start.
- `yue2_idle_stop_s`: stop the server after this many idle seconds (default 300).
- `yue2_adapters_dir`: the source LoRA folder, default
  `<models dir>/loras/ruach-voices/voices`, read one level deep as the server reads a
  folder: each `.safetensors` file is an adapter by its file name, each folder holding one
  by the folder's name. A missing folder means no adapters (not an install problem). The
  server itself reads the split cache made from it (below), not this folder. The loras
  folder itself (`<models dir>/loras`) is read the same way after it, so an adapter dropped
  there (a file, or a folder holding one, as Ruach Studio keeps them) is found too; a name
  the first folder already gave is not offered twice.
- `yue2_instrumental_adapter`: the adapter an Instrumental job on YuE2 runs with (see
  "adapters chosen by what the job is" below); empty means `YuE2-instrumental-cot-full-loras`,
  `none` means no adapter, so Instrumental jobs go to acestep.cpp.
- `yue2_device`: `--device` when set (`CUDA0`, `Vulkan0`, `MTL0`, `CPU`).
- `yue2_max_seq`: `--max-seq` when above 0 (the KV cache size; 0 keeps the model context,
  24576 tokens; 8192 trades length for VRAM, README: 5.8 GB vs 3.8 GB for a 65 s song).

**Models** (relative to the models folder; the first present is used): the stock Q8
backbone `serveurperso/YuE2-3B-Q8_0.gguf` (Serveurperso's GGUF; our own conversion of
m-a-p in `converted/` is tensor-identical, ai-studio `weights.sha256`), the RS-TW finetune
`rs-tw/YuE2-3B-RS-TW-2.0x0.75-Q8_0.gguf`, and the VAE `serveurperso/YuE2-Vae-F32.gguf`.
The engine is `not_installed` without the binary, the VAE, or any backbone; a job naming a
backbone whose file is missing is refused at propose time (`form_info`, read by
`MusicCapabilities.check_params`).

**Batch 1.** `--max-batch 1`: the CUDA flash-attention mask bug at batch > 1 is unfixed in
ggml ef4555b7 (ai-studio `engine/yue2-cpp/BUILD.md`), so the runner sends one request per
variant with `lm_batch_size` and `synth_batch_size` 1.

**Identity** (PROVENANCE §2): the binary and the ggml libraries, the backbone and the VAE,
and each adapter a take used (its source file and both split halves, with their
strengths), by file name, size and sha256 (cached in
`<DPC home>/music/yue2_identity_cache.json`); the version is `/props` `version` or
`YUE2_VERSION` from the build's `version.h` (`"e646d52 (2026-11-17)"`), whose first word is
the commit.

**Platforms.** `yue-server.exe` on Windows, `yue-server` elsewhere. DPC's build recipe is
Windows (ai-studio `experiments/music-player/engine/yue2-cpp/BUILD.md`); yue2.cpp ships
`buildcuda.sh` / `buildvulkan.sh` / `buildcpu.sh` for Linux and macOS (Metal on macOS), and
a build there is found the same way or named by `yue2_binary`.

**Voice adapters** (the Ruach voices) are trained on real readers' voices without stated
consent: for local listening only (`ADAPTER_NOTE`, shown by the form; decision 40). Each is
split into its music and sound halves so the two take their own strengths (see "adapters"
below).

**Adapters chosen by what the job is** (Mike, 2026-11-24, group #75/#76: option (c), and
the mechanism general, instrumental its first user). Stock YuE2 sings invented words when
it is given no lyrics (backlog AN-INSTRUMENTAL-JOB-ON-YUE2-COMES-BACK-SUNG-...), so an
Instrumental job on YuE2 runs with the instrumental adapter by default and goes to
acestep.cpp when that adapter is not installed or does not load. The adapter is named by
`yue2_instrumental_adapter`; `form_info()["purpose_adapters"]` says whether it is offered,
`MusicCapabilities.choose_engine` adds it to the job (or moves the job to acestep.cpp) and
records why on the job (`engine_choice`), and the runner falls back at run time
(`runner.py`). The default is Mothersuperior's `YuE2-instrumental-cot-full-loras`
(`ar_lora_inst_v3abc`, rank 64 on the music half only, CC BY-NC 4.0; also in
goldhub/Ruach_Studio_Models_v2): its README asks for strength 1.0, `cot` full and the bare
`[instrumental]` tag as the lyrics, which is what the request sends (`INSTRUMENTAL_LYRICS`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import job_schemas as js
from .capabilities import YUE2_ENGINE_ID, YUE2_ENGINE_NAME
from .engine_base import (
    LOOPBACK, START_TIMEOUT_S, MusicEngineError, SettingsReader, SupervisedEngine, exe_name, find_binary_in,
)
from .store import atomic_write_text

logger = logging.getLogger(__name__)

Yue2Error = MusicEngineError

KEY = js.ENGINE_YUE2
PROCESS_LAUNCHER = "yue2_server"
BINARY_NAME = exe_name("yue-server")
DEFAULT_PORT = 9994
DEFAULT_IDLE_STOP_S = 300.0
MAX_BATCH = 1
LOG_NAME = "yue2-server.log"
IDENTITY_CACHE = "yue2_identity_cache.json"
LORAS_SUBDIR = Path("loras")
DEFAULT_ADAPTERS_SUBDIR = LORAS_SUBDIR / "ruach-voices" / "voices"

# Adapters chosen by what the job is (module docstring). One purpose today.
PURPOSE_INSTRUMENTAL = "instrumental"
DEFAULT_INSTRUMENTAL_ADAPTER = "YuE2-instrumental-cot-full-loras"
INSTRUMENTAL_ADAPTER_SCALE = 1.0          # its README: apply at scale 1.0
INSTRUMENTAL_LYRICS = "[instrumental]"    # its README's bare form: the model picks the structure

# Relative to the models folder, by role; the first present file is used.
BACKBONE_FILES: Dict[str, Sequence[str]] = {
    "stock": ("serveurperso/YuE2-3B-Q8_0.gguf", "converted/YuE2-3B-Q8_0.gguf", "YuE2-3B-Q8_0.gguf"),
    "rs-tw": ("rs-tw/YuE2-3B-RS-TW-2.0x0.75-Q8_0.gguf", "YuE2-3B-RS-TW-2.0x0.75-Q8_0.gguf"),
}
VAE_FILES: Sequence[str] = ("serveurperso/YuE2-Vae-F32.gguf", "converted/YuE2-Vae-F32.gguf", "YuE2-Vae-F32.gguf")

ADAPTER_NOTE = ("Voice adapters are trained on real readers' voices without stated consent: "
                "for local listening only.")

_NO_LINUX_RECIPE = ("DPC has a build recipe for Windows only (ai-studio experiments/music-player/engine/yue2-cpp/"
                    "BUILD.md); on Linux and macOS build yue2.cpp with its buildcuda.sh, buildvulkan.sh or "
                    "buildcpu.sh and set [music] yue2_binary")


# -- settings ----------------------------------------------------------------------


@dataclass(frozen=True)
class Yue2Config:
    binary: Path
    models_dir: Path
    backbone: str = js.YUE2_BACKBONE_DEFAULT
    port: int = DEFAULT_PORT
    idle_stop_s: float = DEFAULT_IDLE_STOP_S
    adapters_dir: Optional[Path] = None
    device: Optional[str] = None
    instrumental_adapter: Optional[str] = DEFAULT_INSTRUMENTAL_ADAPTER
    max_seq: int = 0
    log_path: Optional[Path] = None
    start_timeout_s: float = START_TIMEOUT_S
    searched: Optional[Path] = None


def default_build_dir(dpc_home: Path) -> Path:
    return Path(dpc_home) / "engines" / "yue2.cpp" / "build"


def find_binary(build_dir: Path) -> Optional[Path]:
    return find_binary_in(build_dir, BINARY_NAME)


def read_config(get: Callable[[str, str, str], Any], dpc_home: Path) -> Yue2Config:
    """The `[music]` yue2 settings through `get(section, key, fallback)` (a `Settings.get`).
    A value that does not parse is logged and replaced by its default."""
    home = Path(dpc_home)
    s = SettingsReader(get)
    searched = None
    binary = s.path("yue2_binary")
    if binary is None:
        build = default_build_dir(home)
        found = find_binary(build)
        binary = found or build / BINARY_NAME
        searched = None if found else build
    models_dir = s.path("yue2_models_dir") or home / "models" / "music" / "yue2"
    backbone = s.text("yue2_backbone") or js.YUE2_BACKBONE_DEFAULT
    if backbone not in js.YUE2_BACKBONES:
        logger.warning("[music] yue2_backbone %r is not one of %s; using %s", backbone, js.YUE2_BACKBONES,
                       js.YUE2_BACKBONE_DEFAULT)
        backbone = js.YUE2_BACKBONE_DEFAULT
    adapters_dir = s.path("yue2_adapters_dir") or models_dir / DEFAULT_ADAPTERS_SUBDIR
    instrumental = s.text("yue2_instrumental_adapter") or DEFAULT_INSTRUMENTAL_ADAPTER
    if instrumental.lower() == "none":
        instrumental = None
    return Yue2Config(binary=binary, models_dir=models_dir, backbone=backbone,
                      port=s.port("yue2_port", DEFAULT_PORT),
                      idle_stop_s=s.seconds("yue2_idle_stop_s", DEFAULT_IDLE_STOP_S),
                      adapters_dir=adapters_dir, device=s.text("yue2_device") or None,
                      instrumental_adapter=instrumental,
                      max_seq=s.whole("yue2_max_seq", 0), log_path=home / "logs" / LOG_NAME, searched=searched)


def _first_present(folder: Path, names: Iterable[str]) -> Path:
    names = list(names)
    for rel in names:
        path = folder / rel
        if path.is_file():
            return path
    return folder / names[0]


def _read_version_h(binary: Path) -> Optional[str]:
    """`YUE2_VERSION` from the `version.h` the CMake build writes into the build folder."""
    for folder in (binary.parent, *binary.parents[:2]):
        try:
            text = (folder / "version.h").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'#define\s+YUE2_VERSION\s+"([^"]+)"', text)
        if m:
            return m.group(1)
    return None


def commit_of(version: Optional[str]) -> Optional[str]:
    """The commit in a yue2.cpp version string (`"e646d52 (2026-11-17)"` -> `"e646d52"`)."""
    if not version:
        return None
    word = version.split()[0]
    return word if re.fullmatch(r"[0-9a-f]{7,40}", word) else None


# -- adapters: the source folder, and the split cache the server reads ---------------
#
# yue2.cpp takes one `scale` per adapter file, merged into every half the file changes.
# The Ruach voice adapters carry both halves in one file with AI-Toolkit's key names:
# `text_encoders.*` is the music half (AR), `diffusion_model.*` the sound half (NAR); their
# README sets the halves apart (music 0.45, sound 0.35). So DPC splits each such file by key
# prefix into `<name>-music.safetensors` and `<name>-sound.safetensors`, tensor bytes
# unchanged (the header is rewritten with the subset and new offsets, the metadata kept with
# `part` and `derived_from` added, exactly as the trial's `scripts/voices_split.py`), into a
# DPC-owned folder `<models dir>/loras/.split/`, and points `--adapters` at that folder. A
# request stacks both files, each at its own strength. `manifest.json` there keys each
# split by its source's sha256 (and size), so a changed source is split again and an
# unchanged one is reused; files no source accounts for are removed. A one-half adapter in
# another layout (native trainer, ComfyUI) is used whole: copied byte for byte into the cache,
# its keys checked as the server reads them (`adapter_key_half`). Not offered: both halves in
# such a layout (no per-half strengths [P]), or a whole one beside an adapter_config.json (its
# alpha would not follow the copy).

HALF_PREFIXES = (("music", "text_encoders."), ("sound", "diffusion_model."))
SPLIT_DIRNAME = ".split"
SPLIT_MANIFEST = "manifest.json"
SPLIT_SCHEMA = "dpc.yue2-adapter-split/1"
_HEADER_MAX = 64 * 1024 * 1024


def read_safetensors_header(path: Path) -> Tuple[Dict[str, Any], int]:
    """`(header, data_start)` of a safetensors file: the JSON header and the offset where
    tensor data begins. Raises ValueError on a file that is not safetensors."""
    with open(path, "rb") as f:
        head = f.read(8)
        if len(head) != 8:
            raise ValueError("shorter than a safetensors header")
        n = struct.unpack("<Q", head)[0]
        if not 0 < n <= _HEADER_MAX:
            raise ValueError(f"header length {n} is not plausible")
        raw = f.read(n)
    if len(raw) != n:
        raise ValueError("the header is cut short")
    header = json.loads(raw.decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("the header is not a JSON object")
    return header, 8 + n


def adapter_halves(header: Mapping[str, Any]) -> Tuple[List[str], Optional[str]]:
    """The halves an AI-Toolkit adapter header carries (`music`, `sound`), or `([], why)`
    when its keys are in another layout."""
    keys = [k for k in header if k != "__metadata__"]
    if not keys:
        return [], "no tensors"
    prefixes = tuple(p for _h, p in HALF_PREFIXES)
    odd = [k for k in keys if not k.startswith(prefixes)]
    if odd:
        return [], f"key {odd[0]!r} is not text_encoders.* or diffusion_model.* (a layout DPC cannot split)"
    return [h for h, p in HALF_PREFIXES if any(k.startswith(p) for k in keys)], None


_ROLE_SUFFIXES = (  # (suffix, role, targets a bias), in yue2.cpp's order
    (".lora_A.weight", "lora", False), (".lora_B.weight", "lora", False),
    (".lora_down.weight", "lora", False), (".lora_up.weight", "lora", False),
    (".lora_A", "lora", False), (".lora_B", "lora", False), (".alpha", "alpha", False),
    (".diff_b", "diff", True), (".diff", "diff", False), (".weight", "full", False), (".bias", "full", True),
)
_SITES = ("self_attn.q_proj", "self_attn.k_proj", "self_attn.v_proj", "self_attn.o_proj", "mlp.gate_proj",
          "mlp.up_proj", "mlp.down_proj", "self_attn.qkv_proj", "mlp.gate_up_proj")
_LAYER_RE = re.compile(r"[0-9]+")


def adapter_key_half(key: str) -> Optional[str]:
    """The half an adapter key changes, `music` (AR) or `sound` (NAR), read as yue2.cpp
    e646d52 reads it (`src/adapter.h` adapter_parse_key), or None for a key the server
    refuses (which makes it refuse the whole adapter)."""
    role = bias = None
    for suffix, r, b in _ROLE_SUFFIXES:
        if len(key) > len(suffix) and key.endswith(suffix):
            key, role, bias = key[:-len(suffix)], r, b
            break
    if role is None:
        return None
    hint = None
    if key.startswith("text_encoders."):
        key, hint = key[len("text_encoders."):], "music"
    elif key.startswith("diffusion_model."):
        key, hint = key[len("diffusion_model."):], "sound"
    elif key.startswith("adapters."):
        key = key[len("adapters."):].replace("-", ".")
    elif key.startswith("companion."):
        key = key[len("companion."):]
    elif key.startswith("io."):
        key = key[len("io."):]
    if key in ("vae2llm", "llm2vae"):
        return None if hint == "music" else "sound"
    if bias or role in ("full", "diff"):
        return None
    if key.startswith("layers."):
        key = "model." + key
    if not key.startswith("model.layers."):
        return None
    tail = key[len("model.layers."):]
    layer, _, site = tail.partition(".")
    if not site or not _LAYER_RE.fullmatch(layer):
        return None
    nar = site.startswith("nar_")
    if nar:
        site = site[len("nar_"):]
        if hint == "music":
            return None
    if site not in _SITES:
        return None
    return "sound" if nar or hint == "sound" else "music"


def adapter_layout(header: Mapping[str, Any]) -> Tuple[Optional[str], List[str], Optional[str]]:
    """How DPC offers an adapter, from its header: `("split", halves, None)` for AI-Toolkit
    keys (split into one file per half), `("whole", [half], None)` for one half in any layout
    yue2.cpp reads (used as it is), or `(None, [], why)`."""
    halves, _why = adapter_halves(header)
    if halves:
        return "split", halves, None
    keys = [k for k in header if k != "__metadata__"]
    if not keys:
        return None, [], "no tensors"
    found = set()
    for k in keys:
        half = adapter_key_half(k)
        if half is None:
            return None, [], f"key {k!r} is not one yue2.cpp reads (it would refuse the adapter)"
        found.add(half)
    if len(found) > 1:
        return None, [], ("it changes both halves in a layout DPC cannot split into one file per half "
                          "(text_encoders.* / diffusion_model.*)")
    return "whole", sorted(found), None


def copy_adapter(source: Path, name: str, half: str, dest_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Copy a one-half adapter byte for byte as `dest_dir/<name>-<half>.safetensors` (temp
    file then rename). Returns `{half: {file, bytes, sha256, tensors}}`. Blocking."""
    header, _start = read_safetensors_header(source)
    tensors = len([k for k in header if k != "__metadata__"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / split_name(name, half)
    tmp = target.with_name(f".{target.name}.tmp")
    h = hashlib.sha256()
    size = 0
    with open(source, "rb") as src, open(tmp, "wb") as o:
        while True:
            chunk = src.read(4 * 1024 * 1024)
            if not chunk:
                break
            o.write(chunk)
            h.update(chunk)
            size += len(chunk)
        o.flush()
        os.fsync(o.fileno())
    os.replace(tmp, target)
    return {half: {"file": target.name, "bytes": size, "sha256": "sha256:" + h.hexdigest(), "tensors": tensors}}


def split_stem(name: str) -> str:
    return name[:-len(".safetensors")] if name.endswith(".safetensors") else name


def split_name(name: str, half: str) -> str:
    """The file (and request name) of one half of adapter `name`."""
    return f"{split_stem(name)}-{half}.safetensors"


def split_adapter(source: Path, name: str, dest_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Write each half of `source` as `dest_dir/<name>-<half>.safetensors` (tensor bytes
    copied as they are; temp file then rename). Returns `{half: {file, bytes, sha256,
    tensors}}`. Blocking."""
    header, data_start = read_safetensors_header(source)
    halves, why = adapter_halves(header)
    if not halves:
        raise ValueError(why or "nothing to split")
    meta = header.get("__metadata__") or {}
    out: Dict[str, Dict[str, Any]] = {}
    dest_dir.mkdir(parents=True, exist_ok=True)
    with open(source, "rb") as src:
        for half, prefix in HALF_PREFIXES:
            if half not in halves:
                continue
            keys = [k for k in header if k != "__metadata__" and k.startswith(prefix)]
            new: Dict[str, Any] = {"__metadata__": dict(meta, part=half, derived_from=source.name)}
            ranges = []
            off = 0
            for k in keys:
                a, b = header[k]["data_offsets"]
                new[k] = dict(dtype=header[k]["dtype"], shape=header[k]["shape"], data_offsets=[off, off + b - a])
                off += b - a
                ranges.append((a, b))
            hb = json.dumps(new, separators=(",", ":")).encode("utf-8")
            hb += b" " * ((8 - len(hb) % 8) % 8)
            target = dest_dir / split_name(name, half)
            tmp = target.with_name(f".{target.name}.tmp")
            h = hashlib.sha256()
            size = 0
            with open(tmp, "wb") as o:
                for chunk in (struct.pack("<Q", len(hb)), hb):
                    o.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
                for a, b in ranges:
                    src.seek(data_start + a)
                    left = b - a
                    while left > 0:
                        chunk = src.read(min(4 * 1024 * 1024, left))
                        if not chunk:
                            raise ValueError(f"{source.name} ends inside tensor data")
                        o.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
                        left -= len(chunk)
                o.flush()
                os.fsync(o.fileno())
            os.replace(tmp, target)
            out[half] = {"file": target.name, "bytes": size, "sha256": "sha256:" + h.hexdigest(), "tensors": len(keys)}
    return out


def scan_adapters(folder: Optional[Path]) -> List[Dict[str, Any]]:
    """The adapters of the source folder, read the way `yue-server --adapters` reads a folder
    (`src/adapter.h`): names sorted, dot entries skipped; a `.safetensors` file is an
    adapter by its file name, a folder holding one by the folder's name (its first
    `.safetensors`, sorted). Each entry: `{name, path, kind, layout, halves, problem}`: `kind`
    `voice` for the Ruach voices (`voice-*`); `layout` `split` or `whole` (`adapter_layout`);
    `halves` what it changes; `problem` why it is not offered (None when it is)."""
    if folder is None or not folder.is_dir():
        return []
    out = []
    try:
        names = sorted(p.name for p in folder.iterdir() if not p.name.startswith("."))
    except OSError:
        return []
    for name in names:
        path = folder / name
        if path.is_dir():
            try:
                inner = sorted(c for c in path.iterdir() if c.is_file() and c.name.endswith(".safetensors")
                               and not c.name.startswith("."))
            except OSError:
                continue
            if not inner:
                continue
            path = inner[0]
        elif not name.endswith(".safetensors"):
            continue
        halves: List[str] = []
        layout: Optional[str] = None
        try:
            layout, halves, problem = adapter_layout(_cached_header(path))
        except Exception as exc:  # a header that parses as JSON but is not shaped like safetensors (TypeError, KeyError, ...)
            problem = f"not a readable safetensors file: {type(exc).__name__}: {exc}"
        if layout == "whole" and (path.parent / "adapter_config.json").is_file():
            layout, halves, problem = None, [], ("an adapter_config.json beside it sets its alpha, which DPC's "
                                                 "cache copy would not carry")
        out.append({"name": name, "path": path, "kind": "voice" if name.startswith("voice-") else "other",
                    "layout": layout, "halves": halves, "problem": problem})
    return out


_HEADER_CACHE: Dict[Tuple[str, int, int], Dict[str, Any]] = {}


def _cached_header(path: Path) -> Dict[str, Any]:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    header = _HEADER_CACHE.get(key)
    if header is None:
        header, _ = read_safetensors_header(path)
        header = {k: (v if k == "__metadata__" else None) for k, v in header.items()}  # keys are enough
        if len(_HEADER_CACHE) > 256:
            _HEADER_CACHE.clear()
        _HEADER_CACHE[key] = header
    return header


# -- the supervisor ----------------------------------------------------------------


class Yue2Server(SupervisedEngine):
    """The DPC-owned `yue-server` child. One instance per backend."""

    KEY = KEY
    ENGINE = YUE2_ENGINE_ID
    DISPLAY = "yue2.cpp"
    NAME = YUE2_ENGINE_NAME
    PROCESS_LAUNCHER = PROCESS_LAUNCHER
    BINARY_NAME = BINARY_NAME
    LOG_NAME = LOG_NAME
    IDENTITY_CACHE = IDENTITY_CACHE
    PORT_SETTING = "yue2_port"
    # ADR-052 L11 (slice S4): the trial's whole-card nvidia-smi peak with the Q8 backbone was 5222-5570
    # MiB ("5.2 to 5.6 GB", ai-studio engine/yue2-cpp/trial.md); the larger figure, rounded up, is the
    # declared peak. The README's 5.8 GB at full context was not measured here.
    VRAM_ESTIMATE_MIB = 5600

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._backbone: Optional[str] = None       # the backbone the child was started with
        self._next_backbone: Optional[str] = None  # the backbone the next start loads
        self._served_adapters: List[str] = []      # what `/props` said the child offers
        self._starting_lock: Optional[asyncio.Lock] = None
        self._split_lock = threading.Lock()
        # adapter name -> why it was left out of the last prepare_adapters run
        self.skipped_adapters: Dict[str, str] = {}

    # -- files ---------------------------------------------------------------------

    def backbone_path(self, backbone: Optional[str] = None) -> Path:
        choice = backbone or self.config.backbone
        return _first_present(self.config.models_dir, BACKBONE_FILES.get(choice, BACKBONE_FILES["stock"]))

    def vae_path(self) -> Path:
        return _first_present(self.config.models_dir, VAE_FILES)

    def adapter_dirs(self) -> List[Path]:
        """The folders adapters are read from, in order: `yue2_adapters_dir`, then the loras
        folder of the models folder (once, when they are the same folder)."""
        out: List[Path] = []
        seen = set()
        for folder in (self.config.adapters_dir, self.config.models_dir / LORAS_SUBDIR):
            if folder is None:
                continue
            key = os.path.normcase(os.path.abspath(folder))
            if key not in seen:
                seen.add(key)
                out.append(Path(folder))
        return out

    def adapters(self) -> List[Dict[str, Any]]:
        """Every adapter of the source folders, offered or not (`problem`). A name an earlier
        folder already gave is not offered again; the adapter `yue2_instrumental_adapter`
        names has `kind` `instrumental`."""
        out: List[Dict[str, Any]] = []
        first: Dict[str, str] = {}
        for folder in self.adapter_dirs():
            for a in scan_adapters(folder):
                if a["name"] in first:
                    a = dict(a, layout=None, halves=[],
                             problem=f"the name is already taken by an adapter in {first[a['name']]}")
                else:
                    first[a["name"]] = folder.name
                if self.config.instrumental_adapter and a["name"] == self.config.instrumental_adapter:
                    a["kind"] = PURPOSE_INSTRUMENTAL
                out.append(a)
        return out

    def purpose_adapters(self, all_adapters: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
        """The adapter of each purpose (module docstring, "adapters chosen by what the job is"):
        `{instrumental: {adapter, offered, state, halves, scale, reason}}`: `state` `offered`,
        `missing` (not in the folders), `unusable` (found, not offered) or `off` (no adapter
        named); `reason` says why it is not offered (None when it is). Cheap (the scan's stat
        and listdir)."""
        name = self.config.instrumental_adapter
        found = {a["name"]: a for a in (self.adapters() if all_adapters is None else all_adapters)}
        a = found.get(name) if name else None
        if not name:
            reason = "turned off: [music] yue2_instrumental_adapter is none"
        elif a is None:
            reason = f"{name} is not in the adapters folders ({', '.join(d.name for d in self.adapter_dirs())})"
        else:
            reason = a["problem"]
        offered = a is not None and not a["problem"]
        state = "offered" if offered else "off" if not name else "missing" if a is None else "unusable"
        return {PURPOSE_INSTRUMENTAL: {"adapter": name, "offered": offered, "state": state,
                                       "halves": list(a["halves"]) if offered else [],
                                       "scale": INSTRUMENTAL_ADAPTER_SCALE, "reason": None if offered else reason}}

    def instrumental_lyrics_for(self, adapters: Sequence[Mapping[str, Any]]) -> Optional[str]:
        """What an instrumental request sends as its lyrics when it stacks `adapters`: the
        instrumental adapter's bare tag when it is among them, else None (the request's own
        default, empty lyrics)."""
        name = self.config.instrumental_adapter
        if name and any(str(a.get("name")) == name for a in adapters):
            return INSTRUMENTAL_LYRICS
        return None

    def offered_adapters(self) -> List[Dict[str, Any]]:
        return [a for a in self.adapters() if not a["problem"]]

    @property
    def split_dir(self) -> Path:
        """The DPC-owned folder of split adapters, which `--adapters` names."""
        return self.config.models_dir / "loras" / SPLIT_DIRNAME

    def _manifest_path(self) -> Path:
        return self.split_dir / SPLIT_MANIFEST

    def read_manifest(self) -> Dict[str, Any]:
        try:
            doc = json.loads(self._manifest_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries = doc.get("adapters") if isinstance(doc, dict) and doc.get("schema") == SPLIT_SCHEMA else None
        return entries if isinstance(entries, dict) else {}

    def prepare_adapters(self) -> Dict[str, Any]:
        """Bring the split cache up to date with the source folder (see "adapters" above):
        split a new or changed source, reuse an unchanged one (same sha256 and size, split
        files present with the recorded size and content hash), remove what no source accounts for. Returns
        the manifest entries by adapter name. Blocking: hashes sources the identity cache
        does not hold, and writes the splits."""
        with self._split_lock:
            return self._prepare_adapters()

    def _split_file_intact(self, half: Any) -> bool:
        """Whether a cached split file is the one the manifest recorded: its size and its content
        hash (not the size alone: a same-size file that was changed or half rewritten fails). The
        hash comes from the identity cache by size and mtime, so a file the cache has seen is not
        read again."""
        name = half.get("file") if isinstance(half, Mapping) else None
        if not isinstance(name, str) or not name or Path(name).name != name:
            return False
        path = self.split_dir / name
        if not path.is_file():
            return False
        ident = self._file_identity(path, with_sha=True)
        return ident["bytes"] == half.get("bytes") and ident["sha256"] == half.get("sha256")

    def _remember_split_hashes(self, halves: Mapping[str, Mapping[str, Any]]) -> None:
        """Put the hash of each split file just written into the identity cache, so the next
        start's check (`_split_file_intact`) does not read it again."""
        for h in halves.values():
            try:
                path = self.split_dir / h["file"]
                st = path.stat()
                if st.st_size == h.get("bytes") and h.get("sha256"):
                    self._hashes.store(path, st.st_size, st.st_mtime_ns, h["sha256"])
            except (OSError, KeyError, TypeError):
                logger.debug("yue2.cpp: could not cache the hash of a split file", exc_info=True)

    def _prepare_adapters(self) -> Dict[str, Any]:
        offered = self.offered_adapters()
        old = self.read_manifest()
        entries: Dict[str, Any] = {}
        self.skipped_adapters = {}
        for a in offered:
            try:
                source = a["path"]
                ident = self._file_identity(source, with_sha=True)
                entry = old.get(a["name"])
                fresh = (isinstance(entry, dict) and entry.get("source_sha256") == ident["sha256"]
                         and entry.get("layout", "split") == a["layout"]
                         and entry.get("source_bytes") == ident["bytes"]
                         and all(self._split_file_intact(h) for h in (entry.get("halves") or {}).values())
                         and sorted((entry.get("halves") or {})) == sorted(a["halves"]))
                if not fresh:
                    try:
                        if a["layout"] == "whole":
                            logger.info("yue2.cpp: copying adapter %s whole (%s half)", a["name"], a["halves"][0])
                            halves = copy_adapter(source, a["name"], a["halves"][0], self.split_dir)
                        else:
                            logger.info("yue2.cpp: splitting adapter %s into its halves (%s)", a["name"],
                                        ", ".join(a["halves"]))
                            halves = split_adapter(source, a["name"], self.split_dir)
                    except (OSError, ValueError) as exc:
                        logger.warning("yue2.cpp: adapter %s could not be split: %s", a["name"], exc)
                        self.skipped_adapters[a["name"]] = f"{type(exc).__name__}: {exc}"
                        continue
                    self._remember_split_hashes(halves)
                    entry = {"source_file": source.name, "source_bytes": ident["bytes"],
                             "source_sha256": ident["sha256"], "layout": a["layout"], "halves": halves}
                entries[a["name"]] = entry
            except Exception as exc:  # a broken adapter (TypeError, KeyError, struct.error, ...) must not stop the engine
                reason = f"{type(exc).__name__}: {exc}"
                logger.warning("yue2.cpp: adapter %s skipped, it could not be prepared: %s", a["name"], reason)
                self.skipped_adapters[a["name"]] = reason
                continue
        keep = {SPLIT_MANIFEST} | {h["file"] for e in entries.values() for h in e["halves"].values()}
        if self.split_dir.is_dir():
            for f in self.split_dir.iterdir():
                if f.is_file() and f.name not in keep and (f.name.endswith(".safetensors") or f.name.endswith(".tmp")):
                    try:
                        f.unlink()
                    except OSError:
                        logger.warning("yue2.cpp: could not remove the stale split %s", f, exc_info=True)
        if entries != old or not self._manifest_path().is_file():
            self.split_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._manifest_path(), json.dumps(
                {"schema": SPLIT_SCHEMA, "note": "derived copies of the source adapters, one file per half; "
                 "same use limits as their sources", "adapters": entries}, ensure_ascii=False, indent=1))
        return entries

    def request_adapters(self, adapters: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """The `adapters` field of a request for a job's `[{name, music, sound}]`: each half
        the source carries, by its split file, at its own strength. Unknown names are
        refused (`invalid_adapter`)."""
        found = {a["name"]: a for a in self.offered_adapters()}
        out: List[Dict[str, Any]] = []
        for a in adapters:
            src = found.get(str(a.get("name")))
            if src is None:
                raise MusicEngineError("invalid_adapter", f"adapter {a.get('name')!r} is not in "
                                                          f"{self.config.adapters_dir} (or cannot be split)")
            for half in src["halves"]:
                out.append({"name": split_name(src["name"], half), "scale": a.get(half)})
        return out

    @property
    def running_backbone(self) -> Optional[str]:
        return self._backbone if self.alive() else None

    def form_info(self) -> Dict[str, Any]:
        """What the Create form needs: the backbones and whether their file is present,
        the adapters found, the voice-adapter note, and the adapter of each purpose
        (`purpose_adapters`). Cheap (stat and listdir)."""
        if not self.alive():
            self.reload_config()
        all_adapters = self.adapters()
        backbones = {}
        for choice in js.YUE2_BACKBONES:
            path = self.backbone_path(choice)
            present = path.is_file()
            backbones[choice] = {"present": present, "file": path.name,
                                 "reason": None if present else f"{path} does not exist"}
        return {
            "backbones": backbones,
            "backbone_default": self.config.backbone,
            "running_backbone": self.running_backbone,
            "adapters_dir": str(self.config.adapters_dir) if self.config.adapters_dir else None,
            "adapters_dirs": [str(d) for d in self.adapter_dirs()],
            "adapters": [{"name": a["name"], "file": a["path"].name, "kind": a["kind"], "layout": a["layout"],
                          "halves": a["halves"]}
                         for a in all_adapters if not a["problem"]],
            "adapters_skipped": [{"name": a["name"], "reason": a["problem"]} for a in all_adapters if a["problem"]],
            "adapter_note": ADAPTER_NOTE,
            "purpose_adapters": self.purpose_adapters(all_adapters),
        }

    # -- what SupervisedEngine asks for ----------------------------------------------

    def build_command(self, port: int) -> List[str]:
        choice = self._next_backbone or self.config.backbone
        cmd = [str(self.config.binary), "--model", str(self.backbone_path(choice)), "--vae", str(self.vae_path()),
               "--host", LOOPBACK, "--port", str(port), "--max-batch", str(MAX_BATCH)]
        if self.split_dir.is_dir():
            cmd += ["--adapters", str(self.split_dir)]
        if self.config.device:
            cmd += ["--device", self.config.device]
        if self.config.max_seq:
            cmd += ["--max-seq", str(self.config.max_seq)]
        self._backbone = choice
        return cmd

    def installed_problem(self, backbone: Optional[str] = None) -> Optional[str]:
        """Why the engine cannot be started, naming what is missing; None when it can. With
        `backbone`, that backbone's file must be present; without, any backbone will do."""
        problems = []
        binary = self.config.binary
        if not binary.is_file():
            where = (f"no {BINARY_NAME} in {self.config.searched}" if self.config.searched
                     else f"{binary} does not exist")
            msg = f"the yue2.cpp server binary is missing ({where})"
            if sys.platform != "win32":
                msg += f"; {_NO_LINUX_RECIPE}"
            problems.append(msg)
        elif os.name != "nt" and not os.access(binary, os.X_OK):
            problems.append(f"{binary} is not executable")
        if backbone is not None:
            path = self.backbone_path(backbone)
            if not path.is_file():
                problems.append(f"the {backbone} backbone is missing in {self.config.models_dir} "
                                f"(looked for {', '.join(BACKBONE_FILES.get(backbone, ()))})")
        elif not any(self.backbone_path(c).is_file() for c in js.YUE2_BACKBONES):
            problems.append(f"no backbone in {self.config.models_dir} (looked for "
                            f"{', '.join(BACKBONE_FILES['stock'] + tuple(BACKBONE_FILES['rs-tw']))})")
        if not self.vae_path().is_file():
            problems.append(f"the VAE is missing in {self.config.models_dir} (looked for {', '.join(VAE_FILES)})")
        return "; ".join(problems) if problems else None

    def status_models(self) -> Dict[str, str]:
        choice = self.running_backbone or self.config.backbone
        return {"backbone": self.backbone_path(choice).name, "backbone_choice": choice, "vae": self.vae_path().name}

    def server_args(self) -> Dict[str, Any]:
        return {"max_batch": MAX_BATCH, "device": self.config.device, "max_seq": self.config.max_seq or None,
                "adapters": "split cache" if self.split_dir.is_dir() else None}

    def _read_version_file(self) -> Optional[str]:
        return _read_version_h(self.config.binary)

    def _running_as_wanted(self) -> bool:
        return super()._running_as_wanted() and self._backbone == (self._next_backbone or self.config.backbone)

    def _check_props(self, props: Any) -> None:
        super()._check_props(props)
        want_model = self.backbone_path(self._backbone).name
        want_vae = self.vae_path().name
        got_model = Path(str(props.get("model") or "")).name
        got_vae = Path(str(props.get("vae") or "")).name
        wrong = []
        if got_model != want_model:
            wrong.append(f"backbone {got_model or '(none)'} instead of {want_model}")
        if got_vae != want_vae:
            wrong.append(f"VAE {got_vae or '(none)'} instead of {want_vae}")
        if wrong:
            raise MusicEngineError("start_failed", f"the server loaded {'; '.join(wrong)}")
        served = props.get("adapters") if isinstance(props.get("adapters"), list) else []
        self._served_adapters = [str(a.get("name")) for a in served if isinstance(a, dict) and a.get("name")]

    # -- start ---------------------------------------------------------------------

    async def start(self, backbone: Optional[str] = None,
                    adapters: Sequence[Mapping[str, Any]] = ()) -> None:
        """Start the server with `backbone` (default: the running one, else the
        `yue2_backbone` setting) and the split cache brought up to date, and wait until it is
        healthy. A server running another backbone, or not offering an adapter split that
        `adapters` (a job's `[{name, music, sound}]`) needs, is stopped first (`swap`): both
        are boot options. The caller checks that no job is using it (the runner holds it for
        its own job; `start_music_engine` refuses a busy engine)."""
        if not self.alive():
            self.reload_config()
        want = backbone or self.running_backbone or self.config.backbone
        if want not in js.YUE2_BACKBONES:
            raise MusicEngineError("invalid_backbone", f"backbone must be one of {', '.join(js.YUE2_BACKBONES)}, "
                                                       f"got {want!r}")
        needed = [r["name"] for r in self.request_adapters(adapters)] if adapters else []
        # The card before any lock (ADR-052 section 3.2): asked for at once, never waited for.
        await self._card_for_a_start()
        async with self._start_lock():
            as_wanted = self.alive() and self._state == "running" and self._backbone == want                 and set(needed) <= set(self._served_adapters)
            if as_wanted:
                return
            try:
                if self.alive():
                    why = (f"the {want} backbone (running {self._backbone})" if self._backbone != want
                           else f"adapter(s) {', '.join(sorted(set(needed) - set(self._served_adapters)))}")
                    logger.info("yue2.cpp: restarting for %s", why)
                    # One start, two children: the card stays with the engine, busy (`starting`), so
                    # no contender takes it between them.
                    self.card_lease.mark_starting()
                    await self.stop("swap", keep_the_card=True)
                # After the stop: on Windows a split the old child still holds open cannot be replaced.
                await asyncio.to_thread(self.prepare_adapters)
                problem = self.installed_problem(want)
                if problem:
                    raise MusicEngineError("not_installed", problem)
                self._next_backbone = want
                await self._start_child()
            except BaseException:
                if not self.alive():
                    self._let_go_of_the_card("failed_start")
                raise
            missing = sorted(set(needed) - set(self._served_adapters))
            if missing:
                raise MusicEngineError("invalid_adapter", f"yue2.cpp did not load the adapter split(s) "
                                                          f"{', '.join(missing)} from {self.split_dir}; see "
                                                          f"{self.log_path.name}")

    def _start_lock(self) -> asyncio.Lock:
        if self._starting_lock is None:
            self._starting_lock = asyncio.Lock()
        return self._starting_lock

    # -- identity (blocking) ---------------------------------------------------------

    def identity(self, *, model_sha256: bool = True, backbone: Optional[str] = None,
                 adapters: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
        """Engine, model and adapter identity for a take's record (PROVENANCE §2): file
        names, sizes and sha256, no paths. Blocking: hashes files the cache does not hold
        (the 3.8 GB backbone once)."""
        choice = backbone or self.running_backbone or self.config.backbone
        bb = self._file_identity(self.backbone_path(choice), with_sha=model_sha256)
        bb["choice"] = choice
        manifest = self.read_manifest() if adapters else {}
        used = []
        for a in adapters:
            entry = manifest.get(str(a.get("name"))) or {}
            item: Dict[str, Any] = {"name": a.get("name"), "source": {
                "file": entry.get("source_file"), "bytes": entry.get("source_bytes"),
                "sha256": entry.get("source_sha256")}}
            for half, h in (entry.get("halves") or {}).items():
                item[half] = {"file": h.get("file"), "bytes": h.get("bytes"), "sha256": h.get("sha256"),
                              "scale": a.get(half)}
            used.append(item)
        version = self.version
        return {
            "engine": YUE2_ENGINE_ID,
            "version": version,
            "commit": commit_of(version),
            "binaries": self._binaries_identity(),
            "server_args": self.server_args(),
            "models": {"backbone": bb, "vae": self._file_identity(self.vae_path(), with_sha=model_sha256)},
            "adapters": used,
        }
