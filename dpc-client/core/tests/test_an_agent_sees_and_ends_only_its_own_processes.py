"""ADR-046 point 8: an agent sees, and may kill, only its own processes.

What is asserted: `list_my_processes` lists only the entries whose spawn origin
names the agent, running first, with its ceiling beside its use;
`kill_my_process` refuses a pid that is the backend's, unknown, finished,
another agent's or a subsystem's, and signals nothing then; it kills its own
with `cancelled_by_agent`, through the registry's path, whether it holds the
`Popen` itself or a `run_supervised` thread is waiting on it. Both tools are
registered with `default_enabled` given, and the firewall seeds them from it.

Real children where the point is a kill; fake pids where nothing may be
signalled. Nothing here touches `~/.dpc`; the registry is the in-memory default.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from dpc_client_core import process_registry as pr
from dpc_client_core.dpc_agent.tools import process as process_mod
from dpc_client_core.dpc_agent.tools import skills
from dpc_client_core.dpc_agent.tools.process import run_supervised
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.process_registry import SpawnOrigin, spawn_origin

SLEEP = [sys.executable, "-c", "import time; time.sleep(600)"]


def _ctx(tmp_path, agent="agent_a"):
    root = tmp_path / agent
    root.mkdir(exist_ok=True)
    return ToolContext(agent_root=root)


def _origin(agent):
    return SpawnOrigin(agent_id=agent, conversation_id=f"chat-of-{agent}", tool="run_shell", round=1)


def _wait(predicate, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def registry():
    return pr.get_registry()


@pytest.fixture
def started():
    """Processes a test starts by hand, killed at the end whatever happens."""
    procs = []
    yield procs
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait()


def _child(started, registry, *, agent="agent_a", launcher="run_shell", kind="tool"):
    child = subprocess.Popen(SLEEP)
    started.append(child)
    registry.register(child.pid, launcher=launcher, launcher_kind=kind, command=SLEEP, handle=child,
                      origin=_origin(agent))
    return child


@pytest.fixture
def no_kill(monkeypatch):
    """Any kill the registry would send fails the test: a refusal signals nothing."""
    def refuse(target):
        raise AssertionError(f"a kill was sent to {target!r}")

    monkeypatch.setattr(pr, "_kill_tree_of", refuse)


class TestListMyProcesses:
    def test_it_shows_only_this_agents_entries_running_first(self, tmp_path, registry, monkeypatch):
        monkeypatch.setattr(process_mod, "_MEMORY_CEILING_MB", 4096)
        registry.register(7_970_001, launcher="run_shell", launcher_kind="tool", command="python mine_done.py",
                          origin=_origin("agent_a"))
        registry.end(7_970_001, 0)
        registry.register(7_970_002, launcher="run_shell", launcher_kind="tool", command="python mine_running.py",
                          origin=_origin("agent_a"))
        registry.register(7_970_003, launcher="run_shell", launcher_kind="tool", command="python theirs.py",
                          origin=_origin("agent_b"))
        registry.register(7_970_004, launcher="device_probe", launcher_kind="subsystem", command="nvidia-smi")
        text = skills._list_my_processes(_ctx(tmp_path))
        assert "mine_running.py" in text and "mine_done.py" in text
        assert "theirs.py" not in text and "7970003" not in text, "another agent's process was shown"
        assert "nvidia-smi" not in text, "a subsystem nobody's call woke was shown"
        assert text.index("7970002") < text.index("7970001"), "running first"
        assert "1 running, 1 ended" in text
        assert "4096 MB" in text and "shell_memory_limit_mb" in text, "the ceiling beside the use"
        assert "chat-of-agent_a" in text and "ended exited (exit_code 0)" in text

    def test_with_nothing_of_its_own_it_says_so(self, tmp_path, registry):
        registry.register(7_970_011, launcher="run_shell", launcher_kind="tool", command="x", origin=_origin("agent_b"))
        text = skills._list_my_processes(_ctx(tmp_path))
        assert "0 running, 0 ended" in text and "No process" in text and "7970011" not in text

    def test_a_subsystem_its_call_woke_is_shown_as_the_backends(self, tmp_path, registry):
        registry.register(7_970_021, launcher="strata", launcher_kind="subsystem", command="launcher",
                          origin=_origin("agent_a"))
        text = skills._list_my_processes(_ctx(tmp_path))
        assert "7970021" in text and "owned by the backend" in text


class TestKillMyProcessRefuses:
    def test_another_agents_process(self, tmp_path, registry, started, no_kill):
        child = _child(started, registry, agent="agent_b")
        text = skills._kill_my_process(_ctx(tmp_path, "agent_a"), child.pid)
        assert text.startswith("⚠️ Refused") and "not started by this agent" in text
        assert child.poll() is None, "another agent's process was killed"
        assert registry.open_entries()[0].pid == child.pid

    def test_a_subsystem_even_when_its_own_call_woke_it(self, tmp_path, registry, started, no_kill):
        child = _child(started, registry, agent="agent_a", launcher="strata", kind="subsystem")
        text = skills._kill_my_process(_ctx(tmp_path, "agent_a"), child.pid)
        assert "Refused" in text and "strata subsystem" in text and "owner is the backend" in text
        assert child.poll() is None

    def test_a_pid_the_registry_does_not_hold(self, tmp_path, registry, no_kill):
        text = skills._kill_my_process(_ctx(tmp_path), 7_971_001)
        assert "Refused" in text and "not a running process in the process registry" in text

    def test_a_pid_with_no_spawn_origin(self, tmp_path, registry, no_kill):
        registry.register(7_971_011, launcher="run_shell", launcher_kind="tool", command="x")
        text = skills._kill_my_process(_ctx(tmp_path), 7_971_011)
        assert "Refused" in text and "not started by this agent" in text

    @pytest.mark.parametrize("which", ["own", "parent"])
    def test_the_backend_and_its_ancestors(self, tmp_path, registry, no_kill, which):
        pid = os.getpid() if which == "own" else os.getppid()
        text = skills._kill_my_process(_ctx(tmp_path), pid)
        assert "Refused" in text and "backend's own process" in text

    def test_its_own_process_that_already_ended(self, tmp_path, registry, no_kill):
        registry.register(7_971_021, launcher="run_shell", launcher_kind="tool", command="x", origin=_origin("agent_a"))
        registry.end(7_971_021, 0)
        text = skills._kill_my_process(_ctx(tmp_path), 7_971_021)
        assert "Refused" in text and "already ended (exited)" in text

    @pytest.mark.parametrize("pid", ["the big one", 12.5, None, True])
    def test_a_pid_that_is_not_a_number(self, tmp_path, registry, no_kill, pid):
        assert "is not a pid" in skills._kill_my_process(_ctx(tmp_path), pid)

    def test_a_pid_that_changed_hands(self, tmp_path, registry, no_kill):
        registry.register(7_971_031, launcher="run_shell", launcher_kind="tool", command="x", origin=_origin("agent_a"))
        entry, said = registry.cancel_by_agent(7_971_031, "agent_a", probe=lambda pid, start: "other")
        assert entry is None and "no longer belongs" in said


class TestKillMyProcessKillsItsOwn:
    def test_it_ends_cancelled_by_agent_with_its_code(self, tmp_path, registry, started):
        child = _child(started, registry, agent="agent_a")
        text = skills._kill_my_process(_ctx(tmp_path, "agent_a"), str(child.pid))
        assert child.wait(timeout=20) is not None, "its own process was left running"
        (entry,) = registry.entries()
        assert entry.exit_reason == "cancelled_by_agent" and entry.ended_at
        assert entry.exit_code is not None, "the code its Popen gave is recorded"
        assert f"pid {child.pid}" in text and "ended cancelled_by_agent" in text

    def test_a_run_supervised_thread_waiting_on_it_reads_the_same_word(self, tmp_path, registry):
        runs = []

        def run():
            with spawn_origin(_origin("agent_a")):
                runs.append(run_supervised(SLEEP, launcher="run_shell", timeout=120))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert _wait(lambda: registry.open_entries()), "the command never registered"
        (entry,) = registry.open_entries()
        text = skills._kill_my_process(_ctx(tmp_path, "agent_a"), entry.pid)
        thread.join(timeout=30)
        assert not thread.is_alive(), "run_supervised never returned after the kill"
        assert runs[0].exit_reason == "cancelled_by_agent" and not runs[0].timed_out
        assert registry.entries()[0].exit_reason == "cancelled_by_agent"
        assert "ended cancelled_by_agent" in text

    def test_announced_after_the_backends_stop_began_it_reads_backend_stop(self, tmp_path, registry, started):
        child = _child(started, registry, agent="agent_a")
        registry.begin_shutdown()
        entry, _said = registry.cancel_by_agent(child.pid, "agent_a")
        assert child.wait(timeout=20) is not None
        assert entry.exit_reason == "backend_stop", "the precedence rule of point 4"


class TestRegistration:
    def _entries(self):
        return {e.name: e for e in skills.get_tools()}

    def test_the_list_is_on_by_default_and_the_kill_is_opt_in(self):
        entries = self._entries()
        assert entries["list_my_processes"].default_enabled is True
        assert entries["kill_my_process"].default_enabled is False
        assert entries["kill_my_process"].schema["parameters"]["required"] == ["pid"]

    def test_the_firewall_seeds_both_from_their_default(self, tmp_path, monkeypatch):
        from dpc_client_core.dpc_agent.tools.registry import ToolRegistry
        from dpc_client_core.firewall import ContextFirewall

        monkeypatch.setenv("DPC_HOME", str(tmp_path))
        tools = ToolRegistry(agent_root=tmp_path / "agent_a")
        assert {"list_my_processes", "kill_my_process"} <= set(tools._entries)
        defaults = {e.name: e.default_enabled for e in tools._entries.values()}
        rules = SimpleNamespace(rules={"dpc_agent": {"tools": {}}, "agent_profiles": {"p": {"tools": {}}}})
        assert ContextFirewall._seed_missing_tools_into_rules(rules, defaults) is True
        for block in (rules.rules["dpc_agent"]["tools"], rules.rules["agent_profiles"]["p"]["tools"]):
            assert (block["list_my_processes"], block["kill_my_process"]) == (True, False)
