"""A cancelled start, on a real port: the next start spawns only once the first child is gone.

The incident of 2026-10-22 was about a port — a second launcher spawned while the
first still held :64276 — and every other test of it fakes the process, where no
port is ever held. Here the supervisor's own `_spawn`, `/health` and `/props`
reads and stop signal (CTRL_BREAK on Windows, SIGINT elsewhere) run against real
child processes: a stdlib HTTP server that binds 127.0.0.1:<port>, answers
/health 503 until a file says it is loaded, and takes `exit_delay` seconds to
exit once asked, like a launcher unloading its engine.

Only the command is swapped (`build_command`); the supervisor is the
llama-server base, whose start, stop and abandoned-stop paths the Strata
launcher inherits.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import LlamaServerSupervisor

CHILD = r'''
import http.server, json, os, signal, sys, threading
port, ready, exit_delay, marks = int(sys.argv[1]), sys.argv[2], float(sys.argv[3]), sys.argv[4]

# Armed before the bind and before the "bound" mark: a test that reacts to the
# mark may signal at once, and a signal that finds no handler is lost.
def asked_to_stop(signum, frame):
    open(os.path.join(marks, f"{os.getpid()}.stopping"), "w").close()
    threading.Timer(exit_delay, lambda: os._exit(0)).start()

signal.signal(signal.SIGBREAK if os.name == "nt" else signal.SIGINT, asked_to_stop)

# Threaded, as a real llama-server is. A single-threaded server sits in recv on
# a connection whose client was cancelled before it sent anything, and Windows
# runs a Python signal handler only when the main thread comes back from that
# call: the stop signal is then not heard until the connection goes away.
class Server(http.server.ThreadingHTTPServer):
    # Windows SO_REUSEADDR lets a second socket bind over a live listener;
    # without it a second child dies at bind, as a real one does.
    allow_reuse_address = os.name != "nt"

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            code, body = (200, {"status": "ok"}) if os.path.exists(ready) else (503, {"status": "loading"})
        elif self.path == "/props":
            code, body = 200, {"pid": os.getpid(), "ppid": os.getppid()}
        else:
            code, body = 404, {}
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

try:
    server = Server(("127.0.0.1", port), Handler)
except OSError as exc:
    open(os.path.join(marks, f"{os.getpid()}.bindfail"), "w").write(str(exc))
    sys.exit(98)
open(os.path.join(marks, f"{os.getpid()}.bound"), "w").close()

server.serve_forever(poll_interval=0.05)
'''


def _port_is_free(port: int) -> bool:
    """Bound the way the child binds, so "free" means a child could take it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


async def _until(predicate, timeout):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


class _Rig:
    def __init__(self, tmp_path: Path, monkeypatch, exit_delay: float):
        monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
        monkeypatch.setattr(base_mod, "ensure_binary", lambda config: Path(sys.executable))
        monkeypatch.setattr(base_mod, "find_cuda_backend", lambda path: None)
        monkeypatch.setattr(base_mod, "_free_vram_mib", lambda: None)
        monkeypatch.setattr(base_mod, "_total_vram_mib", lambda: None)
        script = tmp_path / "child.py"
        script.write_text(CHILD, encoding="utf-8")
        self.ready = tmp_path / "loaded"
        self.marks = tmp_path / "marks"
        self.marks.mkdir()
        self.sup = sup = LlamaServerSupervisor(
            "real-port", {"gguf_path": str(tmp_path / "m.gguf"), "start_timeout_s": 20.0})
        sup.port = base_mod._free_port()
        sup.build_command = lambda binary, port, cache_type=None: [
            sys.executable, "-u", str(script), str(port), str(self.ready),
            str(exit_delay), str(self.marks),
        ]
        self.spawned = []
        # Per spawn: (earlier children still alive, whether the port was free).
        self.at_spawn = []

        async def spawn(cmd, env):
            self.at_spawn.append((
                [p.pid for p in self.spawned if p.returncode is None],
                _port_is_free(sup.port),
            ))
            proc = await LlamaServerSupervisor._spawn(sup, cmd, env)
            self.spawned.append(proc)
            return proc

        sup._spawn = spawn

    def bound(self):
        return sorted(p.stem for p in self.marks.glob("*.bound"))

    def stopping(self):
        """Children that have received the stop signal and are counting down."""
        return sorted(p.stem for p in self.marks.glob("*.stopping"))

    def bind_failures(self):
        return sorted(p.name for p in self.marks.glob("*.bindfail"))

    async def start_and_wait_for_the_bind(self):
        task = asyncio.ensure_future(self.sup.ensure_running())
        n = len(self.bound())
        await _until(lambda: len(self.bound()) > n or task.done(), 15)
        assert not task.done(), "the start ended before its child bound the port"
        return task

    async def cleanup(self):
        try:
            await asyncio.wait_for(self.sup.stop(), 30)
        finally:
            for proc in self.spawned:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            base_mod._CARD_CLAIMS.pop(self.sup, None)


