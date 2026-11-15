"""Extract Knowledge can be cancelled while the model is thinking, before the vote.

The extraction runs as a task held per conversation. `cancel_knowledge_extraction`
cancels it; the pressed command then answers `status: "cancelled"`, the UI hears
`knowledge_extraction_cancelled`, and nothing is proposed. The pin, the consent
state and the monitor's `_extracting` are released, so the next press works.
Once the proposal has reached a consensus session there is nothing to cancel:
the command refuses (`vote_in_progress`, or `extraction_not_running` when nothing
runs). Mike's call, 2026-11-15, in his Claude Code session.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core import knowledge_routing
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.knowledge_service import KnowledgeService
from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.service import CoreService

CHAT = "ai_chat_cancel"
ME = "dpc-node-" + "a" * 32


def _llm():
    provider = SimpleNamespace(config={"type": "deepseek"}, model="v4")
    return SimpleNamespace(providers={"deepseek_flash": provider}, default_provider=None,
                           knowledge_provider=None)


def _proposal(conversation_id=CHAT):
    return SimpleNamespace(
        proposal_id="prop-1", conversation_id=conversation_id, topic="plan",
        entries=["settled"], avg_confidence=0.9, participants=[ME],
        to_dict=lambda: {"proposal_id": "prop-1", "conversation_id": conversation_id},
    )


class _Model:
    """Stands in for the extraction's model call: waits until released."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.seen_pins = []

    def install(self, monitor):
        async def _generate(**kwargs):
            self.calls += 1
            self.seen_pins.append(dict(monitor.extraction_target or {}))
            self.started.set()
            await self.release.wait()
            return _proposal(monitor.conversation_id)
        monitor._generate_commit_proposal = _generate


def _monitor(llm):
    monitor = ConversationMonitor(
        conversation_id=CHAT,
        participants=[{"node_id": ME, "name": "me", "context": ""}],
        llm_manager=llm,
        settings=SimpleNamespace(get_knowledge_cold_fallback_provider=lambda: "",
                                 get_cultural_perspectives_enabled=lambda: False),
        ai_query_func=None,
        p2p_manager=SimpleNamespace(peers={}, own_addresses=None),
    )
    monitor.message_history = [{"id": "m1", "role": "user", "content": "the plan is settled"}]
    monitor.set_inference_settings(None, "v4", "deepseek_flash")
    return monitor


def _node(tmp_path: Path, monitor, llm, sessions=None):
    """A knowledge service over a real firewall file holding a "don't ask again" for the target."""
    svc = KnowledgeService.__new__(KnowledgeService)
    svc.p2p_manager = SimpleNamespace(node_id=ME, peers={}, own_addresses=None)
    svc.group_manager = SimpleNamespace(get_group=lambda gid: None)
    svc.peer_metadata = {}
    svc.consensus_manager = SimpleNamespace(sessions=sessions if sessions is not None else {},
                                            propose_commit=AsyncMock())
    svc.llm_manager = llm
    svc._get_or_create_conversation_monitor = lambda cid: monitor
    svc.events = []

    async def _broadcast(event, payload):
        svc.events.append((event, payload))

    svc.local_api = SimpleNamespace(broadcast_event=_broadcast)

    rules = {}
    knowledge_routing.add_remembered(rules, svc._resolve_extraction_target(monitor))
    rules_path = tmp_path / "privacy_rules.json"
    rules_path.write_text(json.dumps(rules), encoding="utf-8")
    svc.firewall = ContextFirewall(rules_path)
    assert svc._remembered_targets(), "the fixture holds a remembered yes"
    return svc


def _event_names(svc):
    return [name for name, _ in svc.events]


async def _start(svc):
    task = asyncio.create_task(KnowledgeService.end_conversation_session(svc, CHAT))
    return task


