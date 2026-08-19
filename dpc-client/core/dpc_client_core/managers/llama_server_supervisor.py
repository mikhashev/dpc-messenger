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
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .llama_server_fetcher import DPC_HOME, ensure_binary, find_cuda_backend

logger = logging.getLogger(__name__)

# The child needs its own process group for CTRL_BREAK_EVENT to reach it —
# and only that group, so the signal does not take the backend down with it.
_CREATION_NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

DEFAULTS: Dict[str, Any] = {
    "n_ctx": 262144,
    # Measured twice on 2026-08-19: the f16 default does not fit the card this
    # fleet owns — the first live call (18:09) died with CUDA out-of-memory at
    # model load (weights + f16 KV at 262K > 32 GB), while q8_0 ran every probe
    # of the day at 31.9 of 32 GB and Probe D found it quality-neutral against
    # f16 on retrieval at 233K. A default that cannot start is not a default;
    # an alias that wants f16 back says so explicitly.
    "cache_type_k": "q8_0",
    "cache_type_v": "q8_0",
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
    "n_parallel": 1,
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

    def build_command(self, binary: Path, port: int) -> List[str]:
        c = self.config
        cmd: List[str] = [str(binary)]
        cmd += ["-m", str(c["gguf_path"])]
        cmd += ["-c", str(c["n_ctx"])]
        if c["cache_type_k"]:
            cmd += ["-ctk", str(c["cache_type_k"])]
        if c["cache_type_v"]:
            cmd += ["-ctv", str(c["cache_type_v"])]
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
        if c["n_parallel"] and c["n_parallel"] > 1:
            cmd += ["-np", str(c["n_parallel"])]
            if c["kv_unified"]:
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
        """The server is up and its /props are returned; starts it if needed."""
        async with self._start_lock:
            if self._draining:
                raise LlamaServerError(f"llama-server[{self.alias}] is draining; new calls refused")
            if self._proc and self._proc.returncode is None and self.props:
                return self.props
            binary = ensure_binary(self.config)
            self.port = self.port or _free_port()
            cmd = self.build_command(binary, self.port)
            env = self.build_env(binary)
            logger.info("llama-server[%s] starting on :%d", self.alias, self.port)
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

    async def call_slot(self):
        """Claim one in-flight call; refuses while draining, so drain can wait."""
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

    def _close_log(self) -> None:
        if self._log_fh:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None