class TestACancelledStartOnARealPort:
    @pytest.mark.asyncio
    async def test_the_next_start_spawns_only_after_the_first_child_has_left_the_port(
        self, tmp_path, monkeypatch,
    ):
        rig = _Rig(tmp_path, monkeypatch, exit_delay=0.5)
        try:
            a = await rig.start_and_wait_for_the_bind()
            a.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(a, 30)
            first = rig.spawned[0]
            assert first.returncode is not None, "the cancelled start's child is gone"

            rig.ready.touch()
            props = await asyncio.wait_for(rig.sup.ensure_running(), 30)
            assert len(rig.spawned) == 2
            assert rig.spawned[1].pid in (props["pid"], props["ppid"]), "the props came from this start's child"
            assert rig.at_spawn[1] == ([], True), "spawned over no live child and a free port"
            assert rig.bind_failures() == []
        finally:
            await rig.cleanup()

    @pytest.mark.asyncio
    async def test_a_start_cancelled_twice_and_a_cancelled_waiter_still_leave_one_child(
        self, tmp_path, monkeypatch,
    ):
        """The second cancel leaves the first child exiting, the waiter behind it
        is cancelled too, and the old child answers /health 200 while it exits:
        the third start must wait it out, not take its answers.

        The cancels are tied to events, not to a clock. The first /health poll of
        a child still binding can swallow a cancel for up to the client's 2 s
        timeout (a refused connect on Windows is that slow), and a stop that has
        not begun yet is not the stop the second cancel is meant to hit: both
        cancels then land before it and the start ends only after the child has
        exited (`assert 0 is None`, about 1 run in 12). So the second cancel waits
        for the child's own mark that the stop signal reached it, and the child's
        exit delay (3 s) is long against the few milliseconds left to the check."""
        rig = _Rig(tmp_path, monkeypatch, exit_delay=3.0)
        try:
            a = await rig.start_and_wait_for_the_bind()
            a.cancel()
            await _until(lambda: rig.stopping() or a.done(), 15)
            assert not a.done(), "the start ended before the stop reached the child"
            stop = rig.sup._abandoned_stop
            assert stop is not None and not stop.done(), "the stop is running on its own task"
            a.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(a, 30)
            first = rig.spawned[0]
            assert first.returncode is None, "the first child is still exiting"
            rig.ready.touch()

            b = asyncio.ensure_future(rig.sup.ensure_running())
            # Holding the start lock, b is parked on the abandoned stop.
            await _until(lambda: rig.sup._start_lock.locked(), 5)
            await asyncio.sleep(0.05)
            b.cancel()
            with pytest.raises(asyncio.CancelledError):
                await b

            props = await asyncio.wait_for(rig.sup.ensure_running(), 30)
            assert first.returncode is not None
            assert len(rig.spawned) == 2, "one child after the first, not one per caller"
            assert rig.at_spawn[1] == ([], True), "spawned over no live child and a free port"
            assert rig.spawned[1].pid in (props["pid"], props["ppid"]), "the props came from this start's child"
            assert rig.bind_failures() == []
        finally:
            await rig.cleanup()
