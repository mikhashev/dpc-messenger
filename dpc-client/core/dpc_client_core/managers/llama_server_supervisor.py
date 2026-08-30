"""
Llama Server Supervisor — one DPC-owned `llama-server` child per alias.

Route (b2) of ADR-040: DPC starts the server with per-alias flags, watches
`/health`, captures `/props` as the capability record, and stops it on drain
or shutdown. One process, keyed by alias — no queue, no priorities, no model
rotation; those are D4-β and land only after gates G3–G5.

Two standing constraints from the 2026-08-19 window are encoded here rather
than remembered:

- `terminate()` is never called on the child. On Windows it kills the process
  with its stdio buffers unflushed, which is why a week of server logs came
  out empty. The child is spawned with CREATE_NEW_PROCESS_GROUP and asked to
  stop with CTRL_BREAK_EVENT (SIGINT elsewhere), which flushes.
- `GGML_BACKEND_PATH` is set whenever a CUDA backend ships beside the binary,
  because without it the server silently runs on the CPU.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .llama_server_fetcher import DPC_HOME, ensure_binary, find_cuda_backend, install_root

logger = logging.getLogger(__name__)

# The child needs its own process group for CTRL_BREAK_EVENT to reach it —
# and only that group, so the signal does not take the backend down with it.
_CREATION_NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

DEFAULTS: Dict[str, Any] = {
    "n_ctx": 262144,
    # None means AUTO: the ladder in ensure_running steps f16 -> q8_0 -> q4_0
    # against the free VRAM the card actually has, starting each rung and
    # reading the child's own out-of-memory verdict from its log tail (the
    # first live call, 2026-08-19 18:09, died on f16 in 12 s — a failed rung is
    # cheap). An alias that names a type gets exactly that type, one attempt.
    "cache_type_k": None,
    "cache_type_v": None,
    # Measured 2026-08-19 on b10472 at 139 490 tokens: without an explicit
    # -ngl the server left one context at 0/66 layers on the GPU-less side of
    # the split and the draft contexts at 57-59/66, disabling fused Gated
    # Delta Net there; every context fully on the GPU was worth +11.3 %
    # (704.5 -> 784.4 tok/s) and the layer-0 warnings went 5 -> 0. An alias
    # can still override this for a card that cannot hold all layers.
    "n_gpu_layers": 999,
    # None, not False: the binary's own default is `auto`, and False is what the
    # form writes when its owner picks "off". Holding False here made those two
    # the same value, so "off" could not be expressed at all.
    "flash_attn": None,
    "mmproj": None,
    "spec_type": "draft-mtp",
    "spec_draft_n_max": 3,  # measured: acceptance 0.686 against 4's 0.578
    # None = the server's own choice (4 unified slots on b10472). An explicit
    # value is ALWAYS sent, so -np 1 is expressible — the old guard ate it and
    # the config said 1 while the server ran 4.
    "n_parallel": None,
    # Micro-batch. None = the build's own 512. Measured 2026-08-20 on this pin
    # and card (llama-bench, logs in ~/.dpc/logs/bench-2026-08-20-prefill): at a
    # 60 000-token prefill 1024 is worth +5.6 % over 512 and 2048 a further
    # +2.8 %, while at 512-8192 tokens the same flag moves nothing (-0.6 % …
    # +0.5 %). The effect needs depth, which is where this fleet lives, and it
    # costs VRAM: 512 -> 1024 measured +683 MiB on the production child. n_batch
    # is exposed beside it because the two are read together; the build's
    # default is 2048 and micro-batches are cut from it.
    "n_batch": None,
    "n_ubatch": None,
    # How many context checkpoints the child keeps per slot, and how far apart.
    # None = the build's own 32 / 8192, which is what every install had. The
    # reason to name them is size, not availability: a checkpoint is a snapshot
    # of the recurrent state and costs ~585-700 MiB here, so a parked deep
    # conversation weighs 12-16 GB of which only ~2.5 GB is attention KV. With
    # the host cache holding whole conversations, the checkpoint count decides
    # how many of them fit — four checkpoints put a 150K state near 5 GB.
    # The trade is where the engine can resume from after a prefix divergence;
    # with the stable-prefix layout the divergence sits in the turn tail, which
    # is exactly what the surviving checkpoints cover.
    "ctx_checkpoints": None,
    "checkpoint_min_step": None,
    "kv_unified": True,
    # The minimum chunk the child will try to reuse from a cached prefix by
    # shifting KV instead of re-reading it. None = the build's own 0, which is
    # off: today any divergence inside the prefix throws away everything behind
    # it. Ours sit early — the knowledge index and the scratchpad are rebuilt
    # ahead of the whole history — so this is the one flag that survives them.
    # 0 is expressible and means off, which is why the guard tests None.
    "cache_reuse": None,
    "cache_ram_mib": None,
    # What a loaded context costs beyond weights and attention-KV, in MiB. None
    # uses the figure below, which was measured on one model: every term in it
    # is model-shaped, so an alias serving a different model owns its own.
    "vram_overhead_mib": None,
    "slot_save_path": None,
    "jinja": True,
    "start_timeout_s": 300.0,
    "extra_args": [],
}


class LlamaServerError(RuntimeError):
    """A supervisor failure, carrying the child's last log lines when it has any."""

    def __init__(self, message: str, log_lines: Optional[List[str]] = None):
        tail = "".join(f"\n  | {line}" for line in (log_lines or [])[-12:])
        super().__init__(f"{message}{tail}")
        self.log_lines = log_lines or []


