"""A quota refusal ends its turn the way any other turn ends.

The daily quota check sits after the "thinking" progress line went up and used
to return straight away: the line was never cleared and no TASK_COMPLETED was
emitted, so the chat showed the agent thinking about a turn that had already
been refused. It now goes through the same completion as an answered turn,
`_complete_turn`, and keeps its `guard` record.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.dpc_agent.events import EventType
from dpc_client_core.managers import agent_manager as am

CID = "agent_001"


class _Agent:
    def __init__(self):
        self._last_usage, self._last_trace = {}, {}
        self.calls = 0

    async def process(self, **kwargs):
        self.calls += 1
        return "an answer"


def _setup(tmp_path, monkeypatch, quota_used):
    monitor = ConversationMonitor(
        conversation_id=CID,
        participants=[{"node_id": "dpc-node-me", "name": "User", "context": "local"},
                      {"node_id": CID, "name": "Ark", "context": "agent"}],
        llm_manager=None,
    )
    monitor._get_history_path = lambda: tmp_path / CID / "history.json"
    emitter = SimpleNamespace(emit=AsyncMock())
    monkeypatch.setattr(am, "get_event_emitter", lambda: emitter)

    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager.agent_id = CID
    manager.firewall = MagicMock()
    manager.config = {"quota_tokens_per_day": 10}
    manager._agent = _Agent()
    manager._agents = {}
    manager._memory_indexes_initialized = True
    manager._agent_display_name = "Ark"
    manager._interrupt_events = {}
    manager._kill_events = {}
    manager._daily_tokens_used = quota_used
    manager._daily_tokens_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manager.service = SimpleNamespace(
        p2p_manager=SimpleNamespace(node_id="dpc-node-me"),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
    )
    manager.ensure_started = AsyncMock(return_value=manager)
    manager._get_or_create_agent_monitor = lambda conv: monitor
    manager.get_session_state = lambda conv: {}
    manager._resolve_reasoning_effort = lambda conv, effort: (None, "none")
    manager._resolve_context_window = lambda: None
    return manager, emitter


def _ui_events(manager):
    return [c.args[0] for c in manager.service.local_api.broadcast_event.call_args_list]


def _task_events(emitter):
    return [c.args[0] for c in emitter.emit.call_args_list]


@pytest.mark.asyncio
async def test_a_refused_turn_clears_the_thinking_line_and_completes_its_task(
    tmp_path, signing_identity, monkeypatch,
):
    manager, emitter = _setup(tmp_path, monkeypatch, quota_used=10)

    text = await manager._process_message_guarded("hello", CID)
    await asyncio.sleep(0)  # the progress events are sent from tasks

    assert text.startswith("⚠️ Agent quota exceeded") and manager._agent.calls == 0
    assert _ui_events(manager) == ["agent_progress", "agent_progress_clear"]
    assert _task_events(emitter) == [EventType.TASK_STARTED, EventType.TASK_COMPLETED]
    completed = emitter.emit.call_args_list[-1].args[1]
    assert completed["conversation_id"] == CID and completed["result"] == text
    rec = json.loads((tmp_path / CID / "history.json").read_text(encoding="utf-8"))["messages"][-1]
    assert rec["turn_end"] == "guard" and rec["content"] == text


@pytest.mark.asyncio
async def test_an_answered_turn_ends_with_the_same_two_events(
    tmp_path, signing_identity, monkeypatch,
):
    manager, emitter = _setup(tmp_path, monkeypatch, quota_used=0)

    assert await manager._process_message_guarded("hello", CID) == "an answer"
    await asyncio.sleep(0)

    assert _ui_events(manager)[0] == "agent_progress"
    assert _ui_events(manager)[-1] == "agent_progress_clear"
    assert _task_events(emitter) == [EventType.TASK_STARTED, EventType.TASK_COMPLETED]
