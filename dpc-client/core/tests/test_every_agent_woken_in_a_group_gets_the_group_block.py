"""Every agent woken in a group gets the same group block, whichever path woke it.

Only the local wake path (service._invoke_agent_in_group) built a chat_context;
a mention arriving from a peer (group_handler._invoke_agent) passed none, so that
turn had no group name, topic or roster, and external agents got only the text
of the mention (A-GROUP-TURN-WOKEN-BY-A-PEER-MESSAGE-CARRIES-NONE-OF-THE-GROUP-METADATA).
One builder, CoreService._group_chat_context, now feeds both embedded paths and
both cc_group_mention emitters; the bridge prints what it is given.
"""

import json
import types
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent.context import _build_runtime_section
from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.managers.group_manager import GroupMetadata
from dpc_client_core.message_handlers.group_handler import GroupTextHandler
from dpc_client_core.service import CoreService

from tests.test_a_bridge_answers_to_the_tag_it_registered import GROUP as BRIDGE_GROUP  # noqa: F401
from tests.test_a_bridge_answers_to_the_tag_it_registered import bridge  # noqa: F401
from tests.test_a_bridge_can_wait_to_be_mentioned import OK, _Closed, _frame, scripted  # noqa: F401

OWN = "dpc-node-" + "1" * 32
PEER = "dpc-node-" + "2" * 32
GID = "group-0123456789ab"
TOPIC = "Hardware notes. " * 200          # long on purpose: the topic is never cut


def _group(**over):
    fields = dict(
        group_id=GID, name="Hardware", topic=TOPIC,
        created_by=PEER, created_at="2026-10-20T06:04:45+00:00",
        members=[OWN, PEER],
        agents={OWN: ["agent_001", "ext:CC_mike"], PEER: ["agent_win10"]},
        agent_names={OWN: {"agent_001": "Ark", "ext:CC_mike": "CC_mike"},
                     PEER: {"agent_win10": "Win10"}},
        version=7, is_discord_bridge=False, reasoning_effort="high",
        session_started_at=None,
        session_reset_evidence={"votes": ["signed"]},
    )
    fields.update(over)
    return GroupMetadata(**fields)


def _service(group=None, monitor=None):
    group = group or _group()
    manager = types.SimpleNamespace(process_message=AsyncMock(return_value=""))
    provider = types.SimpleNamespace(
        _ensure_manager=AsyncMock(return_value=manager),
        get_manager=lambda aid: manager,
    )
    svc = CoreService.__new__(CoreService)
    svc.peer_metadata = {PEER: {"name": "Win10 box"}}
    svc.p2p_manager = types.SimpleNamespace(
        node_id=OWN, get_display_name=lambda: "Mike",
        peer_cache=types.SimpleNamespace(get_peer=lambda nid: None))
    svc.group_manager = types.SimpleNamespace(get_group=lambda gid: group if gid == GID else None)
    svc.conversation_monitors = {GID: monitor or types.SimpleNamespace(
        persist_history=True, live_history_boundary="2026-10-26T17:12:12+00:00")}
    svc.llm_manager = types.SimpleNamespace(providers={"dpc_agent": provider})
    svc.local_api = types.SimpleNamespace(broadcast_event=AsyncMock())
    svc._get_default_agent_id = lambda: "agent_001"
    svc._get_agent_display_name = lambda aid: {"agent_001": "Ark"}.get(aid, aid)
    svc.get_cc_display_name = lambda: "CC"
    svc.send_group_agent_message = AsyncMock()
    return svc, manager


# (a) the builder, for a fixture group


def test_the_block_carries_the_group_metadata():
    svc, _ = _service()
    ctx = svc._group_chat_context(GID)
    assert ctx == {
        "chat_type": "group",
        "chat_name": "Hardware",
        "chat_id": GID,
        "description": TOPIC,
        "participants": svc._group_participants(_group()),
        "created_by": {"name": "Win10 box", "node_id": PEER},
        "created_at": "2026-10-20T06:04:45+00:00",
        "reasoning_effort": "high",
        "is_discord_bridge": False,
        "persist_history": True,
        "live_history_boundary": "2026-10-26T17:12:12+00:00",
    }
    assert len(ctx["description"]) == len(TOPIC)


def test_internals_and_empty_values_stay_out_and_the_five_keys_stay_in():
    svc, _ = _service(group=_group(topic="", reasoning_effort=None, created_by="", created_at=""),
                      monitor=types.SimpleNamespace(persist_history=False, live_history_boundary=None))
    ctx = svc._group_chat_context(GID)
    for key in ("version", "session_reset_evidence", "members", "agents", "agent_names",
                "session_started_at", "reasoning_effort", "created_by", "created_at",
                "live_history_boundary"):
        assert key not in ctx, key
    assert ctx["description"] == ""
    assert ctx["persist_history"] is False
    assert set(CoreService._CHAT_CONTEXT_KEYS) <= set(ctx)


def test_a_group_created_here_names_this_node_and_a_reset_is_shown():
    svc, _ = _service(group=_group(created_by=OWN, session_started_at="2026-10-01T00:00:00+00:00"))
    ctx = svc._group_chat_context(GID)
    assert ctx["created_by"] == {"name": "Mike", "node_id": OWN}
    assert ctx["session_started_at"] == "2026-10-01T00:00:00+00:00"