def _flash_attn_value(value: Any) -> Optional[str]:
    """`on`, `off`, or nothing at all, out of a field that used to be a bool.

    The binary takes `--flash-attn on|off|auto` and refuses it bare:

        error while handling argument "--flash-attn": unknown value ...

    `build_command` emitted it bare until 2026-08-22, so any alias that turned
    the switch on could not start — the child died on argv, before a backend
    was even loaded. Nothing short of handing the line to the binary shows it
    (Fable 5, 2026-08-22 unpushed-dev-batch review 1a).

    `False` means `off` and `None` means silence, which is why DEFAULTS holds
    None here rather than the False it held until 2026-08-22. With False as the
    default the two were indistinguishable: an alias nobody had touched and an
    alias whose owner had chosen "off" in the form arrived here as the same
    value, and mapping it either way was wrong for one of them. None is the
    absence, so False can now be the choice it was always displayed as.
    """
    if value is None:
        return None
    if value is True or value is False:
        return "on" if value else "off"
    text = str(value).strip().lower()
    if text in ("on", "off"):
        return text
    if text != "auto":
        logger.warning(
            "llama-server: flash_attn=%r is not on/off/auto; leaving the "
            "binary at its own default", value,
        )
    return None


_QUANTISED_KV_TYPES = frozenset({"q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"})


def _flash_attn_effective(value: Any, type_v: Any) -> str:
    """What the child will do, which silence here does not say.

    `auto` is the binary's default, and on a quantised V cache the pin
    resolves it to on and refuses to start when it is off
    (`src/llama-context.cpp:3596-3605`, b10566). So the one configuration
    production runs — no flag, `-ctv q4_0` — is the one a reader of the start
    line could not resolve without opening llama.cpp; the child's own log
    never names flash_attn at any verbosity.
    """
    explicit = _flash_attn_value(value)
    quantised_v = str(type_v or "").strip().lower() in _QUANTISED_KV_TYPES
    if explicit == "on":
        return "on"
    if explicit == "off":
        return "off (refused at start by the quantised V cache)" if quantised_v else "off"
    if quantised_v:
        return "on (auto, forced by the quantised V cache)"
    return "auto (build default)"


def _fmt_knob(value: Any) -> str:
    """How a knob reads in the start line: silence and an explicit 0 differ.

    `x or "build default"` prints "build default" for 0, and 0 is a choice for
    every knob here — no checkpoints, no cache reuse. Only None is silence.
    """
    return "build default" if value is None else str(value)


def window_outgrows_pool(window: Any, n_ctx: int) -> bool:
    """Whether one conversation is allowed to outgrow the pool every slot shares.

    `n_ctx` is one pool for all slots (the engine reports `kv_unified = true`);
    `context_window` is what a single conversation may occupy. Nothing derives
    one from the other, so they can disagree — and only this direction is
    silent, because the other merely wastes cells nobody fills.
    """
    return bool(window) and window > n_ctx


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The KV-cache ladder for AUTO: the first rung that fits the card wins. f16 is
# deliberately NOT a rung — on 2026-08-19 an f16 load at 262 144 "succeeded"
# because WDDM does not refuse allocations, it spills them to shared memory:
# 5.2 GiB paged out, prefill collapsed from 784 to 47-51 tok/s, and the memo
# recorded the poisoned rung. f16 remains available as an explicit alias
# choice, where the supervisor warns from the arithmetic and loads anyway.
KV_LADDER = ("q8_0", "q4_0")

# Admission arithmetic, not the child's OOM verdict: bytes per KV element by
# cache type (q8_0 = 34/32 per block, q4_0 = 18/32, f16 = 2).
_KV_BYTES_PER_ELEM = {"f16": 2.0, "bf16": 2.0, "q8_0": 34 / 32, "q4_0": 18 / 32}

# Everything a loaded context costs beyond weights and attention-KV: compute
# buffers, MTP draft contexts, the hybrid blocks' recurrent state, CUDA
# context. Measured on qwen3.8-27B @ 262 144 on b10472 (ADR-040 table):
# 748 + 1360 + 1024 + 324 + 1136 = 4592, rounded up.
_DEFAULT_OVERHEAD_MIB = 4608

# The card is shared with the desktop; a load that consumes it all is the
# paging regime measured 2026-08-17 ("card busy computing nothing, window
# full"). The reserve keeps that from being re-created by arithmetic.
_DESKTOP_RESERVE_MIB = 2048

_OOM_MARKERS = ("out of memory", "failed to allocate", "cuda error")


def _free_vram_mib() -> Optional[int]:
    """Free VRAM of the first NVIDIA GPU in MiB, or None when there is no
    signal. The same nvidia-smi the device-context collector already uses."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        return int(out.stdout.strip().splitlines()[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _total_vram_mib() -> Optional[int]:
    """Physical VRAM of the first NVIDIA GPU in MiB, or None without a signal.

    Admission is budgeted against physical memory, not free memory: free
    moves with the desktop and says nothing about what a load will do to it,
    and the desktop's share is what the reserve constant is for."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        return int(out.stdout.strip().splitlines()[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


_GGUF_VALUE_SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def gguf_effort_dictionary(path: str) -> Optional[Tuple[Tuple[str, ...], Optional[str]]]:
    """(words the chat template accepts, the word it defaults to), or None.

    The template is the authority on effort words and it ships inside the model:
    `tokenizer.chat_template` holds the same jinja the server reports at /props,
    so the dictionary can be read without starting a child. A template that
    guards the value names its own vocabulary in the guard.
    """
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            if struct.unpack("<I", f.read(4))[0] < 2:
                return None
            _, n_kv = struct.unpack("<QQ", f.read(16))

            def read_str():
                n = struct.unpack("<Q", f.read(8))[0]
                return f.read(n).decode("utf-8", errors="replace")

            template = None
            for _ in range(n_kv):
                key = read_str()
                t = struct.unpack("<I", f.read(4))[0]
                if t == 8:
                    value = read_str()
                    if key == "tokenizer.chat_template":
                        template = value
                        break
                elif t == 9:
                    et = struct.unpack("<I", f.read(4))[0]
                    cnt = struct.unpack("<Q", f.read(8))[0]
                    if et == 8:
                        for _ in range(cnt):
                            read_str()
                    elif et in _GGUF_VALUE_SIZES:
                        f.seek(_GGUF_VALUE_SIZES[et] * cnt, 1)
                    else:
                        return None
                elif t in _GGUF_VALUE_SIZES:
                    f.seek(_GGUF_VALUE_SIZES[t], 1)
                else:
                    return None
    except Exception as e:
        logger.warning("effort dictionary unreadable in %s: %s", path, e)
        return None
    if not template:
        logger.warning("no chat template in %s — effort words fall back to the table", path)
        return None
    words = effort_dictionary_of(template)
    if words is None:
        logger.warning(
            "the chat template in %s does not guard reasoning_effort — "
            "effort words fall back to the table", path,
        )
    return words


