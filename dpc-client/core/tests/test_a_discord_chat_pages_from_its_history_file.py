"""A Discord per-user conversation is paged like every other chat kind.

`discord-user-<id>` conversations (coordinators/discord_coordinator.py) are
written by the agent's own monitor to `~/.dpc/conversations/<id>/history.json`,
and `get_conversation_history` had no branch for them: with no service monitor
the answer was an empty history, and any cursor an error. They are now read from
that file as agent chats are, with the same paging contract (limit,
before_message_id, has_older, live_history_boundary).
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dpc_client_core import history_page
from dpc_client_core import service as service_module
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.service import CoreService

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
BOUNDARY = "2026-09-30T12:00:00+00:00"
CID = "discord-user-42"


def records(n):
    return [
        {"id": f"m{i}", "role": "user" if i % 2 else "assistant",
         "content": f"hello {i}", "timestamp": (T0 + timedelta(minutes=i)).isoformat(),
         "sender_name": "Ann (Discord)"}
        for i in range(n)
    ]


def make_service():
    svc = CoreService.__new__(CoreService)
    svc.conversation_monitors = {}
    svc.llm_manager = None
    svc._voice_transcriptions = {}
    return svc


def discord_service(tmp_path, monkeypatch, history, boundary=None):
    conv_dir = tmp_path / "conversations" / CID
    conv_dir.mkdir(parents=True)
    (conv_dir / "history.json").write_text(
        json.dumps({"messages": history, "token_stats": {"current_token_count": 42}}), encoding="utf-8")
    if boundary:
        (conv_dir / "settings.json").write_text(
            json.dumps({"live_history_boundary": boundary}), encoding="utf-8")
    monkeypatch.setattr(service_module, "DPC_HOME_DIR", tmp_path)
    return make_service()


@pytest.mark.asyncio
async def test_the_first_answer_is_the_newest_page(tmp_path, monkeypatch):
    svc = discord_service(tmp_path, monkeypatch, records(130), boundary=BOUNDARY)

    result = await svc.get_conversation_history(CID)

    assert result["status"] == "success"
    assert [m["message_id"] for m in result["messages"]] == [f"m{i}" for i in range(30, 130)]
    assert result["has_older"] is True
    assert result["message_count"] == 130
    assert result["tokens_used"] == 42
    assert result["live_history_boundary"] == BOUNDARY


@pytest.mark.asyncio
async def test_an_older_page_by_cursor_and_nothing_older_at_the_start(tmp_path, monkeypatch):
    svc = discord_service(tmp_path, monkeypatch, records(130))

    first = await svc.get_conversation_history(CID, limit=100)
    older = await svc.get_conversation_history(
        CID, before_message_id=first["messages"][0]["message_id"], limit=100)

    assert [m["message_id"] for m in older["messages"]] == [f"m{i}" for i in range(30)]
    assert older["has_older"] is False
    assert older["live_history_boundary"] is None


@pytest.mark.asyncio
async def test_an_unknown_cursor_is_an_error(tmp_path, monkeypatch):
    svc = discord_service(tmp_path, monkeypatch, records(5))

    result = await svc.get_conversation_history(CID, before_message_id="gone-after-a-ttl-reset")

    assert result["status"] == "error"
    assert result["messages"] == [] and result["has_older"] is False


@pytest.mark.asyncio
async def test_the_branch_reads_the_file_the_monitor_writes(tmp_path, monkeypatch, signing_identity):
    """No hand-made file: a ConversationMonitor for the id saves, the branch reads."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(service_module, "DPC_HOME_DIR", tmp_path / ".dpc")
    monitor = ConversationMonitor(
        conversation_id=CID,
        participants=[{"node_id": "n1", "name": "User", "context": "local"}],
        llm_manager=None,
    )
    for m in records(7):
        monitor.add_message(m["role"], m["content"], message_id=m["id"], timestamp=m["timestamp"])
    monitor.save_history()
    assert str(monitor._get_history_path()).startswith(str(tmp_path))
    svc = make_service()  # no service monitor: the agent's own one wrote the file

    result = await svc.get_conversation_history(CID, limit=history_page.MAX_LIMIT)

    assert [m["message_id"] for m in result["messages"]] == [f"m{i}" for i in range(7)]
    assert result["has_older"] is False