@pytest.mark.asyncio
async def test_a_cancel_while_the_model_thinks_answers_cancelled_and_proposes_nothing(tmp_path):
    llm = _llm()
    monitor = _monitor(llm)
    model = _Model()
    model.install(monitor)
    svc = _node(tmp_path, monitor, llm)

    pressed = await _start(svc)
    await asyncio.wait_for(model.started.wait(), 5)
    assert monitor._extracting is True
    assert monitor.extraction_target is not None and monitor.remembered_targets, "pinned while it runs"
    assert monitor.fallbacks_need_consent is True

    cancel = await KnowledgeService.cancel_knowledge_extraction(svc, CHAT)
    result = await asyncio.wait_for(pressed, 5)

    assert cancel["status"] == "success", cancel
    assert result["status"] == "cancelled", result
    assert result["conversation_id"] == CHAT
    assert "knowledge_extraction_cancelled" in _event_names(svc)
    assert ("knowledge_extraction_cancelled", {"conversation_id": CHAT,
            "message": result["message"]}) in svc.events
    assert "knowledge_commit_proposed" not in _event_names(svc)
    assert "knowledge_extraction_failed" not in _event_names(svc), "a cancel is not a failure"
    svc.consensus_manager.propose_commit.assert_not_awaited()
    assert monitor.proposals_created == 0
    # Released: the pin, the consent state, and the flag that refuses a second press.
    assert monitor.extraction_target is None
    assert monitor.fallbacks_need_consent is False
    assert monitor.remembered_targets == []
    assert monitor._extracting is False
    assert svc._running_extractions() == {}
    # The buffers rebuilt before the model call are kept: nothing was lost.
    assert monitor.message_buffer


@pytest.mark.asyncio
async def test_the_press_after_a_cancel_runs_a_new_extraction(tmp_path):
    llm = _llm()
    monitor = _monitor(llm)
    model = _Model()
    model.install(monitor)
    svc = _node(tmp_path, monitor, llm)

    pressed = await _start(svc)
    await asyncio.wait_for(model.started.wait(), 5)
    await KnowledgeService.cancel_knowledge_extraction(svc, CHAT)
    assert (await asyncio.wait_for(pressed, 5))["status"] == "cancelled"

    model.started.clear()
    model.release.set()
    result = await asyncio.wait_for(KnowledgeService.end_conversation_session(svc, CHAT), 5)

    assert result["status"] == "success", result
    assert result["proposal_id"] == "prop-1"
    assert model.calls == 2
    assert model.seen_pins[1].get("provider_alias") == "deepseek_flash", "the new press is pinned"
    assert "knowledge_commit_proposed" in _event_names(svc)
    svc.consensus_manager.propose_commit.assert_awaited_once()
    assert monitor._extracting is False and monitor.extraction_target is None


@pytest.mark.asyncio
async def test_a_cancel_with_nothing_running_is_refused_as_extraction_not_running(tmp_path):
    llm = _llm()
    monitor = _monitor(llm)
    svc = _node(tmp_path, monitor, llm)

    result = await KnowledgeService.cancel_knowledge_extraction(svc, CHAT)

    assert result["status"] == "error"
    assert result["reason"] == "extraction_not_running"
    assert svc.events == [], "a refused cancel tells the UI nothing by event"


@pytest.mark.asyncio
async def test_a_cancel_after_the_proposal_reached_a_vote_is_refused(tmp_path):
    llm = _llm()
    monitor = _monitor(llm)
    session = SimpleNamespace(proposal=_proposal(), status="voting")
    svc = _node(tmp_path, monitor, llm, sessions={"prop-1": session})

    result = await KnowledgeService.cancel_knowledge_extraction(svc, CHAT)

    assert result["status"] == "error"
    assert result["reason"] == "vote_in_progress"
    assert result["proposal_id"] == "prop-1"
    assert svc.events == []


@pytest.mark.asyncio
async def test_a_cancel_after_the_extraction_finished_does_not_withdraw_its_proposal(tmp_path):
    llm = _llm()
    monitor = _monitor(llm)
    model = _Model()
    model.install(monitor)
    model.release.set()
    svc = _node(tmp_path, monitor, llm)

    result = await KnowledgeService.end_conversation_session(svc, CHAT)
    assert result["status"] == "success"
    svc.consensus_manager.sessions["prop-1"] = SimpleNamespace(proposal=_proposal(), status="voting")

    refused = await KnowledgeService.cancel_knowledge_extraction(svc, CHAT)

    assert refused["reason"] == "vote_in_progress"
    assert "knowledge_extraction_cancelled" not in _event_names(svc)