def test_an_unknown_group_has_no_block():
    svc, _ = _service()
    assert svc._group_chat_context("group-ffffffffffff") is None


# (b) both embedded wake paths pass the same block


@pytest.mark.asyncio
async def test_the_local_and_the_peer_wake_pass_the_same_block():
    svc, manager = _service()
    expected = svc._group_chat_context(GID)

    await svc._invoke_agent_in_group(GID, "@Ark hi", "Mike", "agent_001")
    local = manager.process_message.await_args.kwargs["chat_context"]

    await GroupTextHandler(svc)._invoke_agent(GID, "@Ark hi", "Win10 box")
    peer = manager.process_message.await_args.kwargs["chat_context"]

    assert local == expected
    assert peer == expected, "the peer wake path lost the group block"


# (c) the mention event carries it, on both emitters


@pytest.mark.asyncio
async def test_the_mention_event_carries_the_block_on_the_send_path():
    svc, _ = _service()
    await svc._handle_group_agent_mentions(GID, "@CC_mike look", "Mike")
    event, payload = svc.local_api.broadcast_event.await_args.args
    assert event == "cc_group_mention" and payload["agent_tag"] == "cc_mike"
    assert payload["chat_context"] == svc._group_chat_context(GID)


@pytest.mark.asyncio
async def test_the_mention_event_carries_the_block_on_the_peer_path():
    svc, _ = _service()
    payload = {"message_id": "m1", "text": "@CC_mike look", "sender_name": "Win10 box"}
    await GroupTextHandler(svc)._handle_agent_mentions(GID, payload, payload["text"],
                                                       "Win10 box", PEER)
    event, sent = svc.local_api.broadcast_event.await_args.args
    assert event == "cc_group_mention" and sent["agent_tag"] == "cc_mike"
    assert sent["chat_context"] == svc._group_chat_context(GID)


@pytest.mark.asyncio
async def test_the_bridge_can_ask_for_the_block():
    assert "get_group_chat_context" in ALLOWED_COMMANDS
    svc, _ = _service()
    reply = await svc.get_group_chat_context(GID)
    assert reply == {"status": "success", "chat_context": svc._group_chat_context(GID)}
    assert (await svc.get_group_chat_context("group-ffffffffffff"))["status"] == "error"


# (d) the runtime section prints the added keys, the five as before


def test_the_runtime_section_prints_the_whole_block(tmp_path):
    svc, _ = _service()
    ctx = svc._group_chat_context(GID)
    text = _build_runtime_section(tmp_path, {"id": "t1", "type": "chat", "chat_context": ctx})
    task = json.loads(text[text.index("{"):text.rindex("}") + 1])["task"]
    assert task["id"] == "t1"
    for key, value in ctx.items():
        assert task[key] == value, key


# (e) the bridge shows it


def test_a_mention_line_is_followed_by_the_block(bridge, scripted, capsys):  # noqa: F811
    sessions, _, _ = scripted
    ctx = {"chat_type": "group", "chat_name": "1234", "description": "topic"}
    sessions.append([OK, json.dumps(_frame(chat_context=ctx)), _Closed()])
    assert bridge.listen_sync(BRIDGE_GROUP, ["CC_mike"], once=True) == 0
    lines = capsys.readouterr().out.splitlines()
    i = next(n for n, line in enumerate(lines) if line.startswith("[MENTION]"))
    assert lines[i + 1] == "[CHAT_CONTEXT] " + json.dumps(ctx, ensure_ascii=False)


def test_list_groups_can_carry_each_groups_block(bridge, monkeypatch):  # noqa: F811
    ctx = {"chat_type": "group", "chat_name": "1234"}
    asked = []

    def fetch(ids):
        asked.extend(ids)
        return {BRIDGE_GROUP: ctx}

    monkeypatch.setattr(bridge, "fetch_chat_contexts", fetch)
    assert "chat_context" not in bridge.list_groups()[0]
    assert asked == []
    groups = bridge.list_groups(with_chat_context=True)
    assert asked == [BRIDGE_GROUP], "the backend must be asked by the canonical id"
    assert groups[0]["chat_context"] == ctx


def test_the_read_prints_the_block_before_the_history(bridge, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(bridge, "fetch_chat_contexts",
                        lambda ids: {BRIDGE_GROUP: {"chat_name": "1234"}})
    bridge.main(["--group", BRIDGE_GROUP, "--chat-context"])
    out = capsys.readouterr().out.splitlines()
    assert out[0] == '[CHAT_CONTEXT] {"chat_name": "1234"}'
    assert out[1].startswith("[CC Group Bridge]")


def test_an_unreachable_backend_is_said_and_gives_no_block(bridge, scripted, capsys):  # noqa: F811
    sessions, _, _ = scripted
    sessions.append(ConnectionRefusedError("refused"))
    assert bridge.fetch_chat_contexts([BRIDGE_GROUP]) == {}
    assert "backend not reached" in capsys.readouterr().err