_EFFORT_GUARD = re.compile(r"reasoning_effort\s+not\s+in\s*\(([^)]*)\)")
_EFFORT_DEFAULT = re.compile(r"reasoning_effort\s*\|\s*default\(\s*'([^']+)'")


def effort_dictionary_of(template: str) -> Optional[Tuple[Tuple[str, ...], Optional[str]]]:
    """The guard's own tuple and the default beside it, or None when unguarded."""
    guard = _EFFORT_GUARD.search(template)
    if not guard:
        return None
    words = tuple(w.strip().strip("'\"") for w in guard.group(1).split(",") if w.strip())
    if not words:
        return None
    default = _EFFORT_DEFAULT.search(template)
    return words, (default.group(1) if default else None)


def _gguf_attention_kv_dims(path: str) -> Optional[Tuple[int, int]]:
    """(attention layer count, kv width per layer) from a GGUF's tensor directory.

    The attn_k weight of every attention block is 2-D (n_embd, n_kv_heads x
    head_dim); the second dim is what the KV cache stores per token per layer.
    Hybrid blocks (Gated DeltaNet here) carry no attn_k and contribute no
    attention KV — their state is fixed size and counted in the overhead
    constant. Returns None when the file cannot be read or has no attention.
    """
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            version = struct.unpack("<I", f.read(4))[0]
            if version < 2:
                return None
            n_tensors, n_kv = struct.unpack("<QQ", f.read(16))

            def read_str():
                n = struct.unpack("<Q", f.read(8))[0]
                return f.read(n).decode("utf-8", errors="replace")

            def skip_value(t):
                if t == 8:
                    read_str()
                elif t == 9:
                    et = struct.unpack("<I", f.read(4))[0]
                    cnt = struct.unpack("<Q", f.read(8))[0]
                    if et == 8:
                        for _ in range(cnt):
                            read_str()
                    elif et in _GGUF_VALUE_SIZES:
                        f.seek(_GGUF_VALUE_SIZES[et] * cnt, 1)
                    else:
                        raise ValueError(f"unknown gguf array type {et}")
                elif t in _GGUF_VALUE_SIZES:
                    f.seek(_GGUF_VALUE_SIZES[t], 1)
                else:
                    raise ValueError(f"unknown gguf value type {t}")

            for _ in range(n_kv):
                read_str()
                skip_value(struct.unpack("<I", f.read(4))[0])

            pat = re.compile(r"^blk\.(\d+)\.attn_k\.weight$")
            nextn_pat = re.compile(r"^blk\.(\d+)\.nextn\.")
            layers = set()
            draft_layers = set()
            kv_width = 0
            for _ in range(n_tensors):
                name = read_str()
                n_dims = struct.unpack("<I", f.read(4))[0]
                dims = struct.unpack(f"<{n_dims}Q", f.read(8 * n_dims))
                f.read(4 + 8)  # type + offset
                m = pat.match(name)
                if m and n_dims == 2:
                    layers.add(int(m.group(1)))
                    kv_width = dims[1]
                nm = nextn_pat.match(name)
                if nm:
                    draft_layers.add(int(nm.group(1)))
            # A block carrying nextn tensors is the MTP draft layer: the loader
            # marks its attn_* unused (blk.64 on the production model) and
            # allocates no KV for it. Counting it was exactly the +6 % the
            # first formula run carried against the loader's own 8704 MiB.
            layers -= draft_layers
            if not layers:
                return None
            return len(layers), kv_width
    except (OSError, ValueError, struct.error):
        return None


def _kv_cache_mib(n_attn_layers: int, kv_width: int, n_ctx: int, cache_type: str) -> int:
    """Attention-KV bytes for one rung: 2 (K and V) x layers x width x ctx x
    bytes-per-element. Predicts exactly the loader's 8704 MiB for q8_0 @
    262 144 on the production model (16 attention layers — the MTP draft
    block is excluded; counting it was the first version's +6 % bias, in the
    safe direction but unexplained until the load log named blk.64 unused)."""
    per_elem = _KV_BYTES_PER_ELEM.get(cache_type)
    if not per_elem:
        return 0
    return int(2 * n_attn_layers * kv_width * per_elem * n_ctx / (1024 * 1024))


def _looks_like_oom(log_lines: List[str]) -> bool:
    joined = "\n".join(log_lines or []).lower()
    return any(marker in joined for marker in _OOM_MARKERS)


# The four lines the engine writes when a parked prefix cannot be laid back
# into the pool. Taken from this machine's own child logs rather than from a
# quote: 2 blocks in the current alias file, 6 in the pre-rename one.
# Matched on bytes, because the position they are remembered by is a byte
# offset into the log: decoding first would drift the two apart on any
# non-ASCII line the child ever writes.
_RESTORE_CELLS_RE = re.compile(rb"state_read_meta: failed to find (\d+) available cells in kv cache")
_RESTORE_SIZE_RE = re.compile(rb"load: failed to restore state with size (\d+)")
_RESTORE_SLOT_RE = re.compile(rb"prompt_load: id\s+(\d+) \| task \S+ \| failed to load prompt from cache")

