"""A turn from Telegram keeps its source and its chat (Mike's call, 2026-11-08, review by
Zcode, Johnny, Ark; step 1 of ideas/layer-audience-design-2026-11-08.md).

Four parts, one change:

1. Every user record an agent turn writes, and the assistant record that answers it,
   carry `message_source`; a Telegram user record also carries `telegram_chat_id`, the
   chat it came from. Before, the bridge wrote `source="telegram"` for the owner and a
   stranger alike, so the history could not show which turns had the owner's memory.
2. The `@CC` door: the turn CC carries took `external_agent`, an owner turn, whoever
   woke CC. Now it takes the source of the records since CC's previous message: one
   not the owner's (an unmarked `source="telegram"` record counts as not) makes the turn
   that source.
3. CC's reply and a CC chain's answer went to `allowed_chat_ids[0]`; now they go to the
   chat recorded on the waking message, and with none recorded nothing is sent.
4. In unified mode only the owner's own turn joins the agent's 1:1 conversation with
   him; any other Telegram turn runs in the chat's own `telegram-<chat_id>`.

Nothing here touches the real ~/.dpc: monitors write under tmp_path and sign with the
test key.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.dpc_agent.agent import TELEGRAM_OWNER_MESSAGE_SOURCE
from dpc_client_core.managers import agent_manager as am
from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge
from dpc_client_core.service import CoreService

OWNER_ID = 111111111        # the owner's Telegram user id; his private chat has the same id
OTHER_ID = 222222222        # somebody else, in a private chat with the bot
GROUP_CHAT = -1009999999999  # an allowed group the owner is in
AGENT = "agent_tg_source_0001"
NODE = "dpc-node-" + "b" * 32
OWNER = TELEGRAM_OWNER_MESSAGE_SOURCE


@pytest.fixture(autouse=True)
def _signed_and_persisted(signing_identity, monkeypatch):
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))


def _monitor(root, conversation_id):
    m = ConversationMonitor(
        conversation_id=conversation_id,
        participants=[{"node_id": NODE, "name": "User", "context": "local"},
                      {"node_id": AGENT, "name": "Ark", "context": "agent"}],
        llm_manager=None,
    )
    m._get_history_path = lambda: root / conversation_id / "history.json"
    return m


def _on_disk(root, conversation_id):
    path = root / conversation_id / "history.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["messages"]


# -- 1. the records, written by the agent manager ---------------------------------------


class _FakeAgent:
    def __init__(self, text="an answer"):
        self.text = text
        self._last_usage, self._last_trace = {}, {}

    async def process(self, **kwargs):
        self._last_usage, self._last_trace = {}, {}
        return self.text


def _manager(root, conversation_id, agent=None):
    monitor = _monitor(root, conversation_id)
    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager.agent_id = AGENT
    manager.firewall = MagicMock()
    manager.config = {}
    manager._agent = agent or _FakeAgent()
    manager._agents = {}
    manager._agent_monitors = {conversation_id: monitor}
    manager._memory_indexes_initialized = True
    manager._agent_display_name = "Ark"
    manager._interrupt_events = {}
    manager._kill_events = {}
    manager._daily_tokens_used = 0
    manager._daily_tokens_date = ""
    manager.service = SimpleNamespace(
        p2p_manager=SimpleNamespace(node_id=NODE),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
    )
    manager.ensure_started = AsyncMock(return_value=manager)
    manager._get_or_create_agent_monitor = lambda conv: monitor
    manager.get_session_state = lambda conv: {}
    manager._resolve_reasoning_effort = lambda conv, effort: (None, "none")
    manager._resolve_context_window = lambda: None
    return manager


@pytest.mark.asyncio
async def test_a_ui_turn_records_ui_on_its_prompt_and_on_its_answer(tmp_path):
    manager = _manager(tmp_path, AGENT)
    await manager._process_message_guarded("hello", AGENT, message_source="ui")
    user, answer = _on_disk(tmp_path, AGENT)
    assert user["role"] == "user" and user["message_source"] == "ui"
    assert answer["role"] == "assistant" and answer["message_source"] == "ui"
    assert "telegram_chat_id" not in user


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["telegram", OWNER])
async def test_a_telegram_turn_records_its_source_and_its_chat(tmp_path, source):
    conv = f"telegram-{OTHER_ID}"
    manager = _manager(tmp_path, conv)
    await manager._process_message_guarded("hi", conv, message_source=source,
                                           telegram_chat_id=str(OTHER_ID))
    user, answer = _on_disk(tmp_path, conv)
    assert user["message_source"] == source and user["telegram_chat_id"] == str(OTHER_ID)
    assert answer["message_source"] == source


@pytest.mark.asyncio
async def test_a_turn_a_guard_ends_says_its_source_on_the_notice(tmp_path):
    manager = _manager(tmp_path, AGENT)
    manager.config = {"quota_tokens_per_day": 1}
    manager._daily_tokens_used = 5
    from datetime import datetime, timezone
    manager._daily_tokens_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manager._complete_turn = AsyncMock()
    await manager._process_message_guarded("hi", AGENT, message_source="telegram")
    user, notice = _on_disk(tmp_path, AGENT)
    assert notice["turn_end"] == "guard"
    assert user["message_source"] == notice["message_source"] == "telegram"


def test_a_turn_stopped_while_waiting_keeps_its_source_and_chat(tmp_path):
    manager = _manager(tmp_path, AGENT)
    manager._close_turn_stopped_while_waiting(
        message="wait", conversation_id=AGENT, message_source="telegram",
        telegram_chat_id=str(OTHER_ID))
    user, notice = _on_disk(tmp_path, AGENT)
    assert notice["turn_end"] == "stopped"
    assert user["message_source"] == notice["message_source"] == "telegram"
    assert user["telegram_chat_id"] == str(OTHER_ID)


@pytest.mark.asyncio
async def test_a_scheduled_run_records_the_source_of_the_turn_that_scheduled_it(tmp_path):
    manager = _manager(tmp_path, AGENT)
    manager._emit_progress = lambda *a, **k: None
    manager._emit_progress_clear = lambda *a, **k: None
    manager._broadcast_history_to_ui = AsyncMock()
    run = {"prompt": "look again", "task_type": "check_back", "message_source": "telegram"}
    async with manager._task_progress_scope(AGENT, run):
        run["answer"] = "looked"
    user, answer = _on_disk(tmp_path, AGENT)
    assert user["task_type"] == "check_back"
    assert user["message_source"] == answer["message_source"] == "telegram"


# -- 1 and 4. the bridge: the records it writes and where a turn runs --------------------


def _bridge(root, unified=True, owner_user_id=str(OWNER_ID),
            chats=(str(OWNER_ID), str(OTHER_ID), str(GROUP_CHAT))):
    kwargs = {"owner_user_id": owner_user_id} if owner_user_id is not None else {}
    bridge = AgentTelegramBridge(bot_token="t", allowed_chat_ids=list(chats), agent_id=AGENT,
                                 unified_conversation=unified, burst_quiet_seconds=0, **kwargs)
    bridge.footer_enabled = False
    runs = []

    async def _process_message(**kw):
        runs.append(kw)
        return "ok"

    monitors = {}
    service = SimpleNamespace(
        p2p_manager=SimpleNamespace(node_id=NODE),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        reset_cc_agent_chain=lambda conv: None,
        _check_agent_cc_mention=AsyncMock(),
        _conversation_display_name=lambda c: "",
        propose_new_session=AsyncMock(return_value={"status": "success"}),
    )
    manager = SimpleNamespace(
        service=service, _agent_monitors=monitors, get_session_state=lambda c: None,
        agent=SimpleNamespace(_last_usage={}),
    )
    manager._get_or_create_agent_monitor = lambda conv: monitors.setdefault(conv, _monitor(root, conv))
    bridge.set_message_handler(_process_message, manager)
    bridge.kept_photos = []
    bridge._keep_incoming_photo = lambda conv, data, mid: bridge.kept_photos.append(conv) or None
    bridge._bot = SimpleNamespace(
        get_file=AsyncMock(return_value=SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"jpg")))),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
    )
    bridge._enabled = True
    return bridge, runs, monitors, service


def _update(user_id, chat_id, chat_type, text="hello", photo=False):
    u = MagicMock()
    u.effective_user.id = user_id
    u.effective_user.first_name = "Mike" if user_id == OWNER_ID else "Guest"
    u.effective_chat.id = chat_id
    u.effective_chat.type = chat_type
    u.message.message_id = 7
    u.message.text = text
    u.message.caption = None
    u.message.reply_text = AsyncMock()
    if photo:
        u.message.photo = [SimpleNamespace(file_id="big")]
    return u


def _owner():
    return _update(OWNER_ID, OWNER_ID, "private")


def _stranger(text="hello", **kw):
    return _update(OTHER_ID, OTHER_ID, "private", text=text, **kw)


def _ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))


@pytest.mark.asyncio
async def test_the_owner_s_turn_joins_the_unified_conversation_and_says_whose_it_is(tmp_path):
    bridge, runs, monitors, _ = _bridge(tmp_path)
    await bridge._handle_message(_owner(), _ctx())
    assert [r["conversation_id"] for r in runs] == [AGENT]
    assert runs[0]["_skip_history"] is True
    (record,) = _on_disk(tmp_path, AGENT)
    assert record["message_source"] == OWNER
    assert record["telegram_chat_id"] == str(OWNER_ID)
    assert record["source"] == "telegram"  # the legacy field the ARCH-10 reader uses


@pytest.mark.asyncio
@pytest.mark.parametrize("update", [
    lambda: _stranger(),
    lambda: _update(OWNER_ID, GROUP_CHAT, "group"),
    lambda: _update(OTHER_ID, GROUP_CHAT, "group"),
], ids=["stranger-private", "owner-in-a-group", "stranger-in-a-group"])
async def test_any_other_telegram_turn_runs_in_its_chat_s_own_conversation(tmp_path, update):
    bridge, runs, monitors, _ = _bridge(tmp_path)
    u = update()
    await bridge._handle_message(u, _ctx())
    chat = str(u.effective_chat.id)
    assert [r["conversation_id"] for r in runs] == [f"telegram-{chat}"]
    run = runs[0]
    # Recorded by the manager there, as without unified mode, with source and chat.
    assert run["_skip_history"] is False
    assert run["message_source"] == "telegram" and run["telegram_chat_id"] == chat
    assert AGENT not in monitors and _on_disk(tmp_path, AGENT) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("update,source", [(_owner, OWNER), (_stranger, "telegram")],
                         ids=["owner", "stranger"])
async def test_a_burst_stopped_at_the_gate_closes_with_its_source_and_chat(tmp_path, update, source):
    bridge, _r, _m, _s = _bridge(tmp_path)
    closes = []
    bridge._agent_manager._close_turn_stopped_while_waiting = lambda **kw: closes.append(kw) or "stopped"
    u = update()
    item, _ = bridge._reserve_burst_slot(str(u.effective_chat.id), "text", u, _ctx(), "Guest (Telegram)")
    bridge._fill_burst_slot(item, text="wait")
    await bridge._close_stopped_burst("whatever", item.burst)
    (kw,) = closes
    assert kw["message_source"] == source
    assert kw["telegram_chat_id"] == str(u.effective_chat.id)


@pytest.mark.asyncio
async def test_with_no_owner_id_even_the_owner_s_turn_runs_in_its_chat_s_conversation(tmp_path):
    bridge, runs, monitors, _ = _bridge(tmp_path, owner_user_id="")
    await bridge._handle_message(_owner(), _ctx())
    assert [r["conversation_id"] for r in runs] == [f"telegram-{OWNER_ID}"]
    assert AGENT not in monitors


@pytest.mark.asyncio
async def test_a_stranger_s_photo_is_kept_beside_his_chat_s_conversation(tmp_path):
    bridge, runs, _m, _ = _bridge(tmp_path)
    await bridge._handle_photo_message(_stranger(photo=True), _ctx())
    assert bridge.kept_photos == [f"telegram-{OTHER_ID}"]
    assert [r["conversation_id"] for r in runs] == [f"telegram-{OTHER_ID}"]


@pytest.mark.asyncio
async def test_a_stranger_s_newsession_does_not_close_the_owner_s_conversation(tmp_path):
    bridge, _r, _m, service = _bridge(tmp_path)
    await bridge._handle_newsession_command(_stranger("/newsession"), _ctx())
    service.propose_new_session.assert_awaited_once_with(f"telegram-{OTHER_ID}")


@pytest.mark.asyncio
@pytest.mark.parametrize("update,conv,source", [
    (_owner, AGENT, OWNER),
    (_stranger, f"telegram-{OTHER_ID}", "telegram"),
], ids=["owner", "stranger"])
async def test_a_cc_only_message_records_its_source_and_chat(tmp_path, update, conv, source):
    bridge, runs, monitors, service = _bridge(tmp_path)
    u = update()
    u.message.text = "@CC look at this"
    await bridge._handle_message(u, _ctx())
    assert runs == []
    (record,) = _on_disk(tmp_path, conv)
    assert record["message_source"] == source
    assert record["telegram_chat_id"] == str(u.effective_chat.id)
    assert service._check_agent_cc_mention.await_args.args[0] == conv
    if conv != AGENT:
        assert _on_disk(tmp_path, AGENT) == []


# -- 2. the @CC door -------------------------------------------------------------------


def _service(bridge=None):
    service = CoreService.__new__(CoreService)
    service.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    service.get_cc_display_name = lambda: "CC"
    service._get_agent_display_name = lambda conversation_id=None: "Ark"
    service._get_telegram_bridge_for_conversation = lambda conversation_id: bridge
    service.telegram_manager = None
    service._cc_ark_chain_depths = {}
    return service


def _door_manager(monitor, answer=""):
    calls = []

    async def process_message(**kw):
        calls.append(kw)
        if answer:
            monitor.add_message(role="assistant", content=answer, sender_node_id=AGENT,
                                message_source=kw.get("message_source"))
        return answer

    return SimpleNamespace(_agent_monitors={monitor.conversation_id: monitor},
                           process_message=process_message), calls


def _cc(monitor, text="@Ark have a look"):
    monitor.add_message(role="user", content=text, sender_node_id="cc", sender_name="CC",
                        sender_type="agent")


def _said(monitor, source=None, *, legacy_telegram=False, chat=None, role="user"):
    monitor.add_message(role=role, content="words", sender_node_id=NODE,
                        sender_name="Guest (Telegram)" if chat or legacy_telegram else "Mike",
                        message_source=source,
                        source="telegram" if legacy_telegram else None,
                        telegram_chat_id=chat)


async def _door(monitor, bridge=None, answer=""):
    manager, calls = _door_manager(monitor, answer)
    await _service(bridge)._invoke_agent_in_agent_chat(monitor.conversation_id, "@Ark look", manager)
    return calls[0]["message_source"]


@pytest.mark.asyncio
async def test_a_stranger_s_record_before_cc_s_reply_makes_cc_s_turn_a_telegram_turn(tmp_path):
    m = _monitor(tmp_path, AGENT)
    _said(m, "telegram", chat=str(OTHER_ID))
    _cc(m)
    assert await _door(m) == "telegram"
    assert m.message_history[-2]["message_source"] == "telegram"  # CC's record says so


@pytest.mark.asyncio
async def test_only_the_owner_s_records_leave_cc_s_turn_external_agent(tmp_path):
    m = _monitor(tmp_path, AGENT)
    _said(m)                                  # an unmarked record from before 2026-11-08
    _said(m, "ui")
    _said(m, OWNER, chat=str(OWNER_ID))
    _cc(m)
    assert await _door(m) == "external_agent"


@pytest.mark.asyncio
async def test_a_mix_takes_the_source_that_is_not_the_owner_s(tmp_path):
    m = _monitor(tmp_path, AGENT)
    _said(m, "discord")
    _said(m, "ui")
    _cc(m)
    assert await _door(m) == "discord"


@pytest.mark.asyncio
async def test_an_unmarked_telegram_record_counts_as_not_the_owner_s(tmp_path):
    m = _monitor(tmp_path, AGENT)
    _said(m, legacy_telegram=True)
    _said(m, "ui")
    _cc(m)
    assert await _door(m) == "telegram"


@pytest.mark.asyncio
async def test_the_walk_stops_at_cc_s_previous_message(tmp_path):
    m = _monitor(tmp_path, AGENT)
    _said(m, "telegram", chat=str(OTHER_ID))
    _cc(m, "first")
    _said(m, "ui")
    _cc(m, "second")
    assert await _door(m) == "external_agent"


@pytest.mark.asyncio
async def test_a_chain_a_stranger_started_stays_his_through_the_agent_s_answer(tmp_path):
    m = _monitor(tmp_path, AGENT)
    _said(m, "telegram", chat=str(OTHER_ID))
    _cc(m, "first")
    _said(m, "telegram", role="assistant")   # the agent's answer to the stranger's turn
    _cc(m, "second")
    assert await _door(m) == "telegram"


# -- 3. the reply chat -----------------------------------------------------------------


CHAT_A, CHAT_B = str(OWNER_ID), str(OTHER_ID)


def _two_chat_bridge(tmp_path):
    bridge, _r, _m, _s = _bridge(tmp_path, chats=(CHAT_A, CHAT_B))
    return bridge


def _sent_to(bridge):
    return [c.kwargs["chat_id"] for c in bridge._bot.send_message.await_args_list]


@pytest.mark.asyncio
async def test_a_chain_response_goes_to_the_chat_it_is_given(tmp_path):
    bridge = _two_chat_bridge(tmp_path)
    await bridge.send_chain_response("done", chat_id=CHAT_B)
    assert _sent_to(bridge) == [CHAT_B]


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", [None, "", "333333333"], ids=["none", "empty", "not-allowed"])
async def test_a_chain_response_with_no_allowed_chat_is_sent_nowhere(tmp_path, chat, caplog):
    bridge = _two_chat_bridge(tmp_path)
    with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.agent_telegram_bridge"):
        await bridge.send_chain_response("done", chat_id=chat)
    assert _sent_to(bridge) == []
    assert any("Chain response not sent to Telegram" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_chain_answer_goes_to_the_chat_of_the_message_that_woke_it(tmp_path):
    bridge = _two_chat_bridge(tmp_path)
    m = _monitor(tmp_path, AGENT)
    _said(m, "telegram", legacy_telegram=True, chat=CHAT_B)
    _cc(m)
    await _door(m, bridge=bridge, answer="Ark: done")
    assert _sent_to(bridge) == [CHAT_B]


@pytest.mark.asyncio
async def test_a_chain_answer_in_a_chat_s_own_conversation_goes_back_to_that_chat(tmp_path):
    """A record the manager wrote (no legacy `source`) still names its chat."""
    bridge = _two_chat_bridge(tmp_path)
    m = _monitor(tmp_path, f"telegram-{CHAT_B}")
    _said(m, "telegram", chat=CHAT_B)
    _cc(m)
    await _door(m, bridge=bridge, answer="Ark: done")
    assert _sent_to(bridge) == [CHAT_B]


@pytest.mark.asyncio
async def test_a_chain_answer_after_a_telegram_record_with_no_chat_is_not_sent(tmp_path, caplog):
    bridge = _two_chat_bridge(tmp_path)
    m = _monitor(tmp_path, AGENT)
    _said(m, legacy_telegram=True)
    _cc(m)
    with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.agent_telegram_bridge"):
        await _door(m, bridge=bridge, answer="Ark: done")
    assert _sent_to(bridge) == []
    assert any("recorded no chat" in r.getMessage() for r in caplog.records)


def _cc_service(tmp_path, bridge, monitor):
    manager = SimpleNamespace(_agent_monitors={monitor.conversation_id: monitor},
                              _telegram_bridge=bridge, agent_id=AGENT,
                              get_session_state=lambda _c: {})
    provider = SimpleNamespace(get_manager=MagicMock(return_value=manager),
                               _managers={AGENT: manager})
    service = _service(bridge)
    service.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    bridge._agent_manager = manager
    return service, provider


@pytest.mark.asyncio
async def test_cc_s_reply_goes_to_the_chat_of_the_message_that_woke_cc(tmp_path):
    bridge = _two_chat_bridge(tmp_path)
    m = _monitor(tmp_path, AGENT)
    _said(m, OWNER, chat=CHAT_B)
    service, _p = _cc_service(tmp_path, bridge, m)
    result = await service.send_cc_agent_response(AGENT, "**Yes**")
    assert result["status"] == "success"
    assert _sent_to(bridge) == [CHAT_B]


@pytest.mark.asyncio
async def test_cc_s_reply_to_a_telegram_message_with_no_chat_is_not_sent(tmp_path, caplog):
    bridge = _two_chat_bridge(tmp_path)
    m = _monitor(tmp_path, AGENT)
    _said(m, legacy_telegram=True)
    service, _p = _cc_service(tmp_path, bridge, m)
    with caplog.at_level(logging.INFO, logger="dpc_client_core.service"):
        await service.send_cc_agent_response(AGENT, "**Yes**")
    assert _sent_to(bridge) == []
    assert any("woke CC recorded no chat" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cc_s_reply_in_a_chat_s_own_conversation_reaches_the_agent_of_that_chat(tmp_path):
    """get_manager would make an agent named telegram-<chat>; the bridge names the agent."""
    bridge = _two_chat_bridge(tmp_path)
    conv = f"telegram-{CHAT_B}"
    m = _monitor(tmp_path, conv)
    _said(m, "telegram", chat=CHAT_B)
    service, provider = _cc_service(tmp_path, bridge, m)
    result = await service.send_cc_agent_response(conv, "**Yes**")
    assert result["status"] == "success"
    provider.get_manager.assert_not_called()
    assert _sent_to(bridge) == [CHAT_B]
