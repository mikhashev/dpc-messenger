"""A Telegram turn is the owner's only from his own user id, in a private chat with the bot.

After eafed550 every turn from an agent's Telegram bot was `message_source="telegram"`,
not the owner, so the owner's own messages from his phone lost recall and
memory_search, the unified conversation included. The bot's access is by chat id, and a
chat id does not say who wrote: anyone in an allowed group writes from that group's id
(A-TELEGRAM-TURN-CANNOT-TELL-THE-OWNER-FROM-ANYONE-IN-AN-ALLOWED-CHAT). Mike's call,
2026-11-08, option D: a per-agent `telegram_owner_user_id` beside
`telegram_allowed_chat_ids`; a message counts as the owner's only when it comes from
that user id AND from a private chat; a burst only when every message in it does; an
agent with no id has no owner turns. Such a turn is `TELEGRAM_OWNER_MESSAGE_SOURCE`,
which `is_owner_caller` admits, and a task it schedules keeps it.

Nothing here touches the real ~/.dpc: `Path.home()` is a temporary directory.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import pathlib
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent import retrieval as retrieval_mod
from dpc_client_core.dpc_agent.task_queue import Task
from dpc_client_core.dpc_agent.tools.core import memory_search, schedule_task
from dpc_client_core.dpc_agent.utils import AgentRegistry
from dpc_client_core.managers.agent_manager import DpcAgentManager
from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge
from dpc_client_core.service import CoreService
from dpc_client_core.telegram_service import TelegramService

from tests.test_recall_and_memory_search_serve_only_the_owners_turns import (  # noqa: F401
    OWN_MARK, QUERY, _ctx, _executor_agent, _index, _schedule_ctx, _turn_tail, world,
)

OWNER_ID = 429727247          # the owner's Telegram user id; his private chat has the same id
OTHER_ID = 41783586           # somebody else
GROUP_CHAT = -1001234567890   # an allowed group the owner is in
AGENT = "agent_owner_tg_0001"
TOKEN = "123:abc"

# Read lazily so this file still collects where the constant does not exist yet.
OWNER_SOURCE = getattr(agent_mod, "TELEGRAM_OWNER_MESSAGE_SOURCE", None)


# -- the source and the predicate --------------------------------------------


def test_the_owner_s_telegram_source_is_one_named_constant_and_not_telegram():
    assert isinstance(OWNER_SOURCE, str) and OWNER_SOURCE
    assert OWNER_SOURCE != "telegram"


def test_the_owner_s_telegram_turn_is_the_owner_and_any_other_telegram_turn_is_not():
    assert agent_mod.is_owner_caller(OWNER_SOURCE) is True
    assert agent_mod.is_owner_caller("telegram") is False


# -- the agent bot's door -----------------------------------------------------


def _bridge(owner_user_id=None, chats=(str(OWNER_ID), str(OTHER_ID), str(GROUP_CHAT)),
            transcribe=None):
    kwargs = {} if owner_user_id is None else {"owner_user_id": owner_user_id}
    bridge = AgentTelegramBridge(bot_token=TOKEN, allowed_chat_ids=list(chats),
                                 burst_quiet_seconds=0, **kwargs)
    bridge.footer_enabled = False
    runs = []

    async def _process_message(**kw):
        runs.append(kw)
        return "ok"

    service = SimpleNamespace(_conversation_display_name=lambda c: "")
    if transcribe is not None:
        service.transcribe_audio = transcribe
    manager = SimpleNamespace(service=service, get_session_state=lambda c: None)
    bridge.set_message_handler(_process_message, manager)
    bridge._bot = SimpleNamespace(get_file=AsyncMock(side_effect=_fake_file))
    bridge._keep_incoming_photo = lambda conv, data, mid: {"type": "image",
                                                           "telegram_message_id": mid}
    return bridge, runs


async def _fake_file(file_id):
    return SimpleNamespace(
        download_to_drive=AsyncMock(),
        download_as_bytearray=AsyncMock(return_value=bytearray(file_id.encode())),
    )


_next_id = iter(range(1, 10_000))


def _update(user_id, chat_id, chat_type, text="hello", voice=False, photo=None):
    u = MagicMock()
    u.effective_user.id = user_id
    u.effective_user.first_name = "Mike" if user_id == OWNER_ID else "Guest"
    u.effective_chat.id = chat_id
    u.effective_chat.type = chat_type
    u.message.message_id = next(_next_id)
    u.message.text = text
    u.message.caption = None
    u.message.reply_text = AsyncMock()
    if voice:
        u.message.voice = SimpleNamespace(duration=2, file_size=10, file_id="voice-file")
    if photo:
        u.message.photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id=photo)]
    return u


def _owner_in_private(**kw):
    return _update(OWNER_ID, OWNER_ID, "private", **kw)


def _tg_ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))


@pytest.mark.asyncio
async def test_the_owner_s_text_in_his_private_chat_is_the_owner_s_turn():
    bridge, runs = _bridge(owner_user_id=str(OWNER_ID))
    await bridge._handle_message(_owner_in_private(), _tg_ctx())
    assert [r["message_source"] for r in runs] == [OWNER_SOURCE]


@pytest.mark.asyncio
async def test_the_owner_s_voice_in_his_private_chat_is_the_owner_s_turn():
    transcribe = AsyncMock(return_value={"text": "said aloud", "provider": "fake"})
    bridge, runs = _bridge(owner_user_id=str(OWNER_ID), transcribe=transcribe)
    await bridge._handle_voice_message(_owner_in_private(voice=True), _tg_ctx())
    assert [r["message_source"] for r in runs] == [OWNER_SOURCE]
    assert runs[0]["message"] == "said aloud"


@pytest.mark.asyncio
async def test_the_owner_s_photo_in_his_private_chat_is_the_owner_s_turn():
    bridge, runs = _bridge(owner_user_id=str(OWNER_ID))
    await bridge._handle_photo_message(_owner_in_private(photo="big"), _tg_ctx())
    assert [r["message_source"] for r in runs] == [OWNER_SOURCE]
    assert runs[0]["image_base64"]


@pytest.mark.asyncio
@pytest.mark.parametrize("update,why", [
    (lambda: _update(OWNER_ID, GROUP_CHAT, "group"), "the owner in a group"),
    (lambda: _update(OWNER_ID, GROUP_CHAT, "supergroup"), "the owner in a supergroup"),
    (lambda: _update(OTHER_ID, OTHER_ID, "private"), "somebody else in a private allowed chat"),
    (lambda: _update(OTHER_ID, GROUP_CHAT, "group"), "somebody else in a group"),
    (lambda: _update(OWNER_ID, OTHER_ID, "private"), "the owner's id over another chat id"),
    (lambda: _update(OWNER_ID, OWNER_ID, "group"), "the owner's chat id on a chat not private"),
    (lambda: _update(OWNER_ID, OWNER_ID, None), "a chat of no stated type"),
], ids=lambda v: v if isinstance(v, str) else "")
async def test_anything_but_the_owner_in_his_private_chat_is_a_telegram_turn(update, why):
    bridge, runs = _bridge(owner_user_id=str(OWNER_ID))
    await bridge._handle_message(update(), _tg_ctx())
    assert [r["message_source"] for r in runs] == ["telegram"], why


@pytest.mark.asyncio
async def test_an_agent_with_no_owner_id_has_no_owner_turns():
    bridge, runs = _bridge(owner_user_id=None)
    await bridge._handle_message(_owner_in_private(), _tg_ctx())
    assert [r["message_source"] for r in runs] == ["telegram"]


@pytest.mark.asyncio
async def test_an_empty_owner_id_is_no_owner_id():
    bridge, runs = _bridge(owner_user_id="")
    await bridge._handle_message(_owner_in_private(), _tg_ctx())
    assert [r["message_source"] for r in runs] == ["telegram"]


def _gathered_run(bridge, chat_id, updates):
    """Messages that arrived in a row in one chat, handed to the agent as one run."""
    items = []
    for u in updates:
        item, _owner = bridge._reserve_burst_slot(str(chat_id), "text", u, _tg_ctx(),
                                                  bridge._sender_name_of(u))
        bridge._fill_burst_slot(item, text=u.message.text)
        items.append(item)
    burst = items[0].burst
    batch = bridge._take_burst_batch(burst)
    assert len(batch) == len(updates)
    return bridge._run_burst_batch(str(chat_id), f"telegram-{chat_id}", batch, burst)


@pytest.mark.asyncio
async def test_a_burst_of_the_owner_s_own_messages_is_the_owner_s_turn():
    bridge, runs = _bridge(owner_user_id=str(OWNER_ID))
    await _gathered_run(bridge, OWNER_ID, [_owner_in_private(text="one"),
                                           _owner_in_private(text="two")])
    assert [r["message_source"] for r in runs] == [OWNER_SOURCE]


@pytest.mark.asyncio
async def test_a_mixed_burst_in_a_group_is_a_telegram_turn():
    bridge, runs = _bridge(owner_user_id=str(OWNER_ID))
    await _gathered_run(bridge, GROUP_CHAT, [_update(OWNER_ID, GROUP_CHAT, "group", text="one"),
                                             _update(OTHER_ID, GROUP_CHAT, "group", text="two")])
    assert [r["message_source"] for r in runs] == ["telegram"]


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["owner_first", "owner_last"])
async def test_one_message_not_the_owner_s_makes_the_whole_burst_a_telegram_turn(order):
    bridge, runs = _bridge(owner_user_id=str(OWNER_ID))
    owner = _owner_in_private(text="mine")
    stranger = _update(OTHER_ID, OWNER_ID, "private", text="not mine")
    updates = [owner, stranger] if order == "owner_first" else [stranger, owner]
    await _gathered_run(bridge, OWNER_ID, updates)
    assert [r["message_source"] for r in runs] == ["telegram"]


@pytest.mark.asyncio
async def test_the_decision_is_taken_from_each_message_on_arrival():
    """A message the owner sent counts as his even if a later look at the update would
    not say so: what decides is the update as it arrived."""
    bridge, runs = _bridge(owner_user_id=str(OWNER_ID))
    u = _owner_in_private()
    item, _ = bridge._reserve_burst_slot(str(OWNER_ID), "text", u, _tg_ctx(), "Mike (Telegram)")
    u.effective_user.id = OTHER_ID  # changed after arrival
    bridge._fill_burst_slot(item, text="hello")
    batch = bridge._take_burst_batch(item.burst)
    await bridge._run_burst_batch(str(OWNER_ID), f"telegram-{OWNER_ID}", batch, item.burst)
    assert [r["message_source"] for r in runs] == [OWNER_SOURCE]


# -- the owner's turn reaches the owner's memory ------------------------------


@pytest.mark.asyncio
async def test_the_owner_s_telegram_turn_gets_its_recall_hints(world, monkeypatch):
    bridge, runs = _bridge(owner_user_id=str(OWNER_ID))
    await bridge._handle_message(_owner_in_private(), _tg_ctx())
    _index(world)
    assert OWN_MARK in await _turn_tail(world, monkeypatch, runs[0]["message_source"])


def test_memory_search_answers_the_owner_s_telegram_turn(world, monkeypatch):
    opened = []

    def _index_opened(*a, **kw):
        opened.append(True)
        raise RuntimeError("stop here")

    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent", _index_opened)
    answer = memory_search(_ctx(world, OWNER_SOURCE), QUERY)
    assert opened, answer
    assert "answers only the owner" not in answer


# -- a task the owner's Telegram turn schedules -------------------------------


def test_schedule_task_stores_the_owner_s_telegram_source():
    ctx = _schedule_ctx(OWNER_SOURCE)
    assert schedule_task(ctx, "chat", '{"text": "later"}').startswith("✓")
    assert ctx._scheduled[0].data["_message_source"] == OWNER_SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["chat", "check_back", "review"])
async def test_the_task_runs_as_the_owner_s_turn(task_type):
    """What schedule_task stored, run by each executor (a check_back's own approval
    gate is not under test here, so the payload is the one `chat` stores)."""
    ctx = _schedule_ctx(OWNER_SOURCE)
    schedule_task(ctx, "chat", '{"text": "look again"}')
    stored = ctx._scheduled[0].data
    agent, calls = _executor_agent()
    task = Task(id="t1", task_type=task_type,
                data={**stored, "_reply_conversation_id": "agent_001"})
    await agent._execute_task_guarded(task)
    assert calls[-1].get("message_source") == OWNER_SOURCE
    assert agent_mod.is_owner_caller(calls[-1]["message_source"]) is True


# -- the key on the agent's row: written, checked, kept, cleared --------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    AgentRegistry().register_agent(AGENT, "Owner test agent")
    return tmp_path


def _row():
    return AgentRegistry().get_agent(AGENT)


def _link(**kw):
    return AgentRegistry().link_agent_to_telegram(AGENT, bot_token=TOKEN,
                                                  chat_ids=[str(OWNER_ID)], **kw)


def test_the_owner_id_is_stored_beside_the_allowed_chat_ids(home):
    _link(owner_user_id=str(OWNER_ID))

    assert _row()["telegram_owner_user_id"] == str(OWNER_ID)
    stored = json.loads((home / ".dpc" / "agents" / "_registry.json").read_text("utf-8"))
    assert stored["agents"][AGENT]["telegram_owner_user_id"] == str(OWNER_ID)
    assert stored["agents"][AGENT]["telegram_allowed_chat_ids"] == [str(OWNER_ID)]


def test_surrounding_spaces_are_not_part_of_the_id(home):
    _link(owner_user_id=f"  {OWNER_ID} ")
    assert _row()["telegram_owner_user_id"] == str(OWNER_ID)


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_an_empty_field_stores_no_owner(home, empty):
    _link(owner_user_id=str(OWNER_ID))
    _link(owner_user_id=empty)
    assert _row()["telegram_owner_user_id"] is None


@pytest.mark.parametrize("bad", ["abc", "-429727247", "4297x7247", "42 97", "4.2", "+429727247",
                                 "４２９", "٤٢", 429727247, True, ["1"]])
def test_anything_but_digits_is_refused_and_nothing_is_written(home, bad):
    with pytest.raises(ValueError, match="owner"):
        _link(owner_user_id=bad)
    row = _row()
    assert "telegram_owner_user_id" not in row
    assert not row.get("telegram_bot_token")


def test_unlinking_clears_the_owner_id(home):
    _link(owner_user_id=str(OWNER_ID))

    AgentRegistry().unlink_agent_from_telegram(AGENT)

    assert _row()["telegram_owner_user_id"] is None
    assert _row()["telegram_allowed_chat_ids"] is None


class _OldBridge:
    async def stop(self):
        pass


def _telegram_service_with_a_live_agent():
    """TelegramService over a manager that runs the real `_start_telegram_bridge`."""
    svc = TelegramService.__new__(TelegramService)
    svc._telegram_link_locks = {}
    manager = SimpleNamespace(
        agent_id=AGENT,
        service=SimpleNamespace(settings=None, telegram_manager=None),
        process_message=AsyncMock(),
        _telegram_bridge=_OldBridge(),
        _telegram_bridge_lock=asyncio.Lock(),
        _stop_event=threading.Event(),
    )
    for name in ("_start_telegram_bridge", "_start_telegram_bridge_unlocked",
                 "_stop_telegram_bridge", "_stop_telegram_bridge_unlocked"):
        setattr(manager, name, MethodType(getattr(DpcAgentManager, name), manager))
    svc.llm_manager = SimpleNamespace(
        providers={"dpc_agent": SimpleNamespace(_managers={AGENT: manager})})
    return svc, manager


def test_saving_the_dialog_keeps_the_keys_the_dialog_does_not_know(home, monkeypatch):
    monkeypatch.setattr(AgentTelegramBridge, "start", AsyncMock(return_value=True))
    AgentRegistry().update_agent(AGENT, {
        "telegram_footer_enabled": False,
        "telegram_unified_conversation": True,
        "compute_host": "dpc-node-feedfacefeedfacefeedfacefeedface",
        "some_key_from_a_later_version": {"kept": True},
    })
    before = dict(_row())
    svc, _ = _telegram_service_with_a_live_agent()

    result = asyncio.run(svc.link_agent_telegram(
        AGENT, TOKEN, [str(OWNER_ID)], unified_conversation=True,
        owner_user_id=str(OWNER_ID)))

    assert result["status"] == "success", result
    row = _row()
    for key in ("agent_id", "name", "provider_alias", "profile_name", "created_at",
                "instruction_set_name", "telegram_footer_enabled", "compute_host",
                "some_key_from_a_later_version"):
        assert row[key] == before[key], key
    assert row["telegram_owner_user_id"] == str(OWNER_ID)


def test_the_saved_owner_id_reaches_the_bridge_at_once(home, monkeypatch):
    monkeypatch.setattr(AgentTelegramBridge, "start", AsyncMock(return_value=True))
    svc, manager = _telegram_service_with_a_live_agent()

    asyncio.run(svc.link_agent_telegram(AGENT, TOKEN, [str(OWNER_ID)],
                                        owner_user_id=str(OWNER_ID)))
    assert manager._telegram_bridge._owner_user_id == OWNER_ID

    asyncio.run(svc.link_agent_telegram(AGENT, TOKEN, [str(OWNER_ID)], owner_user_id=""))
    assert manager._telegram_bridge._owner_user_id is None


def test_an_agent_whose_row_never_had_the_key_builds_a_bridge_with_no_owner(home, monkeypatch):
    monkeypatch.setattr(AgentTelegramBridge, "start", AsyncMock(return_value=True))
    _link(owner_user_id=str(OWNER_ID))
    registry = AgentRegistry()
    del registry._registry["agents"][AGENT]["telegram_owner_user_id"]
    registry._save_registry()
    _, manager = _telegram_service_with_a_live_agent()

    asyncio.run(manager._start_telegram_bridge())

    assert manager._telegram_bridge._owner_user_id is None


def test_a_refused_owner_id_reaches_the_caller_as_an_error_and_writes_nothing(home):
    svc, _ = _telegram_service_with_a_live_agent()

    result = asyncio.run(svc.link_agent_telegram(AGENT, TOKEN, [str(OWNER_ID)],
                                                 owner_user_id="@mike"))

    assert result["status"] == "error"
    assert "owner" in result["message"]
    assert not _row().get("telegram_bot_token")


def test_the_core_service_command_hands_the_owner_id_on():
    """The local API calls the command with the payload as keywords."""
    assert "owner_user_id" in inspect.signature(CoreService.link_agent_telegram).parameters
    telegram = SimpleNamespace(link_agent_telegram=AsyncMock(return_value={"status": "success"}))
    core = SimpleNamespace(telegram_service=telegram)

    asyncio.run(CoreService.link_agent_telegram(
        core, AGENT, TOKEN, [str(OWNER_ID)], owner_user_id=str(OWNER_ID)))

    assert telegram.link_agent_telegram.await_args.kwargs["owner_user_id"] == str(OWNER_ID)


def test_a_hand_edited_owner_id_that_is_not_digits_gives_no_owner_and_the_bot_still_runs():
    """The save path refuses it; a row edited by hand reaches the bridge unchecked, and
    there it fails closed rather than taking the bot down."""
    bridge = AgentTelegramBridge(bot_token=TOKEN, allowed_chat_ids=[str(OWNER_ID)],
                                 owner_user_id="mike")
    assert bridge._owner_user_id is None