# How far past the anchor line the companions are looked for. The four arrive
# within two milliseconds of each other; the window only has to survive another
# slot interleaving a line between them.
_RESTORE_BLOCK_BYTES = 4000


def format_restore_refusal(refusal: Dict[str, Any], n_ctx: int) -> str:
    """The sentence the engine does not write: the arithmetic behind a refusal.

    A restore that wanted W cells out of a P-cell pool failed because fewer
    than W were free, so at least P-W were held by other work. When W exceeds
    the pool the parked state could never have fitted it at all.
    """
    wanted = refusal["cells"]
    where = f"slot {refusal['slot']}" if refusal.get("slot") is not None else "an unnamed slot"
    size = f"{refusal['bytes'] / (1024 * 1024):.0f} MiB" if refusal.get("bytes") else "unknown size"
    if wanted >= n_ctx:
        arithmetic = (
            f"the parked state alone wants {wanted} of the {n_ctx}-cell pool, "
            "so it cannot fit whatever else is running"
        )
    else:
        arithmetic = (
            f"{wanted} cells wanted against a {n_ctx}-cell pool, so at least "
            f"{n_ctx - wanted} were held by other conversations"
        )
    return (
        f"host-cache restore refused on {where} ({size} parked): {arithmetic}. "
        "The turn re-reads that prefix from zero; the usage line's reuse% is what it cost."
    )


# `mean len` is tokens emitted per target forward pass, not draft length: it
# equals acceptance × n_max + 1, which reproduces every line in this box's log
# to two decimals. That identity is also the only place a finished child says
# which n_max it ran with — neither the command line nor the log records it.
_DRAFT_RE = re.compile(
    r"draft acceptance = ([\d.]+) \(\s*(\d+) accepted /\s*(\d+) generated\), "
    r"mean len =\s*([\d.]+)"
)


def _parse_draft(tail: str) -> Dict[str, Any]:
    """Speculation counters of the last finished task, or {} when there are none.

    Empty rather than zero: an alias with no `--spec-type` writes no such line,
    and a reader that cannot tell "not drafting" from "drafted nothing" is the
    defect this project already paid for once.
    """
    found = _DRAFT_RE.findall(tail)
    if not found:
        return {}
    rate, accepted, generated, mean_len = found[-1]
    rate, mean_len = float(rate), float(mean_len)
    out: Dict[str, Any] = {
        "draft_acceptance": rate,
        "draft_accepted": int(accepted),
        "draft_generated": int(generated),
        "draft_tokens_per_pass": mean_len,
    }
    if rate > 0:
        n_est = (mean_len - 1) / rate
        if abs(n_est - round(n_est)) <= 0.08 and 1 <= round(n_est) <= 16:
            out["draft_n_max"] = round(n_est)
    return out


def _gguf_mib(path: str) -> int:
    try:
        return os.path.getsize(path) // (1024 * 1024)
    except OSError:
        return 0


