"""Sleep must not start beside a turn of the same agent, in any chat.

Seen live on 2026-10-27 (dpc-client.log, 12:51-12:55): Ark's 1:1 chat was reset
at 12:52:02 and sleep started at 12:52:03, while Ark was mid-turn in the group
"Hardware". `toggle_sleep` looked only at the agent's own 1:1 history, so the
sleep pipeline ran in parallel with the turn on one provider, consolidating
memory the turn was reading.

The run gate (`dpc_agent/run_gate.py`) already knows the agent's turn and the
chats waiting for it. Sleep reads that snapshot and refuses with the existing
error shape, naming the chat. Owner's choice, 2026-10-27.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent import run_gate as run_gate_module
from dpc_client_core.dpc_agent import sleep_pipeline
from dpc_client_core.dpc_agent.run_gate import get_run_gate
from dpc_client_core.service import CoreService

AGENT = "agent_sleepgate_test"


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _make_service(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".dpc" / "conversations" / AGENT).mkdir(parents=True)
    agent_dir = tmp_path / ".dpc" / "agents" / AGENT
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.json").write_text(json.dumps({"name": "Ark"}), encoding="utf-8")

    svc = CoreService.__new__(CoreService)
    svc.local_api = _Api()
    svc.llm_manager = None
    run_sleep = AsyncMock(return_value={"status": "no_new_sessions"})
    monkeypatch.setattr(sleep_pipeline, "run_sleep", run_sleep)
    return svc, run_sleep


async def _let_background_finish():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_running_turn_in_another_chat_refuses_sleep_and_names_the_chat(tmp_path, monkeypatch):
    svc, run_sleep = _make_service(tmp_path, monkeypatch)

    async with get_run_gate(AGENT).hold(
        {"conversation_id": "group-hardware", "conversation_title": "Hardware"}
    ):
        result = await svc.toggle_sleep(AGENT)

    await _let_background_finish()
    assert result["status"] == "error"
    assert result["message"] == "Ark is busy in Hardware — start sleep after the turn ends"
    run_sleep.assert_not_called()
    assert svc.local_api.events == [], "no sleeping state may be announced for a refused sleep"


@pytest.mark.asyncio
async def test_a_turn_without_a_title_is_named_by_its_conversation_id(tmp_path, monkeypatch):
    svc, run_sleep = _make_service(tmp_path, monkeypatch)

    async with get_run_gate(AGENT).hold({"conversation_id": "group-x", "conversation_title": ""}):
        result = await svc.toggle_sleep(AGENT)

    assert result["status"] == "error"
    assert "busy in group-x " in result["message"]
    run_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_a_chat_waiting_at_the_gate_refuses_sleep_too(tmp_path, monkeypatch):
    svc, run_sleep = _make_service(tmp_path, monkeypatch)
    waiting = {
        "conversation_id": "group-later", "conversation_title": "Later chat",
        "waiting_since": "2026-10-27T12:00:00Z", "wait_reason": "other_chat", "held_by": None,
    }
    monkeypatch.setattr(
        run_gate_module, "agent_turn_snapshot",
        lambda agent_id: {"agent_id": agent_id, "turn": None, "waiting": [waiting], "ts": ""},
    )

    result = await svc.toggle_sleep(AGENT)

    assert result == {"status": "error",
                      "message": "Ark is busy in Later chat — start sleep after the turn ends"}
    run_sleep.assert_not_called()


@pytest.mark.asyncio
async def test_an_idle_agent_starts_sleep_as_before(tmp_path, monkeypatch):
    svc, run_sleep = _make_service(tmp_path, monkeypatch)

    result = await svc.toggle_sleep(AGENT)
    await _let_background_finish()

    assert result == {"status": "sleeping", "message": "Sleep pipeline started in background"}
    run_sleep.assert_awaited_once()
    assert run_sleep.await_args.args[0] == tmp_path / ".dpc" / "conversations" / AGENT
    assert [name for name, _ in svc.local_api.events][:1] == ["sleep_state_changed"]


@pytest.mark.asyncio
async def test_sleep_starts_once_the_turn_has_ended(tmp_path, monkeypatch):
    svc, run_sleep = _make_service(tmp_path, monkeypatch)

    async with get_run_gate(AGENT).hold({"conversation_id": "group-hardware",
                                         "conversation_title": "Hardware"}):
        refused = await svc.toggle_sleep(AGENT)
    started = await svc.toggle_sleep(AGENT)
    await _let_background_finish()

    assert refused["status"] == "error"
    assert started["status"] == "sleeping"
    run_sleep.assert_awaited_once()
