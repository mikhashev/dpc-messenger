"""The agent_history_updated event from the main Telegram bot says what it carries.

After a Telegram message is answered by a linked agent, the coordinator
broadcasts `agent_history_updated` built from `get_conversation_history`. Since
paging (46d0fa29) that answer holds the newest page, not the whole history, and
the event's `message_count` had become the page length. It is the whole live
history's count again, the meaning it has in `get_conversation_history`'s own
answer; `messages` stays the page.

The history is a real agent history.json under tmp_path, read by the real
`get_conversation_history`; never ~/.dpc.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core import service as service_module
from dpc_client_core.coordinators.telegram_coordinator import TelegramBridge
from dpc_client_core.service import CoreService

AGENT = "agent_001"
TG_CHAT = "424242"
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
TOTAL = 250


def _records(n):
    return [
        {"id": f"m{i}", "role": "user" if i % 2 else "assistant",
         "content": f"line {i}", "timestamp": (T0 + timedelta(minutes=i)).isoformat()}
        for i in range(n)
    ]


class _Recorder:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _service(tmp_path, monkeypatch):
    conv_dir = tmp_path / "conversations" / AGENT
    conv_dir.mkdir(parents=True)
    (conv_dir / "history.json").write_text(
        json.dumps({"messages": _records(TOTAL), "token_stats": {}}), encoding="utf-8")
    monkeypatch.setattr(service_module, "DPC_HOME_DIR", tmp_path)

    svc = CoreService.__new__(CoreService)
    svc.conversation_monitors = {f"agent-{AGENT}": SimpleNamespace(on_message=AsyncMock())}
    svc._resolve_agent_token_limit = lambda _id: 0
    svc.local_api = _Recorder()
    svc.settings = MagicMock()
    svc.reset_cc_agent_chain = lambda _agent_id: None

    manager = SimpleNamespace(
        process_message=AsyncMock(return_value="done"),
        _agent_monitors={AGENT: SimpleNamespace(current_token_count=5, token_limit=1000)},
    )
    provider = SimpleNamespace(_ensure_manager=AsyncMock(), get_manager=lambda _id: manager)
    svc.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    return svc


def _coordinator(svc):
    telegram = MagicMock()
    telegram.is_allowed.return_value = True
    telegram.bridge_to_p2p = False
    telegram.send_message = AsyncMock()
    coordinator = TelegramBridge.__new__(TelegramBridge)
    coordinator.service = svc
    coordinator.telegram = telegram
    coordinator._get_or_create_conversation_id = lambda chat_id, agent_id=None: f"agent-{AGENT}"
    coordinator._load_agent_context = lambda agent_id: {}
    return coordinator


def _update():
    update = MagicMock()
    update.message.chat_id = TG_CHAT
    update.message.text = "hello from the phone"
    update.message.message_id = 7
    update.message.from_user.full_name = "Mike"
    update.message.date = datetime(2026, 10, 29, 12, 0, 0)
    return update


@pytest.mark.asyncio
async def test_the_event_counts_the_whole_live_history_and_carries_the_newest_page(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    answer = await svc.get_conversation_history(AGENT)
    assert answer["message_count"] == TOTAL and len(answer["messages"]) < TOTAL

    await _coordinator(svc).handle_text_message(_update(), None)

    updates = [p for name, p in svc.local_api.events if name == "agent_history_updated"]
    assert len(updates) == 1
    event = updates[0]
    assert event["conversation_id"] == AGENT
    assert event["message_count"] == TOTAL == answer["message_count"]
    # The page, newest last, exactly as get_conversation_history answers it.
    assert [m["message_id"] for m in event["messages"]] == [m["message_id"] for m in answer["messages"]]
    assert event["messages"][-1]["message_id"] == f"m{TOTAL - 1}"
    assert len(event["messages"]) == len(answer["messages"]) <= 100


@pytest.mark.asyncio
async def test_a_history_answer_without_a_count_falls_back_to_the_page_length(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    svc.get_conversation_history = AsyncMock(return_value={"messages": [{"id": "a"}, {"id": "b"}]})

    await _coordinator(svc).handle_text_message(_update(), None)

    (event,) = [p for name, p in svc.local_api.events if name == "agent_history_updated"]
    assert event["message_count"] == 2
