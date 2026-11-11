"""New Chat in an agent chat right after a restart archives the session.

The dialog promises "Current history will be archived". With no agent manager
yet (or a manager that found no monitor) the handler used to unlink
history.json directly, so the session was lost. Now a reset without a live
monitor reads the file through a throwaway monitor and resets it like every
other reset: archive first when the owner preserves history.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from dpc_client_core import service as service_module
from dpc_client_core.service import CoreService

CONV = "agent_001"


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _Firewall:
    def __init__(self, preserve, max_sessions=0):
        self.value = (preserve, max_sessions)

    def get_history_settings(self, profile_name=None):
        return self.value


def _msg(i):
    return {"id": f"m{i}", "role": "user", "content": f"hello {i}",
            "timestamp": f"2026-11-0{i}T10:00:00+00:00"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(service_module, "DPC_HOME_DIR", tmp_path / ".dpc")
    conv = tmp_path / ".dpc" / "conversations" / CONV
    conv.mkdir(parents=True)
    (conv / "history.json").write_text(
        json.dumps({"messages": [_msg(1), _msg(2)]}), encoding="utf-8")
    return conv


def _service(managers, preserve=True):
    provider = SimpleNamespace(_managers=managers)
    return SimpleNamespace(
        llm_manager=SimpleNamespace(providers={"dpc_agent": provider}),
        local_api=_Api(),
        firewall=_Firewall(preserve),
        settings=None,
    )


def _archives(conv):
    return sorted((conv / "archive").rglob("*_reset_session.json"))


def test_no_agent_manager_archives_before_clearing(home):
    svc = _service({})
    result = asyncio.run(CoreService.propose_new_session(svc, CONV))

    assert result["status"] == "success"
    assert not (home / "history.json").exists()
    archives = _archives(home)
    assert len(archives) == 1
    saved = json.loads(archives[0].read_text(encoding="utf-8"))
    assert [m["content"] for m in saved["messages"]] == ["hello 1", "hello 2"]
    assert svc.local_api.events == [("conversation_reset", {"conversation_id": CONV})]


def test_preserve_off_clears_without_archive(home):
    svc = _service({}, preserve=False)
    result = asyncio.run(CoreService.propose_new_session(svc, CONV))

    assert result["status"] == "success"
    assert not (home / "history.json").exists()
    assert _archives(home) == []


def test_manager_returning_false_never_deletes_without_archiving(home):
    manager = SimpleNamespace(reset_conversation=lambda cid: False)
    svc = _service({CONV: manager})
    result = asyncio.run(CoreService.propose_new_session(svc, CONV))

    assert result["status"] == "success"
    assert len(_archives(home)) == 1
    assert not (home / "history.json").exists()


def test_nothing_on_disk_is_still_a_success(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(service_module, "DPC_HOME_DIR", tmp_path / ".dpc")
    svc = _service({})
    result = asyncio.run(CoreService.propose_new_session(svc, CONV))
    assert result["status"] == "success"