@pytest.mark.asyncio
async def test_a_second_press_during_a_running_extraction_still_answers_extraction_in_progress(tmp_path):
    llm = _llm()
    monitor = _monitor(llm)
    model = _Model()
    model.install(monitor)
    svc = _node(tmp_path, monitor, llm)

    pressed = await _start(svc)
    await asyncio.wait_for(model.started.wait(), 5)
    pin = monitor.extraction_target

    second = await KnowledgeService.end_conversation_session(svc, CHAT)

    assert second["status"] == "error"
    assert second["reason"] == "extraction_in_progress"
    assert monitor.extraction_target is pin, "the second press leaves the running pin alone"
    assert model.calls == 1

    await KnowledgeService.cancel_knowledge_extraction(svc, CHAT)
    assert (await asyncio.wait_for(pressed, 5))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_press_in_the_moment_before_the_first_extraction_starts_is_turned_away(tmp_path):
    """The task is registered before it first runs, so the gap holds no second extraction."""
    llm = _llm()
    monitor = _monitor(llm)
    model = _Model()
    model.install(monitor)
    svc = _node(tmp_path, monitor, llm)

    first = asyncio.create_task(KnowledgeService.end_conversation_session(svc, CHAT))
    second = asyncio.create_task(KnowledgeService.end_conversation_session(svc, CHAT))
    await asyncio.wait_for(model.started.wait(), 5)
    await asyncio.sleep(0)

    assert model.calls == 1
    done = [t for t in (first, second) if t.done()]
    assert len(done) == 1 and done[0].result()["reason"] == "extraction_in_progress"

    await KnowledgeService.cancel_knowledge_extraction(svc, CHAT)
    running = first if second in done else second
    assert (await asyncio.wait_for(running, 5))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_shutdown_cancelling_the_press_itself_still_propagates(tmp_path):
    """Only the cancel command's cancellation becomes an answer; the outer's own is re-raised."""
    llm = _llm()
    monitor = _monitor(llm)
    model = _Model()
    model.install(monitor)
    svc = _node(tmp_path, monitor, llm)

    pressed = await _start(svc)
    await asyncio.wait_for(model.started.wait(), 5)
    pressed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pressed

    assert monitor._extracting is False
    assert monitor.extraction_target is None
    assert svc._running_extractions() == {}
    assert "knowledge_extraction_cancelled" not in _event_names(svc)


def test_the_command_is_allowed_and_runs_off_the_read_loop():
    assert "cancel_knowledge_extraction" in ALLOWED_COMMANDS
    handler = CoreService.cancel_knowledge_extraction
    assert asyncio.iscoroutinefunction(handler)
    assert getattr(handler, "dpc_slow", False), "its wait for the press to settle must not hold the socket"


# --- the auto-detect path is held by the same flag ----------------------------


@pytest.mark.asyncio
async def test_auto_detect_does_not_start_an_extraction_while_one_runs():
    llm = _llm()
    monitor = _monitor(llm)
    monitor.auto_detect = True
    monitor._calculate_knowledge_score = AsyncMock(return_value=1.0)
    monitor._detect_consensus = lambda: True
    monitor._generate_commit_proposal = AsyncMock(return_value=_proposal())
    monitor._extracting = True
    monitor.message_buffer = [object()] * 4

    msg = SimpleNamespace(message_id="m2", conversation_id=CHAT, sender_node_id=ME,
                          sender_name="me", text="and agreed", timestamp=None)
    result = await monitor.on_message(msg)

    assert result is None
    monitor._generate_commit_proposal.assert_not_awaited()
    assert monitor._extracting is True, "the running extraction's flag is left alone"

    monitor._extracting = False
    monitor.message_buffer = [object()] * 4
    result = await monitor.on_message(msg)
    assert result is not None
    monitor._generate_commit_proposal.assert_awaited_once()
    assert monitor._extracting is False
