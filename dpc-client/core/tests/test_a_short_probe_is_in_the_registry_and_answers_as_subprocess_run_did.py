"""`run_probe`: a short command in the process registry, answering as `subprocess.run` did.

ADR-046 point 5 moves the device probes (`nvidia-smi`, `lspci`, a tool's
`--version`, `ip addr`) and the agent sandbox's git helpers from `subprocess.run`
to `run_probe` in `dpc_agent/tools/process.py`. The sites kept their code: each
reads a `CompletedProcess` and catches the exceptions it caught before, so what
is asserted is that the helper gives the same return and raises the same
exceptions, that it registers the child as a subsystem, and point 7's rule: once
the backend's stop began a git helper is refused, a device probe is not, because
the stop itself reads the card through one after Strata's engine is gone.

Real children (`sys.executable`) where the point is a real exit; the registry is
the empty in-memory one `conftest.py` gives every test.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import time
import types

import pytest

from dpc_client_core import process_registry as pr
from dpc_client_core.dpc_agent.tools import process as process_mod
from dpc_client_core.dpc_agent.tools.process import run_probe
from dpc_client_core.process_registry import ProcessRegistryClosed, SpawnOrigin, spawn_origin

PY = sys.executable
WHICH = shutil.which  # the real one: the tests patch the module attribute


def _py(code: str) -> list:
    return [PY, "-c", code]


def _only_entry():
    (entry,) = pr.get_registry().entries()
    return entry


class TestTheSameAnswerAsSubprocessRun:
    def test_text_mode_gives_what_subprocess_run_gave(self):
        # Bytes, so the child's own newline translation does not differ by platform.
        cmd = _py("import sys; sys.stdout.buffer.write(b'a\\r\\nb\\rc\\n'); sys.stderr.write('e'); sys.exit(3)")
        expected = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        got = run_probe(cmd, launcher="device_probe", text=True, timeout=30)
        assert isinstance(got, subprocess.CompletedProcess)
        assert (got.returncode, got.stdout, got.stderr) == (expected.returncode, expected.stdout, expected.stderr)
        assert got.stdout == "a\nb\nc\n"

    def test_without_text_the_output_is_bytes_as_written(self):
        cmd = _py("import sys; sys.stdout.buffer.write(b'\\xff\\r\\n')")
        expected = subprocess.run(cmd, capture_output=True, timeout=30)
        got = run_probe(cmd, launcher="git_helper", timeout=30)
        assert got.stdout == expected.stdout == b"\xff\r\n"

    def test_text_is_decoded_with_the_locales_encoding_and_strict_as_text_mode_does(self):
        """0xE9 is 'é' in a Western code page and invalid UTF-8: whichever this
        machine's locale is, the helper and `subprocess.run` must agree, both
        on the text and on raising."""
        cmd = _py("import sys; sys.stdout.buffer.write(b'caf\\xe9\\n')")
        try:
            expected = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
        except Exception:
            expected = None
        if expected is None:
            with pytest.raises(UnicodeDecodeError):
                run_probe(cmd, launcher="device_probe", text=True, timeout=30)
            assert not _only_entry().is_open, "the end is recorded before the decode"
        else:
            assert run_probe(cmd, launcher="device_probe", text=True, timeout=30).stdout == expected

    def test_a_named_encoding_and_errors_are_honoured(self):
        cmd = _py("import sys; sys.stdout.buffer.write(b'caf\\xe9')")
        got = run_probe(cmd, launcher="device_probe", encoding="utf-8", errors="replace", timeout=30)
        assert got.stdout == "caf�"

    def test_a_missing_binary_raises_file_not_found_as_before_and_leaves_no_entry(self):
        missing = ["dpc-no-such-binary-for-this-test"]
        with pytest.raises(FileNotFoundError):
            subprocess.run(missing, capture_output=True, timeout=5)
        with pytest.raises(FileNotFoundError):
            run_probe(missing, launcher="device_probe", text=True, timeout=5)
        assert pr.get_registry().entries() == []

    def test_a_timeout_raises_timeout_expired_after_the_tree_is_killed(self):
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            run_probe(_py("import sys, time; print('before', flush=True); time.sleep(600)"),
                      launcher="device_probe", text=True, timeout=1)
        assert time.monotonic() - start < 20
        assert "before" in (caught.value.output or "")
        assert _only_entry().exit_reason == "timeout"

    def test_check_raises_called_process_error(self):
        with pytest.raises(subprocess.CalledProcessError) as caught:
            run_probe(_py("import sys; sys.exit(2)"), launcher="git_helper", timeout=30, check=True)
        assert caught.value.returncode == 2

    def test_a_launcher_that_is_not_a_probe_is_refused_before_any_spawn(self):
        with pytest.raises(ValueError):
            run_probe(_py("pass"), launcher="run_shell", timeout=5)
        assert pr.get_registry().entries() == []


class TestTheEntry:
    def test_a_probe_is_a_subsystem_child_of_the_backend(self, caplog):
        caplog.set_level(logging.INFO)
        run_probe(_py("print(1)"), launcher="device_probe", text=True, timeout=30)
        entry = _only_entry()
        assert (entry.launcher, entry.launcher_kind, entry.exit_reason, entry.exit_code) == (
            "device_probe", "subsystem", "exited", 0)
        assert "lost attribution" not in caplog.text

    def test_inside_a_tool_call_it_keeps_that_calls_origin(self):
        origin = SpawnOrigin(agent_id="agent_001", conversation_id="conv-1", tool="update_identity", round=4)
        with spawn_origin(origin):
            run_probe(_py("pass"), launcher="git_helper", timeout=30)
        entry = _only_entry()
        assert entry.launcher_kind == "subsystem"
        assert (entry.agent_id, entry.conversation_id, entry.tool, entry.round) == (
            "agent_001", "conv-1", "update_identity", 4)

    def test_on_windows_it_gets_a_hidden_console_of_its_own(self, monkeypatch):
        seen = {}
        real = subprocess.Popen

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(process_mod.subprocess, "Popen", spy)
        run_probe(_py("pass"), launcher="device_probe", timeout=30)
        assert seen["stdin"] is subprocess.DEVNULL
        if sys.platform == "win32":
            assert seen["creationflags"] == process_mod.CREATE_NO_WINDOW
        else:
            assert seen["start_new_session"] is True


class TestDuringTheStop:
    """ADR-046 point 7: what the backend's stop still lets be born."""

    def test_a_git_helper_is_refused_and_no_child_is_born(self, monkeypatch):
        pr.get_registry().begin_shutdown()
        born = []
        monkeypatch.setattr(process_mod.subprocess, "Popen", lambda *a, **k: born.append(a))
        with pytest.raises(ProcessRegistryClosed):
            run_probe(_py("pass"), launcher="git_helper", timeout=30)
        assert born == [] and pr.get_registry().entries() == []

    def test_a_device_probe_runs_and_is_registered(self):
        pr.get_registry().begin_shutdown()
        got = run_probe(_py("print(7)"), launcher="device_probe", text=True, timeout=30)
        assert got.stdout == "7\n"
        entry = _only_entry()
        assert (entry.launcher, entry.exit_reason) == ("device_probe", "exited")

    def test_a_tools_command_is_still_refused(self):
        pr.get_registry().begin_shutdown()
        with pytest.raises(ProcessRegistryClosed):
            process_mod.run_supervised(_py("pass"), launcher="run_shell", timeout=30)

    def test_a_subsystem_engine_is_still_refused(self):
        registry = pr.get_registry()
        registry.begin_shutdown()
        with pytest.raises(ProcessRegistryClosed):
            registry.check_open("strata", "subsystem")
        with pytest.raises(ProcessRegistryClosed):
            registry.check_open("device_probe", "tool")  # a tool named so is still a tool

    @pytest.mark.asyncio
    async def test_strata_reads_the_card_released_after_its_engine_stops(self, monkeypatch):
        """The probe the stop itself runs (`_verify_vram_released`, ADR-044 point 2):
        a refusal there would read as an unreadable card, `vram_released` None,
        after waiting out the whole release timeout."""
        from dpc_client_core.managers import llama_server_supervisor as base
        from dpc_client_core.managers.strata_supervisor import StrataSupervisor

        real = subprocess.Popen

        def nvidia_smi(cmd, *args, **kwargs):
            if cmd and cmd[0] == "nvidia-smi":
                cmd = _py("print(30000)")
            return real(cmd, *args, **kwargs)

        monkeypatch.setattr(base.shutil, "which", lambda name: name if name == "nvidia-smi" else WHICH(name))
        monkeypatch.setattr(process_mod.subprocess, "Popen", nvidia_smi)
        pr.get_registry().begin_shutdown()
        sup = types.SimpleNamespace(
            alias="flash", _vram_free_before=29000, vram_released=None, last_stop_vram=None,
            config={"vram_release_tolerance_mib": 512, "vram_release_timeout_s": 3},
        )
        released = await StrataSupervisor._verify_vram_released(sup)
        assert released is True
        assert sup.last_stop_vram == {"before_mib": 29000, "after_mib": 30000, "released": True}
        entry = _only_entry()
        assert (entry.launcher, entry.command) == ("device_probe", "nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits")


