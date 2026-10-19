"""THE-UI-KEEPS-CHATS-AND-AI-CHAT-HISTORIES-IN-THE-WEBVIEW-OUTSIDE-DOT-DPC.

Mike's call 2026-10-19, Variant A: the backend is the only owner of chats and
their histories. An AI chat's history is written to its folder in ~/.dpc by the
same writer as an agent's, read back after a restart, and what the old UI kept
in localStorage comes across once without overwriting anything on disk.
"""

import json
import pathlib
from types import SimpleNamespace

import pytest

from dpc_client_core import ai_chats
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.service import CoreService

CID = "ai_chat_1a2b3c4d"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    return tmp_path


def _service(links=None):
    """A CoreService with only what the AI chat paths touch — one per 'start'."""
    ks = KnowledgeService.__new__(KnowledgeService)
    ks.conversation_monitors = {}
    ks.p2p_manager = SimpleNamespace(node_id="dpc-node-" + "b" * 32)
    ks.llm_manager = None
    ks.settings = None
    ks.instruction_set = SimpleNamespace(default="general")
    ks.group_manager = None
    ks.peer_metadata = {}
    ks._send_ai_query = None
    svc = CoreService.__new__(CoreService)
    svc.knowledge_service = ks
    svc.conversation_monitors = ks.conversation_monitors
    svc._voice_transcriptions = {}
    svc.telegram_service = None
    svc.settings = SimpleNamespace(get=lambda *a, **k: json.dumps(links or {}))
    return svc


def _history_file(home, cid=CID):
    return home / ".dpc" / "conversations" / cid / "history.json"


def _stored(home, cid=CID):
    return json.loads(_history_file(home, cid).read_text(encoding="utf-8"))["messages"]


def _ui_message(mid, sender, text, ts, **extra):
    return {"id": mid, "sender": sender, "text": text, "timestamp": ts, **extra}


# --- persistence round trip -------------------------------------------------


@pytest.mark.asyncio
async def test_an_ai_chat_history_survives_a_restart(home):
    first = _service()
    monitor = first._get_or_create_conversation_monitor(CID)
    monitor.add_message("user", "What is a vector clock?")
    monitor.add_message("assistant", "A causality counter.", thinking="recall Lamport", sender_name="AI (q3)")
    assert monitor.save_history()
    assert _history_file(home).exists(), "an AI chat must persist by default, like an agent"

    second = _service()  # a restart: no monitor in memory
    result = await second.get_conversation_history(CID)

    assert result["status"] == "success"
    assert [m["content"] for m in result["messages"]] == ["What is a vector clock?", "A causality counter."]
    assert result["messages"][1]["thinking"] == "recall Lamport"
    assert result["messages"][1]["sender_name"] == "AI (q3)"
    assert all(m["message_id"] for m in result["messages"])


def test_the_first_message_after_a_restart_does_not_overwrite_the_history(home):
    first = _service()
    m1 = first._get_or_create_conversation_monitor(CID)
    m1.add_message("user", "one")
    m1.save_history()

    m2 = _service()._get_or_create_conversation_monitor(CID)
    m2.add_message("user", "two")
    m2.save_history()

    assert [m["content"] for m in _stored(home)] == ["one", "two"]


def test_local_ai_stays_unpersisted(home):
    monitor = _service()._get_or_create_conversation_monitor("local_ai")
    monitor.add_message("user", "scratch")
    monitor.save_history()
    assert not _history_file(home, "local_ai").exists()


# --- the one-time import ----------------------------------------------------


UI_HISTORY = [
    _ui_message("u1", "user", "hello", 1760000000000),
    _ui_message("a1", "ai", "hi there", 1760000001000, model="q3", thinking="greet", tool_calls=[]),
    _ui_message("s1", "system", "Error: provider down", 1760000002000),
    _ui_message("p1", "ai", "Thinking...", 1760000003000, commandId="cmd-1"),
]
UI_RECORD = {"name": "qwen (q3)", "provider": "qwen", "instruction_set_name": "general", "junk": {"x": 1}}


@pytest.mark.asyncio
async def test_import_brings_record_and_dialogue_and_lists_the_chat(home):
    svc = _service()
    result = await svc.import_ai_chats(chats={CID: UI_RECORD}, histories={CID: UI_HISTORY})

    (row,) = result["results"]
    assert row["status"] == "imported"
    assert row["messages_added"] == 2 and row["messages_skipped"] == 2
    stored = _stored(home)
    assert [(m["id"], m["role"], m["content"]) for m in stored] == [
        ("u1", "user", "hello"), ("a1", "assistant", "hi there"),
    ]
    assert stored[1]["thinking"] == "greet"
    assert stored[1]["sender_name"] == "AI (q3)"

    listed = (await svc.list_ai_chats())["chats"]
    assert [(c["conversation_id"], c["name"], c["provider"]) for c in listed] == [(CID, "qwen (q3)", "qwen")]
    assert "junk" not in listed[0]


