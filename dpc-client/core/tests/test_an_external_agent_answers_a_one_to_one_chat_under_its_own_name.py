"""An external agent answers a 1:1 agent chat under its own name.

The 1:1 bridge (cc_agent_bridge.py) used to send {conversation_id, text} only, and
the backend stamped every harness with the node's single cc_display_name; the
duplicate guard keyed on the literal sender id 'cc', so a second harness's
identical short answer was dropped (AN-EXTERNAL-AGENT-HAS-NO-IDENTITY-IN-A-ONE-TO-ONE-CHAT).
Now `--as TAG` sends `sender_name`, the record carries it, and the guard drops
only the same sender's repeat. The sender id stays 'cc', the 1:1 mark of an
external agent's record that the chain and Telegram paths read.

The bridge is loaded by file path against a fake DPC_HOME; the service is a bare
CoreService with doubles, as in test_a_cc_agent_chain_stops_after_five_rounds.py.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.service import CoreService

BRIDGE = Path(__file__).resolve().parents[1] / "cc_agent_bridge.py"


# --- the bridge -------------------------------------------------------------

@pytest.fixture
def bridge(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("cc_agent_bridge_as_under_test", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / "dpc"
    home.mkdir()
    monkeypatch.setattr(mod, "DPC_HOME", home)
    monkeypatch.setattr(mod, "CONFIG_PATH", home / "config.ini")
    return mod


def test_as_puts_the_name_on_the_send(bridge):
    cmd = bridge._build_send_command("agent_001", "ok", "Zcode")
    assert cmd["command"] == "send_cc_agent_response"
    assert cmd["payload"] == {"conversation_id": "agent_001", "text": "ok", "sender_name": "Zcode"}


def test_without_as_the_send_is_what_it_was(bridge):
    cmd = bridge._build_send_command("agent_001", "ok")
    assert cmd["payload"] == {"conversation_id": "agent_001", "text": "ok"}


def test_the_cli_takes_as_like_the_group_bridge(bridge):
    args = bridge._build_parser().parse_args(["--as", "Zcode", "--send", "hi"])
    assert args.as_name == "Zcode"
    assert bridge._build_parser().parse_args(["--send", "hi"]).as_name is None


def test_as_is_also_the_name_scanned_for(bridge):
    msgs = [{"sender_name": "Mike", "content": t} for t in ("@Zcode look", "@CC look")]
    assert [i for i, _ in bridge.find_mentions(msgs, name=bridge._identity_name("Zcode"))] == [0]
    assert [i for i, _ in bridge.find_mentions(msgs, name=bridge._identity_name(None))] == [1]


# --- the backend ------------------------------------------------------------

class _Monitor:
    def __init__(self):
        self.message_history = []

    def add_message(self, role, content, timestamp=None, **kwargs):
        self.message_history.append({"role": role, "content": content, **kwargs})

    def save_history(self):
        pass

    def get_last_msg_index(self):
        return len(self.message_history) - 1


class _Manager:
    agent_id = "agent_001"

    def __init__(self):
        self._agent_monitors = {"agent_001": _Monitor()}
        self.process_message = AsyncMock(return_value="")

    def _get_or_create_agent_monitor(self, conversation_id):
        return self._agent_monitors.setdefault(conversation_id, _Monitor())

    def get_session_state(self, conversation_id):
        return {}


def _service():
    service = CoreService.__new__(CoreService)
    manager = _Manager()
    provider = SimpleNamespace(get_manager=lambda cid: manager)
    service.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    service.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    service.telegram_manager = None
    service._cc_ark_chain_depths = {}
    service.get_cc_display_name = lambda: "CC"
    service._get_agent_display_name = lambda agent_id=None: "Ark"
    service.manager = manager
    return service


def _history(service):
    return service.manager._agent_monitors["agent_001"].message_history


@pytest.mark.asyncio
async def test_the_name_sent_lands_in_the_record():
    service = _service()
    result = await service.send_cc_agent_response("agent_001", "ok", sender_name="Zcode")
    assert result["status"] == "success"
    (rec,) = _history(service)
    assert rec["sender_name"] == "Zcode"
    assert rec["sender_node_id"] == "cc"
    event = service.local_api.broadcast_event.await_args.args[1]
    assert event["sender_name"] == "Zcode"


@pytest.mark.asyncio
async def test_no_name_sent_is_the_configured_name_as_before():
    service = _service()
    await service.send_cc_agent_response("agent_001", "ok")
    await service.send_cc_agent_response("agent_001", "next", sender_name="  ")
    assert [r["sender_name"] for r in _history(service)] == ["CC", "CC"]


@pytest.mark.asyncio
async def test_two_harnesses_with_the_same_answer_both_land():
    service = _service()
    await service.send_cc_agent_response("agent_001", "+1", sender_name="CC_mike")
    await service.send_cc_agent_response("agent_001", "+1", sender_name="Zcode")
    assert [r["sender_name"] for r in _history(service)] == ["CC_mike", "Zcode"]


@pytest.mark.asyncio
async def test_the_same_harness_sending_twice_is_still_dropped():
    service = _service()
    await service.send_cc_agent_response("agent_001", "+1", sender_name="Zcode")
    result = await service.send_cc_agent_response("agent_001", "+1", sender_name="Zcode")
    assert result.get("deduplicated") is True
    await service.send_cc_agent_response("agent_001", "+1")
    second = await service.send_cc_agent_response("agent_001", "+1")
    assert second.get("deduplicated") is True
    assert [r["sender_name"] for r in _history(service)] == ["Zcode", "CC"]


@pytest.mark.asyncio
async def test_a_turn_woken_under_a_tag_is_still_an_external_agent_turn():
    """ADR-048: a 1:1 bridge turn is message_source external_agent, under --as too."""
    service = _service()
    await service.send_cc_agent_response("agent_001", "@Ark look", sender_name="Zcode")
    await service._invoke_agent_in_agent_chat("agent_001", "@Ark look", service.manager)
    kwargs = service.manager.process_message.await_args.kwargs
    assert kwargs["message_source"] == "external_agent"
    assert kwargs["sender_name"] == "Zcode"
    assert _history(service)[-1]["message_source"] == "external_agent"
