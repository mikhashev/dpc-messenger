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
    "flash_attn": False,
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
    "kv_unified": True,
    "cache_ram_mib": None,
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
_FIXED_OVERHEAD_MIB = 4608

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
        self._in_flight = 0
        self._start_lock = asyncio.Lock()

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
        if c["n_gpu_layers"]:
            cmd += ["-ngl", str(c["n_gpu_layers"])]
        if c["flash_attn"]:
            cmd += ["--flash-attn"]
        if c["mmproj"]:
            cmd += ["--mmproj", str(c["mmproj"])]
        if c["spec_type"] and c["spec_type"] != "none":
            cmd += ["--spec-type", str(c["spec_type"])]
            if c["spec_draft_n_max"]:
                cmd += ["--spec-draft-n-max", str(c["spec_draft_n_max"])]
        # -np is sent whenever it is set, so an explicit 1 reaches the child —
        # the old `> 1` guard ate it and the server fell back to its own 4.
        # --kv-unified only means something above one slot; at one slot
        # unified and split are the same pool.
        if c["n_batch"]:
            cmd += ["-b", str(c["n_batch"])]
        if c["n_ubatch"]:
            cmd += ["-ub", str(c["n_ubatch"])]
        if c["n_parallel"]:
            cmd += ["-np", str(c["n_parallel"])]
            if c["n_parallel"] > 1 and c["kv_unified"]:
                cmd += ["--kv-unified"]
        if c["cache_ram_mib"]:
            cmd += ["--cache-ram", str(c["cache_ram_mib"])]
        if c["slot_save_path"]:
            cmd += ["--slot-save-path", str(c["slot_save_path"])]
        if c["jinja"]:
            cmd += ["--jinja"]
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
        return weights + _kv_cache_mib(layers, width, self.config["n_ctx"], cache_type) + _FIXED_OVERHEAD_MIB

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
                        predicted - weights - _FIXED_OVERHEAD_MIB, _FIXED_OVERHEAD_MIB, budget,
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

    async def _launch(self, binary: Path, cache_type: Optional[str] = None) -> Dict[str, Any]:
        cmd = self.build_command(binary, self.port, cache_type=cache_type)
        env = self.build_env(binary)
        logger.info(
            "llama-server[%s] starting on :%d (kv=%s)",
            self.alias, self.port, cache_type or "configured",
        )
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self._log_path, "ab")
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

    def tail_log(self, n: int = 12) -> List[str]:
        try:
            with open(self._log_path, "rb") as f:
                lines = f.read().decode("utf-8", errors="replace").splitlines()
            return lines[-n:]
        except OSError:
            return []

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

    async def drain(self, timeout: float = 120.0) -> None:
        """Refuse new work, let in-flight calls finish, then stop."""
        self._draining = True
        try:
            await asyncio.wait_for(self._wait_idle(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "llama-server[%s]: drain timed out with %d call(s) still in flight",
                self.alias,
                self._in_flight,
            )
        await self.stop()

    async def _wait_idle(self) -> None:
        while self._in_flight > 0:
            await asyncio.sleep(0.2)

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
        try:
            with open(self._log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            return None
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
        return {
            "prefill_tok_s": int(float(prefill_rate)),
            "decode_tok_s": int(float(decode_rate)),
            "engine_prompt_tokens": int(n_prompt),
            "engine_gen_tokens": int(n_gen),
        }

    def _close_log(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None