@pytest.mark.asyncio
async def test_a_second_import_of_the_same_store_changes_nothing(home):
    svc = _service()
    await svc.import_ai_chats(chats={CID: UI_RECORD}, histories={CID: UI_HISTORY})
    before = _history_file(home).read_bytes()

    again = await _service().import_ai_chats(chats={CID: UI_RECORD}, histories={CID: UI_HISTORY})

    (row,) = again["results"]
    assert row["status"] == "kept" and row["messages_added"] == 0
    assert _history_file(home).read_bytes() == before


@pytest.mark.asyncio
async def test_import_never_overwrites_what_the_backend_holds(home):
    svc = _service()
    monitor = svc._get_or_create_conversation_monitor(CID)
    monitor.add_message("user", "said on disk", message_id="u1", timestamp="2026-01-01T00:00:10+00:00")
    monitor.save_history()
    ai_chats.write_record(CID, {"name": "Disk name"})

    older = [
        _ui_message("u1", "user", "a different text under the same id", 1767225600000),  # 2026-01-01T00:00:00
        _ui_message("u0", "user", "only in the UI", 1767225605000),                     # 2026-01-01T00:00:05
    ]
    result = await svc.import_ai_chats(chats={CID: {"name": "UI name"}}, histories={CID: older})

    assert result["results"][0]["messages_added"] == 1
    stored = _stored(home)
    assert [(m["id"], m["content"]) for m in stored] == [("u0", "only in the UI"), ("u1", "said on disk")]
    assert [m["msg_index"] for m in stored] == [1, 2], "the chain is rebuilt in time order"
    assert ai_chats.read_record(CID)["name"] == "Disk name"


@pytest.mark.asyncio
async def test_malformed_input_is_refused_and_writes_nothing(home):
    svc = _service()
    assert (await svc.import_ai_chats(chats="not an object"))["status"] == "error"

    result = await svc.import_ai_chats(
        chats={
            "../escape": {"name": "x"},
            "dpc-node-" + "c" * 32: {"name": "peer"},
            "ai_chat_badrecord": "a string",
            "agent_001": {"name": "Ark"},
        },
        histories={"ai_chat_badhistory": {"not": "a list"}},
    )
    by_id = {r["conversation_id"]: (r["status"], r.get("reason", "")) for r in result["results"]}
    assert by_id["../escape"][0] == "refused"
    assert by_id["dpc-node-" + "c" * 32][0] == "refused"
    assert by_id["ai_chat_badrecord"][0] == "refused"
    assert by_id["ai_chat_badhistory"][0] == "refused"
    assert by_id["agent_001"][0] == "skipped"
    conversations = home / ".dpc" / "conversations"
    assert not conversations.exists() or list(conversations.iterdir()) == []


# --- Telegram chats come from the bridge's links ----------------------------


@pytest.mark.asyncio
async def test_telegram_chats_are_listed_from_the_links_and_named_by_their_record(home):
    svc = _service(links={"42": "telegram-42", "7": "agent-agent_001"})
    imported = await svc.import_ai_chats(chats={"telegram-42": {"name": "📱 Telegram (Mike)", "provider": "telegram"}})
    assert imported["results"][0]["status"] == "imported"

    listed = (await svc.list_ai_chats())["chats"]
    assert listed == [{"conversation_id": "telegram-42", "provider": "telegram",
                       "name": "📱 Telegram (Mike)", "telegram_chat_id": "42"}]

    # A record with no link behind it is not a chat: that is the stale-after-reset case.
    ai_chats.write_record("telegram-99", {"name": "stale"})
    assert [c["conversation_id"] for c in (await svc.list_ai_chats())["chats"]] == ["telegram-42"]


@pytest.mark.asyncio
async def test_save_ai_chat_writes_a_record_and_refuses_other_ids(home):
    svc = _service()
    ok = await svc.save_ai_chat(CID, {"name": "new chat", "provider": "qwen"})
    assert ok["status"] == "success"
    assert ai_chats.read_record(CID)["name"] == "new chat"
    assert (await svc.save_ai_chat("group-abc", {"name": "x"}))["status"] == "error"
    assert (await svc.save_ai_chat(CID, "x"))["status"] == "error"


# --- review fixes, 2026-10-19 -----------------------------------------------


async def _noop_broadcast(*args, **kwargs):
    return None


def _with_api(svc):
    svc.knowledge_service.local_api = SimpleNamespace(broadcast_event=_noop_broadcast)
    return svc


@pytest.mark.asyncio
async def test_a_reset_is_not_undone_by_the_next_import(home):
    svc = _service()
    await svc.import_ai_chats(chats={CID: UI_RECORD}, histories={CID: UI_HISTORY})
    svc._get_or_create_conversation_monitor(CID).reset_conversation()
    assert not _history_file(home).exists()

    again = await _service().import_ai_chats(chats={CID: UI_RECORD}, histories={CID: UI_HISTORY})

    assert again["results"][0]["status"] == "kept"
    assert not _history_file(home).exists(), "the reset must stand"