class TestTheMovedSites:
    """The sites read the same values through the helper; each registers its child."""

    def test_the_llama_server_free_and_total_queries(self, monkeypatch):
        from dpc_client_core.managers import llama_server_supervisor as base

        real = subprocess.Popen

        def nvidia_smi(cmd, *args, **kwargs):
            if cmd and cmd[0] == "nvidia-smi":
                cmd = _py("print(' 32607 ')" if "memory.total" in cmd[1] else "print('1234\\n5678')")
            return real(cmd, *args, **kwargs)

        monkeypatch.setattr(base.shutil, "which", lambda name: name)
        monkeypatch.setattr(process_mod.subprocess, "Popen", nvidia_smi)
        assert base._free_vram_mib() == 1234
        assert base._total_vram_mib() == 32607
        assert [e.launcher for e in pr.get_registry().entries()] == ["device_probe", "device_probe"]

    def test_a_card_query_that_times_out_still_answers_none(self, monkeypatch):
        from dpc_client_core.managers import llama_server_supervisor as base

        def times_out(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(base.shutil, "which", lambda name: name)
        monkeypatch.setattr(process_mod, "run_probe", times_out)
        assert base._free_vram_mib() is None and base._total_vram_mib() is None

    def test_the_collectors_dev_tools_probe(self, monkeypatch):
        from dpc_client_core import device_context_collector as collector_mod

        if shutil.which("git") is None:
            pytest.skip("git is not on PATH here")
        monkeypatch.setattr(collector_mod.shutil, "which", lambda name: WHICH(name) if name == "git" else None)
        tools = collector_mod.DeviceContextCollector()._detect_dev_tools()
        version = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30).stdout
        assert tools == {"git": re.search(r"(\d+\.\d+)", version).group(1)}
        (entry,) = pr.get_registry().entries()
        assert (entry.launcher, entry.command) == ("device_probe", "git --version")

    def test_the_collector_still_answers_installed_when_the_probe_fails(self, monkeypatch):
        from dpc_client_core import device_context_collector as collector_mod

        def fails(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(collector_mod.shutil, "which", lambda name: name if name == "git" else None)
        monkeypatch.setattr(process_mod, "run_probe", fails)
        assert collector_mod.DeviceContextCollector()._detect_dev_tools() == {"git": "installed"}


@pytest.fixture
def sandbox(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH here")
    root = tmp_path / "agent_001"
    root.mkdir()
    for cmd in (["git", "init", "-q"], ["git", "config", "user.name", "a"],
                ["git", "config", "user.email", "a@dpc-local"], ["git", "config", "commit.gpgsign", "false"]):
        subprocess.run(cmd, cwd=root, capture_output=True, timeout=30, check=True)
    return root


class TestTheGitHelpers:
    def test_an_auto_commit_is_two_git_helper_entries(self, sandbox, caplog):
        from dpc_client_core.dpc_agent.utils import auto_commit_agent_change

        (sandbox / "identity.md").write_text("me", encoding="utf-8")
        auto_commit_agent_change(sandbox, "identity: test")
        log = subprocess.run(["git", "log", "--format=%s"], cwd=sandbox, capture_output=True, text=True, timeout=30)
        assert log.stdout.strip() == "identity: test"
        entries = pr.get_registry().entries()
        assert [(e.launcher, e.command, e.exit_reason) for e in entries] == [
            ("git_helper", "git add -A", "exited"), ("git_helper", "git commit -m identity: test", "exited")]

    def test_nothing_to_commit_is_still_not_a_warning(self, sandbox, caplog):
        from dpc_client_core.dpc_agent.utils import auto_commit_agent_change

        (sandbox / "f").write_text("x", encoding="utf-8")
        auto_commit_agent_change(sandbox, "first")
        caplog.clear()
        auto_commit_agent_change(sandbox, "second")
        assert "Auto-commit failed" not in caplog.text

    def test_during_the_stop_an_auto_commit_is_refused_and_says_so(self, sandbox, caplog):
        from dpc_client_core.dpc_agent.utils import auto_commit_agent_change

        pr.get_registry().begin_shutdown()
        (sandbox / "f").write_text("x", encoding="utf-8")
        auto_commit_agent_change(sandbox, "late")
        assert "Auto-commit failed" in caplog.text and "stopping" in caplog.text
        assert pr.get_registry().entries() == []

    def test_get_git_info_reads_the_branch_and_the_sha(self, sandbox):
        from dpc_client_core.dpc_agent.utils import get_git_info, run_cmd

        (sandbox / "f").write_text("x", encoding="utf-8")
        run_cmd(["git", "add", "-A"], cwd=sandbox)
        run_cmd(["git", "commit", "-q", "-m", "c"], cwd=sandbox)
        branch, sha = get_git_info(sandbox)
        assert branch and len(sha) == 40
        assert sha == run_cmd(["git", "rev-parse", "HEAD"], cwd=sandbox)
        with pytest.raises(RuntimeError):
            run_cmd(["git", "no-such-subcommand"], cwd=sandbox)
