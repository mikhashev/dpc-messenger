"""A chat's live history is served in pages (A-CHAT-LOADS-ITS-WHOLE-LIVE-HISTORY-IN-ONE-ANSWER).

`get_conversation_history` used to answer with the whole live history in one
local-API message. It now answers the newest page, or the page before the
record named by `before_message_id`, cut by `limit` and by bytes, with
`has_older` saying whether more is left. An unknown cursor is an error, never
the newest page. Every branch — group and agent from disk, peer, AI, local AI
and Telegram chats from their monitor — pages the same way.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dpc_client_core import history_page
from dpc_client_core import service as service_module
from dpc_client_core.service import CoreService

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
BOUNDARY = "2026-09-30T12:00:00+00:00"


def records(n, content="hello", start=0):
    return [
        {"id": f"m{i}", "role": "user" if i % 2 else "assistant",
         "content": f"{content} {i}", "timestamp": (T0 + timedelta(minutes=i)).isoformat(),
         "sender_node_id": "dpc-node-" + "b" * 32, "sender_name": "Bob"}
        for i in range(start, start + n)
    ]


def make_service():
    svc = CoreService.__new__(CoreService)
    svc.conversation_monitors = {}
    svc.llm_manager = None
    svc._voice_transcriptions = {}
    svc._group_agent_context = {}
    svc._resolve_agent_token_limit = lambda _id: 0
    svc._worst_group_agent_context = lambda _id: None
    svc._group_agent_context_list = lambda _id: []
    return svc


class FakeGroupManager:
    def __init__(self, conv_dir):
        self._conv_dir = conv_dir

    def _get_conversation_dir(self, conversation_id):
        return self._conv_dir


class FakeMonitor:
    """What the monitor branch reads, and nothing else."""

    def __init__(self, history, boundary=None):
        self.message_history = history
        self.live_history_boundary = boundary
        self._tokens_after_last_response = 7
        self._tokens_after_last_response_at = None

    def get_message_history(self):
        return list(self.message_history)

    def get_token_usage(self):
        return {"tokens_used": 3, "token_limit": 1000}

    def set_token_count(self, n):
        pass


def group_service(tmp_path, history, boundary=None):
    conv_dir = tmp_path / "group-abc-slug"
    conv_dir.mkdir()
    (conv_dir / "history.json").write_text(json.dumps({"messages": history}), encoding="utf-8")
    if boundary:
        (conv_dir / "settings.json").write_text(
            json.dumps({"live_history_boundary": boundary}), encoding="utf-8")
    svc = make_service()
    svc.group_manager = FakeGroupManager(conv_dir)
    return svc, "group-abc"


def agent_service(tmp_path, monkeypatch, history, boundary=None):
    conv_dir = tmp_path / "conversations" / "agent_x"
    conv_dir.mkdir(parents=True)
    (conv_dir / "history.json").write_text(
        json.dumps({"messages": history, "token_stats": {"current_token_count": 42}}), encoding="utf-8")
    if boundary:
        (conv_dir / "settings.json").write_text(
            json.dumps({"live_history_boundary": boundary}), encoding="utf-8")
    monkeypatch.setattr(service_module, "DPC_HOME_DIR", tmp_path)
    return make_service(), "agent_x"


def monitor_service(cid, history, boundary=None):
    svc = make_service()
    svc.conversation_monitors[cid] = FakeMonitor(history, boundary)
    return svc, cid


MONITOR_KINDS = [
    "dpc-node-" + "c" * 32,   # 1:1 peer
    "ai_chat_1a2b3c4d",       # AI chat
    "local_ai",               # the local AI chat
    "telegram-123456",        # Telegram chat
]


async def walk(svc, cid, limit=None):
    """Every page from the newest back, as the UI pager asks for them."""
    pages = []
    cursor = None
    while True:
        kwargs = {}
        if cursor:
            kwargs["before_message_id"] = cursor
        if limit is not None:
            kwargs["limit"] = limit
        result = await svc.get_conversation_history(cid, **kwargs)
        assert result["status"] == "success", result
        pages.append(result)
        if not result["has_older"]:
            return pages
        cursor = result["messages"][0]["message_id"]


def ids_of(pages):
    """Pages arrive newest first; the UI prepends each, so read them oldest first."""
    out = []
    for page in reversed(pages):
        out.extend(m["message_id"] for m in page["messages"])
    return out


# --- the newest page and the walk back -------------------------------------


@pytest.mark.asyncio
async def test_the_first_answer_is_the_newest_default_page(tmp_path):
    history = records(250)
    svc, cid = group_service(tmp_path, history)

    result = await svc.get_conversation_history(cid)

    assert [m["message_id"] for m in result["messages"]] == [f"m{i}" for i in range(150, 250)]
    assert result["has_older"] is True
    # Fields that describe the conversation describe all of it, not the page.
    assert result["message_count"] == 250
    assert result["history_tokens"] == sum(len(m["content"]) for m in history) // 4


@pytest.mark.asyncio
async def test_walking_back_returns_every_record_once_and_in_order(tmp_path):
    history = records(237)
    svc, cid = group_service(tmp_path, history)

    pages = await walk(svc, cid, limit=50)

    assert len(pages) == 5
    assert ids_of(pages) == [m["id"] for m in history]
    assert pages[-1]["has_older"] is False
    assert all(p["has_older"] for p in pages[:-1])


@pytest.mark.asyncio
async def test_a_page_is_in_the_order_the_history_is_presented_in(tmp_path):
    # Stored out of order (arrival order); presented and paged by timestamp.
    history = records(6)
    shuffled = [history[i] for i in (3, 0, 5, 1, 4, 2)]
    svc, cid = group_service(tmp_path, shuffled)

    pages = await walk(svc, cid, limit=2)

    assert ids_of(pages) == ["m0", "m1", "m2", "m3", "m4", "m5"]


@pytest.mark.asyncio
async def test_a_short_history_is_one_page_with_nothing_older(tmp_path):
    svc, cid = group_service(tmp_path, records(3))

    result = await svc.get_conversation_history(cid)

    assert len(result["messages"]) == 3
    assert result["has_older"] is False


# --- the byte cut -------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_messages_cut_the_page_by_bytes_and_leave_has_older_true(tmp_path):
    # 40 messages of ~60 KB as JSON: 2.4 MB, far past the page budget.
    history = records(40, content="x" * 60_000)
    svc, cid = group_service(tmp_path, history)

    result = await svc.get_conversation_history(cid, limit=100)

    page = result["messages"]
    assert 1 <= len(page) < 40
    assert result["has_older"] is True
    assert len(json.dumps(page)) <= history_page.PAGE_BYTES
    assert page[-1]["message_id"] == "m39"
    # The next cursor is the oldest message returned: nothing is skipped.
    pages = await walk(svc, cid, limit=100)
    assert ids_of(pages) == [m["id"] for m in history]


@pytest.mark.asyncio
async def test_a_message_larger_than_the_budget_still_comes_alone(tmp_path):
    history = records(3, content="y" * (history_page.PAGE_BYTES + 10))
    svc, cid = group_service(tmp_path, history)

    result = await svc.get_conversation_history(cid)

    assert [m["message_id"] for m in result["messages"]] == ["m2"]
    assert result["has_older"] is True


# --- refusals -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, history_page.MAX_LIMIT + 1, True, "10", 2.5])
async def test_a_limit_that_is_not_a_page_size_is_refused(tmp_path, limit):
    svc, cid = group_service(tmp_path, records(5))

    result = await svc.get_conversation_history(cid, limit=limit)

    assert result["status"] == "error"
    assert "limit" in result["message"]
    assert result["messages"] == []
    assert result["has_older"] is False


@pytest.mark.asyncio
async def test_the_cap_itself_is_accepted(tmp_path):
    svc, cid = group_service(tmp_path, records(5))

    result = await svc.get_conversation_history(cid, limit=history_page.MAX_LIMIT)

    assert result["status"] == "success"
    assert len(result["messages"]) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["gone-after-a-reset", ""])
async def test_an_unknown_cursor_is_an_error_not_the_newest_page(tmp_path, cursor):
    svc, cid = group_service(tmp_path, records(5))

    result = await svc.get_conversation_history(cid, before_message_id=cursor)

    assert result["status"] == "error"
    assert result["messages"] == []
    assert result["has_older"] is False
    assert "before_message_id" in result["message"]


@pytest.mark.asyncio
async def test_a_cursor_on_the_oldest_record_returns_an_empty_last_page(tmp_path):
    svc, cid = group_service(tmp_path, records(5))

    result = await svc.get_conversation_history(cid, before_message_id="m0")

    assert result["status"] == "success"
    assert result["messages"] == []
    assert result["has_older"] is False


# --- every chat kind ------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_chat_pages_from_its_history_file(tmp_path, monkeypatch):
    history = records(130)
    svc, cid = agent_service(tmp_path, monkeypatch, history, boundary=BOUNDARY)

    first = await svc.get_conversation_history(cid)
    pages = await walk(svc, cid, limit=40)

    assert len(first["messages"]) == history_page.DEFAULT_LIMIT
    assert first["message_count"] == 130
    assert first["tokens_used"] == 42
    assert first["live_history_boundary"] == BOUNDARY
    assert ids_of(pages) == [m["id"] for m in history]
    bad = await svc.get_conversation_history(cid, before_message_id="nope")
    assert bad["status"] == "error"


@pytest.mark.asyncio
async def test_a_group_answer_names_its_live_history_boundary(tmp_path):
    svc, cid = group_service(tmp_path, records(3), boundary=BOUNDARY)

    result = await svc.get_conversation_history(cid)

    assert result["live_history_boundary"] == BOUNDARY


@pytest.mark.asyncio
async def test_a_chat_never_reset_has_no_boundary(tmp_path):
    svc, cid = group_service(tmp_path, records(3))

    result = await svc.get_conversation_history(cid)

    assert result["live_history_boundary"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cid", MONITOR_KINDS)
async def test_a_chat_served_from_its_monitor_pages_the_same_way(cid):
    history = records(120)
    svc, cid = monitor_service(cid, history, boundary=BOUNDARY)

    first = await svc.get_conversation_history(cid)
    pages = await walk(svc, cid, limit=25)

    assert len(first["messages"]) == history_page.DEFAULT_LIMIT
    assert first["has_older"] is True
    assert first["message_count"] == 120
    assert first["live_history_boundary"] == BOUNDARY
    assert first["history_tokens"] == sum(len(m["content"]) for m in history) // 4
    assert ids_of(pages) == [m["id"] for m in history]
    bad = await svc.get_conversation_history(cid, before_message_id="nope")
    assert bad["status"] == "error" and bad["messages"] == []


@pytest.mark.asyncio
async def test_a_group_with_no_history_file_falls_back_to_its_monitor_and_pages(tmp_path):
    conv_dir = tmp_path / "group-empty"
    conv_dir.mkdir()
    svc = make_service()
    svc.group_manager = FakeGroupManager(conv_dir)
    svc.conversation_monitors["group-empty"] = FakeMonitor(records(30))

    pages = await walk(svc, "group-empty", limit=10)

    assert len(pages) == 3
    assert ids_of(pages) == [f"m{i}" for i in range(30)]


@pytest.mark.asyncio
async def test_a_chat_with_no_history_at_all(tmp_path):
    svc = make_service()
    cid = "dpc-node-" + "d" * 32

    empty = await svc.get_conversation_history(cid)
    paged = await svc.get_conversation_history(cid, before_message_id="m1")

    assert empty["status"] == "success" and empty["messages"] == [] and empty["has_older"] is False
    assert paged["status"] == "error"


@pytest.mark.asyncio
async def test_a_real_peer_monitor_pages_and_names_its_boundary(tmp_path, monkeypatch):
    from dpc_client_core.conversation_monitor import ConversationMonitor

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: False))
    cid = "dpc-node-" + "e" * 32
    monitor = ConversationMonitor(
        conversation_id=cid,
        participants=[{"node_id": "dpc-node-" + "a" * 32, "name": "Mike", "context": "local"}],
        llm_manager=None,
    )
    for m in records(12):
        monitor.add_message(m["role"], m["content"], message_id=m["id"], timestamp=m["timestamp"])
    settings = monitor._get_settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"live_history_boundary": BOUNDARY}), encoding="utf-8")
    svc = make_service()
    svc.conversation_monitors[cid] = monitor

    pages = await walk(svc, cid, limit=5)

    assert ids_of(pages) == [f"m{i}" for i in range(12)]
    assert pages[0]["live_history_boundary"] == BOUNDARY
    assert str(settings).startswith(str(tmp_path))