@pytest.mark.asyncio
async def test_a_delete_is_not_undone_by_the_next_import(home):
    svc = _with_api(_service())
    await svc.import_ai_chats(chats={CID: UI_RECORD}, histories={CID: UI_HISTORY})
    assert (await svc.knowledge_service.delete_conversation(CID))["status"] == "success"

    again = await _service().import_ai_chats(chats={CID: UI_RECORD}, histories={CID: UI_HISTORY})

    assert again["results"][0]["status"] == "kept"
    assert not (home / ".dpc" / "conversations" / CID).exists()


@pytest.mark.asyncio
async def test_a_chat_that_does_not_persist_is_deferred_not_settled(home):
    folder = home / ".dpc" / "conversations" / CID
    folder.mkdir(parents=True)
    (folder / "settings.json").write_text(json.dumps({"persist_history": False}), encoding="utf-8")

    result = await _service().import_ai_chats(chats={CID: UI_RECORD}, histories={CID: UI_HISTORY})

    assert result["results"][0]["status"] == "deferred"
    assert not _history_file(home).exists()
    assert ai_chats.read_record(CID) is None


@pytest.mark.asyncio
async def test_a_history_sent_in_parts_is_imported_once_whole(home):
    first, second = UI_HISTORY[:1], UI_HISTORY[1:2]
    part = await _service().import_ai_chats(chats={CID: UI_RECORD}, histories={CID: first}, partial=True)
    assert part["results"][0]["status"] == "imported"
    last = await _service().import_ai_chats(histories={CID: second})
    assert last["results"][0]["messages_added"] == 1
    assert [m["id"] for m in _stored(home)] == ["u1", "a1"]
    again = await _service().import_ai_chats(histories={CID: UI_HISTORY})
    assert again["results"][0]["status"] == "kept"


def test_an_error_bubble_is_not_imported_as_an_answer():
    bubble = _ui_message("e1", "ai", "⚠️ **Out of Memory**", 1760000000000, isError=True)
    assert ai_chats.message_from_ui(CID, bubble, None) is None


def test_a_failed_save_leaves_the_previous_history_whole(home):
    monitor = _service()._get_or_create_conversation_monitor(CID)
    monitor.add_message("user", "kept")
    assert monitor.save_history()
    monitor.message_history.append({"id": "bad", "role": "user", "content": object()})

    assert monitor.save_history() is False
    assert [m["content"] for m in _stored(home)] == ["kept"]
    assert [p.name for p in _history_file(home).parent.iterdir() if p.name.endswith(".tmp")] == []


def test_a_torn_history_is_moved_aside_not_overwritten(home):
    folder = home / ".dpc" / "conversations" / CID
    folder.mkdir(parents=True)
    torn = '{"messages": [{"id": "x", "role": "user", "content": "old"'
    (folder / "history.json").write_text(torn, encoding="utf-8")

    monitor = _service()._get_or_create_conversation_monitor(CID)
    monitor.add_message("user", "new")
    monitor.save_history()

    aside = [p for p in folder.iterdir() if p.name.startswith("history.json.corrupt-")]
    assert len(aside) == 1 and aside[0].read_text(encoding="utf-8") == torn
    assert [m["content"] for m in _stored(home)] == ["new"]


def test_a_hyphenated_ai_id_neither_persists_nor_takes_another_chats_folder(home):
    assert not ai_chats.is_ai_chat_id("ai_x-y")
    hyphen = _service()._get_or_create_conversation_monitor("ai_x-y")
    hyphen.add_message("user", "I am ai_x-y")
    hyphen.save_history()
    assert not (home / ".dpc" / "conversations" / "ai_x-y").exists()
    assert _service()._get_or_create_conversation_monitor("ai_x").message_history == []


@pytest.mark.asyncio
async def test_an_answer_in_flight_does_not_recreate_a_deleted_chat(home):
    svc = _with_api(_service())
    monitor = svc._get_or_create_conversation_monitor(CID)  # what execute_ai_query holds
    monitor.add_message("user", "question")
    monitor.save_history()

    assert (await svc.knowledge_service.delete_conversation(CID))["status"] == "success"
    monitor.add_message("assistant", "the answer arrives after the delete")
    monitor.save_history()

    assert not (home / ".dpc" / "conversations" / CID).exists()


@pytest.mark.asyncio
async def test_delete_reports_a_folder_it_could_not_remove(home, monkeypatch):
    svc = _with_api(_service())
    monitor = svc._get_or_create_conversation_monitor(CID)
    monitor.add_message("user", "x")
    monitor.save_history()
    monkeypatch.setattr(monitor, "delete_conversation_folder", lambda: False)

    result = await svc.knowledge_service.delete_conversation(CID)

    assert result["status"] == "error"
    assert CID in svc.conversation_monitors


@pytest.mark.asyncio
async def test_delete_refuses_an_id_that_leaves_the_conversations_folder(home):
    victim = home / ".dpc" / "keep-me"
    victim.mkdir(parents=True)
    (home / ".dpc" / "conversations").mkdir(parents=True)
    svc = _with_api(_service())

    result = await svc.knowledge_service.delete_conversation("../keep-me")

    assert result["status"] == "error"
    assert victim.exists()