class LlamaServerSupervisor:
    """Owns the `llama-server` child process for one alias.

    Responsibilities and nothing else: spawn with flags, health-poll to a
    deadline, capture /props, read /slots through, drain, stop.
    """

    def __init__(self, alias: str, config: Dict[str, Any]):
        self.alias = alias
        self.config = {**DEFAULTS, **{k: v for k, v in config.items() if v is not None}}
        self.port: Optional[int] = None
        self.props: Optional[Dict[str, Any]] = None
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._log_path = DPC_HOME / "logs" / f"llama-server-{alias}.log"
        self._log_fh = None
        self._draining = False
        # A superseded child holds the whole model until it has finished
        # draining, and this card cannot hold two. Set by the provider when this
        # supervisor replaces a live one; `ensure_running` waits on it before
        # spending VRAM, so draining the old child cannot turn into two children.
        self._predecessor: Optional["asyncio.Task"] = None
        self._in_flight = 0
        self._start_lock = asyncio.Lock()
        # Where the restore-refusal scan has already looked. The log is opened
        # "ab" and outlives restarts, so None means "not primed yet": the first
        # scan starts at the end of the file rather than reporting a previous
        # child's refusal as this one's.
        self._restore_scan_offset: Optional[int] = None

    # --- command assembly, pure and table-testable -------------------------

    def build_command(self, binary: Path, port: int, cache_type: Optional[str] = None) -> List[str]:
        c = self.config
        cmd: List[str] = [str(binary)]
        cmd += ["-m", str(c["gguf_path"])]
        cmd += ["-c", str(c["n_ctx"])]
        ctk = cache_type or c["cache_type_k"]
        ctv = cache_type or c["cache_type_v"]
        if ctk:
            cmd += ["-ctk", str(ctk)]
        if ctv:
            cmd += ["-ctv", str(ctv)]
        # `is not None` and not truthiness, on all three of these: 0 is a
        # sentence in each — no layers on the card, no draft tokens, no host
        # cache — and the guard twenty lines below records this same class
        # being found and fixed for `-np` before it was repeated here.
        if c["n_gpu_layers"] is not None:
            cmd += ["-ngl", str(c["n_gpu_layers"])]
        flash_attn = _flash_attn_value(c["flash_attn"])
        if flash_attn is not None:
            cmd += ["--flash-attn", flash_attn]
        if c["mmproj"]:
            cmd += ["--mmproj", str(c["mmproj"])]
        if c["spec_type"] and c["spec_type"] != "none":
            cmd += ["--spec-type", str(c["spec_type"])]
            if c["spec_draft_n_max"] is not None:
                cmd += ["--spec-draft-n-max", str(c["spec_draft_n_max"])]
        # -np is sent whenever it is set, so an explicit 1 reaches the child —
        # the old `> 1` guard ate it and the server fell back to its own 4.
        # --kv-unified only means something above one slot; at one slot
        # unified and split are the same pool.
        if c["ctx_checkpoints"] is not None:
            cmd += ["--ctx-checkpoints", str(c["ctx_checkpoints"])]
        if c["checkpoint_min_step"] is not None:
            cmd += ["--checkpoint-min-step", str(c["checkpoint_min_step"])]
        if c["n_batch"]:
            cmd += ["-b", str(c["n_batch"])]
        if c["n_ubatch"]:
            cmd += ["-ub", str(c["n_ubatch"])]
        if c["n_parallel"]:
            cmd += ["-np", str(c["n_parallel"])]
        # Said out loud in both directions, because the binary's own default is
        # conditional — "enabled if number of slots is auto" — so silence means
        # one thing with `-np` set and the opposite without it. The old guard
        # emitted nothing below two slots, which left an alias that asked for a
        # unified pool alongside an explicit `-np` running a split one.
        if c["kv_unified"] is not None:
            cmd += ["--kv-unified" if c["kv_unified"] else "--no-kv-unified"]
        if c["cache_reuse"] is not None:
            cmd += ["--cache-reuse", str(c["cache_reuse"])]
        if c["cache_ram_mib"] is not None:
            cmd += ["--cache-ram", str(c["cache_ram_mib"])]
        if c["slot_save_path"]:
            cmd += ["--slot-save-path", str(c["slot_save_path"])]
        # `--jinja` is the binary's default, so emitting nothing for False left
        # the engine with jinja on and the control unable to express the only
        # thing it was added for. The negative form exists; use it.
        if c["jinja"] is not None:
            cmd += ["--jinja" if c["jinja"] else "--no-jinja"]
        cmd += ["--host", "127.0.0.1", "--port", str(port)]
        cmd += [str(a) for a in c["extra_args"]]
        return cmd

    def build_env(self, binary: Path) -> Dict[str, str]:
        env = dict(os.environ)
        backend = find_cuda_backend(binary.parent)
        if backend:
            env["GGML_BACKEND_PATH"] = str(backend)
        return env

    # --- lifecycle -----------------------------------------------------------

    async def ensure_running(self) -> Dict[str, Any]:
        """The server is up and its /props are returned; starts it if needed.

        A cache type the alias set explicitly is started once, as configured.
        Without one, the KV ladder in `_launch_auto_kv` picks the rung that
        fits the free VRAM the card has right now."""
        async with self._start_lock:
            if self._draining:
                raise LlamaServerError(f"llama-server[{self.alias}] is draining; new calls refused")
            if self._proc and self._proc.returncode is None and self.props:
                return self.props
            await self._await_predecessor()
            binary = ensure_binary(self.config)
            self.port = self.port or _free_port()
            if self.config.get("cache_type_k") is not None or self.config.get("cache_type_v") is not None:
                self._warn_if_explicit_type_exceeds_budget()
                return await self._launch(binary)
            return await self._launch_auto_kv(binary)

    # --- the KV ladder --------------------------------------------------------

    def _fit_key(self) -> str:
        c = self.config
        return ":".join(str(x) for x in (
            _gguf_mib(c["gguf_path"]), c["n_ctx"], c["n_gpu_layers"],
            c["n_parallel"], c["spec_draft_n_max"],
        ))

    @staticmethod
    def _fit_memo_path() -> Path:
        return install_root() / ".dpc-kv-fit.json"

    def _read_fit_memo(self) -> Dict[str, Any]:
        try:
            return json.loads(self._fit_memo_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_fit_memo(self, key: str, cache_type: str, free_mib: Optional[int]) -> None:
        # Best-effort: a memo that cannot be written only costs the ladder a
        # re-run, never a start.
        try:
            memo = self._read_fit_memo()
            memo[key] = {"type": cache_type, "free_mib": free_mib or 0, "ts": int(time.time())}
            path = self._fit_memo_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(memo), encoding="utf-8")
        except (OSError, ValueError):
            logger.debug("llama-server[%s]: could not write the KV fit memo", self.alias, exc_info=True)

    def _overhead_mib(self) -> int:
        """The alias's own overhead when it names one, the measured default
        otherwise. Whichever it is, the arithmetic below prints it."""
        configured = self.config.get("vram_overhead_mib")
        try:
            return int(configured) if configured is not None else _DEFAULT_OVERHEAD_MIB
        except (TypeError, ValueError):
            logger.warning(
                "llama-server[%s]: vram_overhead_mib %r is not a number — using %d",
                self.alias, configured, _DEFAULT_OVERHEAD_MIB,
            )
            return _DEFAULT_OVERHEAD_MIB

    def _admission(self) -> Optional[Tuple[int, int, int]]:
        """(budget_mib, weights_mib, kv_dims) for the arithmetic, or None when
        the card or the model cannot be sized and the ladder must fall back to
        listening for the child's own failures. Physical VRAM minus the desktop
        reserve is the budget: free memory moves with the desktop and predicts
        nothing about what the load will do to it."""
        total = _total_vram_mib()
        if total is None:
            return None
        dims = _gguf_attention_kv_dims(self.config["gguf_path"])
        weights = _gguf_mib(self.config["gguf_path"])
        if dims is None or not weights:
            return None
        return total - _DESKTOP_RESERVE_MIB, weights, dims

    def _predicted_total_mib(self, admission: Tuple[int, int, int], cache_type: str) -> int:
        _, weights, (layers, width) = admission
        return weights + _kv_cache_mib(layers, width, self.config["n_ctx"], cache_type) + self._overhead_mib()

    def _warn_if_explicit_type_exceeds_budget(self) -> None:
        """An alias that names a KV type owns the consequence — the arithmetic
        warns, it does not refuse."""
        admission = self._admission()
        if admission is None:
            return
        budget, _, _ = admission
        cache_type = self.config.get("cache_type_k")
        predicted = self._predicted_total_mib(admission, cache_type)
        if predicted > budget:
            logger.warning(
                "llama-server[%s]: KV %s explicitly configured — arithmetic predicts "
                "%d MiB against a %d MiB budget; loading anyway (explicit choice "
                "overrides arithmetic)",
                self.alias, cache_type, predicted, budget,
            )

    async def _launch_auto_kv(self, binary: Path) -> Dict[str, Any]:
        """Start at the best rung the arithmetic and the card both clear.

        Two gates per rung, in order: admission arithmetic (weights + KV bytes
        for the rung + fixed overhead + desktop reserve <= physical VRAM) and,
        for what passes, the child's own out-of-memory verdict — which under
        WDDM is a late and unreliable signal, since Windows pages instead of
        refusing (2026-08-19: an f16 rung "fit" that way and prefill collapsed
        16x). A rung that fit is memoised with the free-VRAM level it fit at,
        and reused only while the card has at least that much free again — a
        busier card re-runs the ladder. A memoised rung the arithmetic now
        refuses falls through to the full ladder and is allowed to rewrite
        the memo."""
        key = self._fit_key()
        free = _free_vram_mib()
        memo = self._read_fit_memo().get(key)
        memo_hit = bool(memo and free is not None and free >= (memo.get("free_mib") or 0))
        admission = self._admission()
        if memo_hit:
            candidates = [memo["type"]]
        else:
            gguf_mib = _gguf_mib(self.config["gguf_path"])
            if free is not None and gguf_mib and free < gguf_mib + 4096:
                raise LlamaServerError(
                    f"llama-server[{self.alias}]: {free} MiB free on the GPU against a "
                    f"{gguf_mib} MiB model — no KV type will fit; free the card or point "
                    "the alias at a smaller model"
                )
            candidates = list(KV_LADDER)
        if admission is not None:
            budget, weights, _ = admission
            for rung in KV_LADDER:
                if rung not in candidates:
                    candidates.append(rung)
            for rung in list(candidates):
                predicted = self._predicted_total_mib(admission, rung)
                if predicted > budget:
                    logger.warning(
                        "llama-server[%s]: KV %s refused by arithmetic — predicted %d MiB "
                        "(weights %d + kv %d + overhead %d) against a %d MiB budget",
                        self.alias, rung, predicted, weights,
                        predicted - weights - self._overhead_mib(), self._overhead_mib(),
                        budget,
                    )
                    candidates.remove(rung)
                    memo_hit = memo_hit and rung != memo.get("type")
            if not candidates:
                raise LlamaServerError(
                    f"llama-server[{self.alias}]: no KV rung fits the arithmetic — "
                    "the model is too large for this card at n_ctx "
                    f"{self.config['n_ctx']}"
                )
        for cache_type in candidates:
            try:
                props = await self._launch(binary, cache_type=cache_type)
            except LlamaServerError as e:
                if not _looks_like_oom(e.log_lines) or cache_type == candidates[-1]:
                    raise
                logger.warning(
                    "llama-server[%s]: KV %s did not fit (out of memory); stepping down",
                    self.alias, cache_type,
                )
                continue
            if not memo_hit:
                self._write_fit_memo(key, cache_type, free)
            return props
        raise LlamaServerError(f"llama-server[{self.alias}] unreachable ladder state")

    def log_start(
        self,
        cache_type: Optional[str],
        env: Dict[str, str],
        binary: Optional[Path] = None,
    ) -> None:
        """What the child was actually given, in one line, plus the one
        disagreement between two of those numbers that fails silently.

        The prompt cache is why the line exists at all: its size can arrive as
        LLAMA_ARG_CACHE_RAM from the environment, where it is invisible in the
        command line, in the config and in the child's own captured log — three
        readers of that log reached three different answers about it in one
        afternoon (2026-08-20).

        The speculation pair is here for the third time the same thing happened.
        On 2026-08-23 the owner changed `spec_draft_n_max` from 4 to 3 to compare
        the acceptance the two produce — and neither this line nor the child's own
        log records which value a child ran with, so the comparison would have
        rested on someone remembering what the config said at the time. A knob
        worth changing is a knob worth reading back.

        `binary` is here for the same reason, one reader later. The child log is
        one file per alias and carries no line naming the executable that wrote
        it, so two builds' starts are indistinguishable in it — and on
        2026-08-22 a reviewer who did the right thing and read the whole file
        still drew the opposite conclusion from the right lines, because the
        failures were the pin and the successes a hand-built binary. Since
        `binary_path` reached the provider form, two builds side by side is an
        ordinary state rather than a laboratory one.
        """
        _cache = c_ram if (c_ram := self.config.get("cache_ram_mib")) else (
            f"{env.get('LLAMA_ARG_CACHE_RAM')} (from environment)"
            if env.get("LLAMA_ARG_CACHE_RAM") else "build default"
        )
        n_ctx = self.config["n_ctx"]
        window = self.config.get("context_window")
        logger.info(
            "llama-server[%s] starting on :%s (binary=%s, n_ctx=%s, context_window=%s, "
            "kv=%s, flash_attn=%s, cache_ram=%s, ctx_checkpoints=%s, checkpoint_min_step=%s, "
            "n_ubatch=%s, cache_reuse=%s, spec_type=%s, spec_draft_n_max=%s, "
            "vram_overhead=%s)",
            self.alias, self.port, binary if binary is not None else "unknown",
            n_ctx, _fmt_knob(window),
            cache_type or "configured",
            _flash_attn_effective(
                self.config.get("flash_attn"),
                cache_type or self.config.get("cache_type_v"),
            ),
            _cache,
            _fmt_knob(self.config.get("ctx_checkpoints")),
            _fmt_knob(self.config.get("checkpoint_min_step")),
            _fmt_knob(self.config.get("n_ubatch")),
            _fmt_knob(self.config.get("cache_reuse")),
            _fmt_knob(self.config.get("spec_type")),
            _fmt_knob(self.config.get("spec_draft_n_max")),
            f"{self._overhead_mib()} MiB"
            + ("" if self.config.get("vram_overhead_mib") else " (measured elsewhere)"),
        )
        if window_outgrows_pool(window, n_ctx):
            # The direction that fails without a word: the agent fills to a
            # limit the engine never allocated, and the guard meant to refuse
            # before the model dies is calibrated on the larger number.
            logger.warning(
                "llama-server[%s]: context_window %s is larger than the KV pool "
                "n_ctx %s — one conversation may grow past everything the child "
                "allocated for all of its slots together",
                self.alias, window, n_ctx,
            )

    async def _launch(self, binary: Path, cache_type: Optional[str] = None) -> Dict[str, Any]:
        cmd = self.build_command(binary, self.port, cache_type=cache_type)
        env = self.build_env(binary)
        # What the child was actually given, in one line. The prompt cache is the
        # reason this exists: its size can arrive as LLAMA_ARG_CACHE_RAM from the
        # environment, where it is invisible in the command line, in the config
        # and in the child's own captured log — three readers of that log reached
        # three different answers about it in one afternoon (2026-08-20).
        self.log_start(cache_type, env, binary)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self._log_path, "ab")
        self.prime_restore_scan()
        self._proc = await self._spawn(cmd, env)
        try:
            await self._wait_healthy()
            self.props = await self._get("/props")
            return self.props
        except Exception as e:
            await self.stop()
            raise

    async def _spawn(self, cmd: List[str], env: Dict[str, str]) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=self._log_fh,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=_CREATION_NEW_PROCESS_GROUP,
        )

    async def _wait_healthy(self) -> None:
        deadline = time.monotonic() + float(self.config["start_timeout_s"])
        while time.monotonic() < deadline:
            if self._proc and self._proc.returncode is not None:
                raise LlamaServerError(
                    f"llama-server[{self.alias}] exited with {self._proc.returncode} before "
                    f"becoming healthy",
                    self.tail_log(),
                )
            if await self._health_ok():
                return
            await asyncio.sleep(0.5)
        raise LlamaServerError(
            f"llama-server[{self.alias}] did not become healthy within "
            f"{self.config['start_timeout_s']}s",
            self.tail_log(),
        )

    async def _health_ok(self) -> bool:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"http://127.0.0.1:{self.port}/health")
                return resp.status_code == 200
        except Exception:
            return False

    async def _get(self, path: str) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"http://127.0.0.1:{self.port}{path}")
            resp.raise_for_status()
            return resp.json()

    async def slots(self) -> Dict[str, Any]:
        """Live slot occupancy, read through without interpretation."""
        if not self.port:
            raise LlamaServerError(f"llama-server[{self.alias}] is not running")
        return await self._get("/slots")

    def _read_tail(self, window: int = 65536) -> Optional[Tuple[bytes, int]]:
        """(the last `window` bytes, the absolute offset they start at).

        Bytes rather than text: one of the two readers remembers where it
        stopped, and that position has to mean the same thing as the seek.
        """
        try:
            with open(self._log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                start = max(0, size - window)
                f.seek(start)
                return f.read(), start
        except OSError:
            return None

    def prime_restore_scan(self) -> None:
        """Watch from here on. Called at launch, so the previous child's lines
        in this append-only file are never reported as this one's."""
        try:
            self._restore_scan_offset = self._log_path.stat().st_size
        except OSError:
            self._restore_scan_offset = 0

    def new_restore_refusals(self) -> List[Dict[str, Any]]:
        """Restore refusals appended since the last scan, oldest first.

        The engine logs a prompt-cache load only when it FAILS, so this is the
        one place the host cache is visible at all. Keyed by the anchor line's
        absolute offset: the window moves forward, the offset does not go back.
        """
        read = self._read_tail()
        if read is None:
            return []
        data, start = read
        end = start + len(data)
        if self._restore_scan_offset is None:
            self._restore_scan_offset = end
            return []
        if self._restore_scan_offset > end:
            # The file shrank — rotated or truncated under us. What we counted
            # is gone; watch what is there now, including its first byte.
            self._restore_scan_offset = start - 1
        out: List[Dict[str, Any]] = []
        for m in _RESTORE_CELLS_RE.finditer(data):
            offset = start + m.start()
            if offset <= self._restore_scan_offset:
                continue
            block = data[m.end():m.end() + _RESTORE_BLOCK_BYTES]
            size_m = _RESTORE_SIZE_RE.search(block)
            slot_m = _RESTORE_SLOT_RE.search(block)
            out.append({
                "cells": int(m.group(1)),
                "bytes": int(size_m.group(1)) if size_m else None,
                "slot": int(slot_m.group(1)) if slot_m else None,
                "offset": offset,
            })
        if out:
            self._restore_scan_offset = out[-1]["offset"]
        return out

    def log_restore_refusals(self) -> int:
        """Say what the child said, with the arithmetic it does not carry."""
        refusals = self.new_restore_refusals()
        for refusal in refusals:
            logger.warning(
                "llama-server[%s]: %s",
                self.alias, format_restore_refusal(refusal, int(self.config["n_ctx"])),
            )
        return len(refusals)

    def tail_log(self, n: int = 12) -> List[str]:
        try:
            with open(self._log_path, "rb") as f:
                lines = f.read().decode("utf-8", errors="replace").splitlines()
            return lines[-n:]
        except OSError:
            return []

    def supersedes(self, drain_task: Optional["asyncio.Task"]) -> None:
        """Record the drain of the child this supervisor replaces."""
        self._predecessor = drain_task

    async def _await_predecessor(self) -> None:
        """Do not start until the superseded child has actually let go.

        Without this, draining the old child only trades one failure for
        another: the old one keeps its ~25 GB while a call that arrives during
        the drain starts a second child on a card that cannot hold both.

        The wait re-checks itself, because the world moves while it waits. Two
        saves inside one turn make this supervisor a predecessor in its turn:
        it is retired and set draining while it is still queued behind the child
        it was replacing. Reading `_draining` once before the wait — which is
        what `ensure_running` does — cannot see that, and the supervisor would
        wake up and start a child nobody wants, on a card that by then holds
        another. A second predecessor arriving during the wait is the same
        story from the other side, and the loop covers both.
        """
        while True:
            task, self._predecessor = self._predecessor, None
            if task is None or task.done():
                break
            logger.info(
                "llama-server[%s]: waiting for the superseded child to finish its "
                "in-flight work and release the card before starting",
                self.alias,
            )
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a failed drain must not block the successor
                logger.warning(
                    "llama-server[%s]: the superseded child's drain ended badly (%s); "
                    "starting anyway", self.alias, exc,
                )
            if self._draining:
                raise LlamaServerError(
                    f"llama-server[{self.alias}] was itself superseded while waiting "
                    "for its predecessor; refusing to start a child nobody holds"
                )

    def call_slot(self):
        """Claim one in-flight call; refuses while draining, so drain can wait.

        Deliberately synchronous: callers use it as `async with call_slot():`
        and an `async def` here would hand them a coroutine instead of the
        slot — the exact TypeError Johnny hit live on 2026-08-19 21:26, which
        every test missed because the fakes had the right shape and the one
        real-supervisor test awaited the coroutine instead."""
        if self._draining:
            raise LlamaServerError(f"llama-server[{self.alias}] is draining; new calls refused")
        self._in_flight += 1
        return self._Slot(self)

    class _Slot:
        def __init__(self, supervisor: "LlamaServerSupervisor"):
            self._supervisor = supervisor

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self._supervisor._in_flight -= 1
            return False

    async def drain(self, timeout: Optional[float] = 120.0) -> None:
        """Refuse new work, let in-flight calls finish, then stop.

        `timeout=None` waits as long as the work takes. That is Mike's rule for
        a configuration change, 2026-08-22: a save must never cut a generation
        that is already running, and an agent turn on 40-170K context can run
        for minutes. Shutdown does not come through here — it calls `stop`
        directly — so an unbounded wait cannot hang the process exit.
        """
        self._draining = True
        try:
            if timeout is None:
                await self._wait_idle()
            else:
                await asyncio.wait_for(self._wait_idle(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "llama-server[%s]: drain timed out with %d call(s) still in flight",
                self.alias,
                self._in_flight,
            )
        await self.stop()

    async def _wait_idle(self) -> None:
        # A silent wait is the shape this project keeps rediscovering: the work
        # is happening, nothing says so, and the operator concludes it hung. One
        # line every 15 s costs nothing and answers "is it stuck or is it busy".
        waited = 0.0
        while self._in_flight > 0:
            await asyncio.sleep(0.2)
            waited += 0.2
            if waited % 15 < 0.2:
                logger.info(
                    "llama-server[%s]: draining, %d call(s) still in flight after %.0fs",
                    self.alias,
                    self._in_flight,
                    waited,
                )

    async def stop(self) -> None:
        """Ask the child to stop with a signal that lets it flush, then wait.

        terminate() is banned in this file: on Windows it discards the
        child's stdio buffers, which is how a week of server logs were lost.
        """
        proc, self._proc = self._proc, None
        self.props = None
        if proc is None or proc.returncode is not None:
            self._close_log()
            return
        try:
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGINT)
            await asyncio.wait_for(proc.wait(), timeout=20.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            proc.kill()
            await proc.wait()
        finally:
            self._close_log()
            logger.info("llama-server[%s] stopped (rc=%s)", self.alias, proc.returncode)

    def last_task_timings(self) -> Optional[Dict[str, Any]]:
        """Exact prefill/decode rates of the most recently finished task, read
        from the child's own print_timing lines - the engine's numbers, not an
        estimate. Gives the phase split to non-streaming callers (the agents'
        tools path has no first-chunk boundary to time it by itself).

        Caveat, stated: with concurrent slots the last block in the file
        belongs to whichever task finished last - under the supervisor's
        serialized traffic that is the caller's own task."""
        import re
        read = self._read_tail()
        if read is None:
            return None
        tail = read[0].decode("utf-8", errors="replace")
        # [\d.]+ in the rate capture: the rate is fractional ("438.86 tokens
        # per second") and a \d+ capture eats only its tail ("86").
        pre = re.findall(
            r"prompt eval time =\s+[\d.]+ ms /\s+(\d+) tokens \(.*?([\d.]+) tokens per second\)",
            tail,
        )
        # (?<!prompt ) — "prompt eval time" lines contain "eval time" as a
        # substring; without the lookbehind the decode capture lands on the
        # true eval line only by line ORDER inside a block (review: works-by-
        # order is the class this codebase burns out). The negative lookbehind
        # identifies decode lines by the absence of the marker, not by position.
        dec = re.findall(
            r"(?<!prompt )eval time =\s+[\d.]+ ms /\s+(\d+) tokens \(.*?([\d.]+) tokens per second\)",
            tail,
        )
        if not pre or not dec:
            return None
        n_prompt, prefill_rate = pre[-1]
        n_gen, decode_rate = dec[-1]
        out = {
            "prefill_tok_s": int(float(prefill_rate)),
            "decode_tok_s": int(float(decode_rate)),
            "engine_prompt_tokens": int(n_prompt),
            "engine_gen_tokens": int(n_gen),
        }
        out.update(_parse_draft(tail))
        return out

    def _close_log(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None
