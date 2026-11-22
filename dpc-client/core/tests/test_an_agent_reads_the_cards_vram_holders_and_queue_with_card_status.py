"""The agent tool `card_status` (ADR-046 point 8, ADR-052): one read-only call answers what the
Agent map shows the owner - the card's free and total VRAM, who holds the card, who waits for it
and which processes the driver lists on it.

What is asserted: the shape of the answer; every NVIDIA-dependent value is None (and the note says
why) without nvidia-smi or when the driver gives no per-process figure (WDDM `[N/A]`); no
`conversation_id` leaves the owner's queue rows; the compute-apps read is one registered
`device_probe` per 30 s window, like `read_vram_mib`; the tool is registered on by default, in the
same tier as `list_my_processes`.

Nothing here runs nvidia-smi or touches `~/.dpc`: the probes are replaced, the supervisors' and the
lease's own readers run on empty or faked state, and both caches are reset around every test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import commit_memory as cm
from dpc_client_core.managers import llama_server_supervisor as lss

NVIDIA_SMI_APPS = (
    "1234, C:\\Program Files\\llama\\llama-server.exe, 7168 MiB\n"
    "5678, C:\\Windows\\explorer.exe, 300 MiB\n"
)
WDDM_APPS = (
    "1234, C:\\Program Files\\llama\\llama-server.exe, [N/A]\n"
    "5678, C:\\Windows\\explorer.exe, [N/A]\n"
)


def _reset():
    cm._vram_reading.update(at=None, free=None, total=None)
    cm._gpu_apps_reading.update(at=None, apps=None)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    _reset()
    monkeypatch.setattr(lss, "_LEASE_HOLDERS", lambda: [])
    monkeypatch.setattr(lss, "_LEASE_QUEUE_ROWS", lambda: [])
    monkeypatch.setattr(lss, "_live_claims", lambda: [])
    monkeypatch.setattr(lss, "_CARD_QUEUE", [])
    yield
    _reset()


class _Card:
    """nvidia-smi as a stub: the two VRAM figures and the compute-apps text, with call counts."""

    def __init__(self, free=9000, total=32607, apps=NVIDIA_SMI_APPS):
        self.free, self.total, self.apps = free, total, apps
        self.app_calls = 0

    def install(self, monkeypatch, present=True):
        monkeypatch.setattr(lss, "_free_vram_mib", lambda while_stopping=False: self.free)
        monkeypatch.setattr(lss, "_total_vram_mib", lambda: self.total)
        monkeypatch.setattr(cm.shutil, "which", lambda name: "nvidia-smi" if present else None)

        def probe(cmd, **kwargs):
            assert "--query-compute-apps=pid,process_name,used_memory" in cmd, cmd
            assert "--format=csv,noheader" in cmd, cmd
            self.app_calls += 1
            return SimpleNamespace(stdout=self.apps, returncode=0)

        monkeypatch.setattr(lss, "_device_probe", probe)
        return self


def _status():
    from dpc_client_core.dpc_agent.tools import card

    return json.loads(card._card_status(SimpleNamespace()))


class _Lease:
    IS_CARD_LEASE = True
    alias = "whisper"

    def card_holder_row(self, state):
        return {"alias": "whisper", "engine": "faster-whisper", "state": state, "port": None, "kind": "transcribe",
                "display": "Whisper", "busy": True, "uses": [{"what": "transcribe", "since": 1.0}],
                "vram_mib": 4447, "since": 1.0, "yield_cost": "none", "eta_s": 42.0, "beside": None}


def test_the_answer_carries_vram_holders_queue_and_the_gpu_processes(monkeypatch):
    _Card().install(monkeypatch)
    sup = SimpleNamespace(alias="gemma", ENGINE="llama.cpp", port=8081)
    monkeypatch.setattr(lss, "_live_claims", lambda: [(sup, "running")])
    monkeypatch.setattr(lss, "_LEASE_HOLDERS", lambda: [(_Lease(), "granted")])
    monkeypatch.setattr(lss, "_LEASE_QUEUE_ROWS", lambda: [
        {"alias": "music", "kind": "music", "what": "render", "queued_at": 5.0, "give_up_at": None,
         "until_cancelled": True, "wait": {"eta_s": 90}}])
    out = _status()
    assert (out["vram_free_mib"], out["vram_total_mib"]) == (9000, 32607)
    by_alias = {row["alias"]: row for row in out["holders"]}
    assert by_alias["gemma"]["kind"] == "inference" and by_alias["gemma"]["state"] == "running"
    assert by_alias["whisper"]["busy"] is True and by_alias["whisper"]["eta_s"] == 42.0
    assert by_alias["whisper"]["vram_mib"] == 4447
    assert [row["alias"] for row in out["queue"]] == ["music"], "the lease line's rows follow the card queue's"
    assert out["queue"][0]["position"] == 1
    assert out["gpu_processes"] == [
        {"pid": 1234, "process_name": "C:\\Program Files\\llama\\llama-server.exe", "used_memory_mib": 7168},
        {"pid": 5678, "process_name": "C:\\Windows\\explorer.exe", "used_memory_mib": 300},
    ]
    assert out["gpu_processes_note"] is None


def test_every_nvidia_value_is_none_without_nvidia_smi_and_the_rest_still_answers(monkeypatch):
    _Card(free=None, total=None).install(monkeypatch, present=False)
    sup = SimpleNamespace(alias="gemma", ENGINE="llama.cpp", port=8081)
    monkeypatch.setattr(lss, "_live_claims", lambda: [(sup, "running")])
    out = _status()
    assert out["vram_free_mib"] is None and out["vram_total_mib"] is None
    assert out["gpu_processes"] is None
    assert "nvidia" in out["gpu_processes_note"].lower()
    assert [row["alias"] for row in out["holders"]] == ["gemma"]
    assert out["queue"] == []


def test_a_driver_with_no_per_process_figure_gives_none_and_the_note_names_wddm(monkeypatch):
    card = _Card(apps=WDDM_APPS).install(monkeypatch)
    out = _status()
    assert [(p["pid"], p["used_memory_mib"]) for p in out["gpu_processes"]] == [(1234, None), (5678, None)]
    assert "WDDM" in out["gpu_processes_note"] and "N/A" in out["gpu_processes_note"]
    assert card.app_calls == 1


def test_no_compute_process_is_an_empty_list_and_not_none(monkeypatch):
    _Card(apps="").install(monkeypatch)
    out = _status()
    assert out["gpu_processes"] == []
    assert out["gpu_processes_note"] is None


def test_a_failing_probe_is_none_never_a_crash(monkeypatch):
    _Card().install(monkeypatch)

    def refused(cmd, **kwargs):
        raise RuntimeError("the registry is closed")

    monkeypatch.setattr(lss, "_device_probe", refused)
    out = _status()
    assert out["gpu_processes"] is None and out["gpu_processes_note"]
    assert out["vram_free_mib"] == 9000


def test_the_owners_conversation_ids_never_leave_the_queue_rows(monkeypatch):
    _Card().install(monkeypatch)
    place = SimpleNamespace(
        alias="gemma",
        requests=[SimpleNamespace(
            hold=SimpleNamespace(agent_id="agent_b", conversation_id="group-secret-chat"),
            queued_at=7.0, give_up_at=float("inf"))])
    monkeypatch.setattr(lss, "_CARD_QUEUE", [place])
    from dpc_client_core.dpc_agent.tools import card

    text = card._card_status(SimpleNamespace())
    assert "conversation_id" not in text and "group-secret-chat" not in text
    (row,) = json.loads(text)["queue"]
    assert row["agent_id"] == "agent_b" and row["alias"] == "gemma" and row["position"] == 1


def test_a_second_call_inside_the_window_runs_no_second_probe(monkeypatch):
    card = _Card().install(monkeypatch)
    assert cm.read_gpu_compute_apps(now=100.0)[0]["pid"] == 1234
    card.apps = ""
    assert cm.read_gpu_compute_apps(now=129.9)[0]["pid"] == 1234, "the window's reading is reused"
    assert card.app_calls == 1
    assert cm.read_gpu_compute_apps(now=130.0) == [], "30 s later the card is asked again"
    assert card.app_calls == 2


def test_the_tool_reuses_the_cached_reading_across_calls(monkeypatch):
    card = _Card().install(monkeypatch)
    _status()
    _status()
    assert card.app_calls == 1


def test_a_refused_probe_is_not_cached(monkeypatch):
    card = _Card().install(monkeypatch)
    good = lss._device_probe

    def refused(cmd, **kwargs):
        raise RuntimeError("the registry is closed")

    monkeypatch.setattr(lss, "_device_probe", refused)
    assert cm.read_gpu_compute_apps(now=0.0) is None
    monkeypatch.setattr(lss, "_device_probe", good)
    assert len(cm.read_gpu_compute_apps(now=1.0)) == 2, "the failure left no reading behind"
    assert card.app_calls == 1


class TestRegistration:
    def test_it_is_on_by_default_in_the_tier_of_list_my_processes(self):
        from dpc_client_core.dpc_agent.tools import card, skills

        card_entry = {e.name: e for e in card.get_tools()}["card_status"]
        list_entry = {e.name: e for e in skills.get_tools()}["list_my_processes"]
        assert (card_entry.default_enabled, card_entry.is_core) == (list_entry.default_enabled, list_entry.is_core)
        assert card_entry.default_enabled is True
        assert card_entry.schema["parameters"] == {"type": "object", "properties": {}, "required": []}

    def test_the_registry_finds_it_and_the_firewall_seeds_it_from_its_default(self, tmp_path, monkeypatch):
        from dpc_client_core.dpc_agent.tools.registry import ToolRegistry
        from dpc_client_core.firewall import ContextFirewall

        monkeypatch.setenv("DPC_HOME", str(tmp_path))
        tools = ToolRegistry(agent_root=tmp_path / "agent_a")
        assert "card_status" in tools._entries
        defaults = {e.name: e.default_enabled for e in tools._entries.values()}
        rules = SimpleNamespace(rules={"dpc_agent": {"tools": {}}, "agent_profiles": {}})
        assert ContextFirewall._seed_missing_tools_into_rules(rules, defaults) is True
        assert rules.rules["dpc_agent"]["tools"]["card_status"] is True

    def test_a_discord_turn_does_not_get_it(self):
        from dpc_client_core.dpc_agent.agent import DpcAgent

        assert "card_status" not in DpcAgent.EXTERNAL_SOURCE_ALLOWED_TOOLS
