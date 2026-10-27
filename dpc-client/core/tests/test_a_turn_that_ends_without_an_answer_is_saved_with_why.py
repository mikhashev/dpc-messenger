"""A turn that ends without an answer is saved, marked with why (Mike's call, 2026-10-27).

Until then `agent_manager` kept an assistant record out of history.json whenever
the response opened with a warning sign or the host refused: stops, kills,
provider errors — and a real answer that opens "⚠️ Not done" — left the
person's message standing with nothing after it, and the next prompt carried
two user turns in a row.

Now the loop names the reason in the branch where it exits, into
`trace["turn_end"]`, and the record carries it: `stopped`, `killed`, `failed`,
`refused` or `guard`. A record without the field is an answer, and its bytes —
keys, chain hash, content hash — are what they were before the field existed.
The field stays out of chain_hash and out of the signed preimage. Marked
records stay out of the knowledge buffers.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_protocol.message_signing import message_content_hash
from dpc_protocol.protocol import PeerRefused
from dpc_client_core.conversation_monitor import (
    TURN_END_VALUES,
    ConversationMonitor,
    chain_hash_for,
)
from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.guards import ContextLimitGuard, ResearchLimitGuard
from dpc_client_core.dpc_agent.hooks import HookRegistry
from dpc_client_core.dpc_agent.loop import _finalize_after_guard_stop, run_llm_loop
from dpc_client_core.managers import agent_manager as am

CID = "agent_001"
NOT_DONE = (
    "⚠️ Not done: the agent wrote its last action as text instead of running it.\n\n"
    "Here is what I found so far."
)

# The keys an answer's record had before `turn_end` existed, signed.
ANSWER_KEYS = {
    "id", "role", "content", "timestamp", "sender_node_id", "sender_name",
    "thinking", "streaming_raw", "tool_calls", "msg_index", "chain_hash",
    "content_hash", "tool_calls_digest", "signature", "signer_node_id",
    "preimage_version",
}


# --- the loop names the reason where it exits ---------------------------------------


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return []

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        return "ok"


class _Llm:
    """chat() answering from a script: a reply tuple, or an exception to raise."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.started = asyncio.Event()

    async def chat(self, messages, **kwargs):
        self.started.set()
        step = self.steps.pop(0)
        if step == "hang":
            await asyncio.sleep(60)
        if isinstance(step, BaseException):
            raise step
        return step


def _usage(prompt=1, completion=1):
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion, "cost": 0.0}


@pytest.fixture(autouse=True)
def _no_agent_config(monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})


async def _loop(llm, tmp_path, stop=None, kill=None):
    return await run_llm_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=_Tools(), llm=llm, agent_root=tmp_path,
        emit_progress=lambda *a, **k: None,
        task_id="t1", stop_event=stop or asyncio.Event(), kill_event=kill or asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_an_answer_carries_no_turn_end_even_when_it_opens_with_a_warning(tmp_path):
    text, _u, trace = await _loop(
        _Llm(({"role": "assistant", "content": NOT_DONE, "tool_calls": []}, _usage())), tmp_path)

    assert text.startswith("⚠️ Not done")
    assert "turn_end" not in trace


@pytest.mark.asyncio
async def test_a_stop_between_rounds_is_stopped(tmp_path):
    stop = asyncio.Event()
    stop.set()
    text, _u, trace = await _loop(_Llm(), tmp_path, stop=stop)

    assert text.startswith("⚠️ Stopped by user")
    assert trace["turn_end"] == "stopped"


@pytest.mark.asyncio
async def test_a_second_press_outranks_the_first_between_rounds(tmp_path):
    stop, kill = asyncio.Event(), asyncio.Event()
    stop.set()
    kill.set()
    _text, _u, trace = await _loop(_Llm(), tmp_path, stop=stop, kill=kill)

    assert trace["stopped_by_user"] is True
    assert trace["turn_end"] == "killed"


@pytest.mark.asyncio
async def test_a_kill_mid_call_is_killed(tmp_path):
    llm = _Llm("hang")
    stop, kill = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(_loop(llm, tmp_path, stop=stop, kill=kill))
    await asyncio.wait_for(llm.started.wait(), 2)
    stop.set()
    kill.set()
    text, _u, trace = await asyncio.wait_for(task, 5)

    assert "killed mid-call" in text
    assert trace["stopped_by_user"] is True and trace["killed_mid_call"] is True
    assert trace["turn_end"] == "killed"


@pytest.mark.asyncio
async def test_a_hosts_refusal_is_refused(tmp_path):
    text, _u, trace = await _loop(_Llm(PeerRefused("busy", code="provider_unavailable")), tmp_path)

    assert text.startswith("No answer this time")
    assert trace["turn_end"] == "refused"


@pytest.mark.asyncio
@pytest.mark.parametrize("step, opening", [
    (RuntimeError("Peer dpc-node-x is not connected"), "⚠️ LLM error"),
    # A None reply raises inside the call's own try, so it exits here too; the
    # "No response from LLM" branch behind it is marked but not reachable.
    ((None, {}), "⚠️ LLM error"),
    (({"role": "assistant", "content": "", "tool_calls": []}, _usage(0, 0)),
     "⚠️ The provider dropped the request"),
    # A call shaped so the loop's own code raises past the model call.
    (({"role": "assistant", "content": "", "tool_calls": [{"no": "function"}]}, _usage()),
     "⚠️ Loop error"),
], ids=["llm-error", "none-reply", "provider-dropped", "loop-error"])
async def test_every_error_exit_is_failed(tmp_path, step, opening):
    text, _u, trace = await _loop(_Llm(step), tmp_path)

    assert text.startswith(opening)
    assert trace["turn_end"] == "failed"


def _stopped_by(guard) -> HookRegistry:
    hooks = HookRegistry()
    hooks.register(guard)
    hooks.last_triggered = guard
    return hooks


class _FinalLlm:
    def __init__(self, content="final answer", fail=False):
        self.content, self.fail = content, fail

    async def chat(self, messages, **kwargs):
        if self.fail:
            raise RuntimeError("down")
        return {"role": "assistant", "content": self.content}, _usage()


@pytest.mark.asyncio
async def test_a_guard_stop_is_guard_whichever_way_it_ends():
    at_edge = ContextLimitGuard()
    at_edge._seen_ratio = 1.02
    text, _u, trace = await _finalize_after_guard_stop(
        _stopped_by(at_edge), [], _FinalLlm(), None, "c", {}, {},
        fallback_reason="stopped", stop_event=asyncio.Event())
    assert text.startswith("[CONTEXT_LIMIT]") and trace["turn_end"] == "guard"

    text, _u, trace = await _finalize_after_guard_stop(
        _stopped_by(ResearchLimitGuard()), [], _FinalLlm(), None, "c", {}, {},
        fallback_reason="stopped", stop_event=asyncio.Event())
    assert text == "final answer" and trace["turn_end"] == "guard"

    text, _u, trace = await _finalize_after_guard_stop(
        _stopped_by(ResearchLimitGuard()), [], _FinalLlm(fail=True), None, "c", {}, {},
        fallback_reason="⚠️ Agent loop stopped by guard.", stop_event=asyncio.Event())
    assert "[RESEARCH_LIMIT]" in text and trace["turn_end"] == "guard"


@pytest.mark.asyncio
async def test_a_stop_pressed_during_a_guard_stop_is_the_users():
    stop, kill = asyncio.Event(), asyncio.Event()
    stop.set()
    _t, _u, trace = await _finalize_after_guard_stop(
        _stopped_by(ResearchLimitGuard()), [], _FinalLlm(), None, "c", {}, {},
        fallback_reason="stopped", stop_event=stop, kill_event=kill)
    assert trace["turn_end"] == "stopped"

    kill.set()
    _t, _u, trace = await _finalize_after_guard_stop(
        _stopped_by(ResearchLimitGuard()), [], _FinalLlm(), None, "c", {}, {},
        fallback_reason="stopped", stop_event=stop, kill_event=kill)
    assert trace["turn_end"] == "killed"


@pytest.mark.asyncio
async def test_a_full_context_refusal_is_a_guard_and_drops_the_previous_turns_state(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(agent_mod, "build_llm_messages",
                        lambda **kw: ([], {"estimated_tokens_before": 10}))
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = tmp_path
    agent.memory = agent.skill_store = agent._firewall = agent._embedding_provider = None
    agent._get_allowed_tools = lambda **kw: set()
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    agent._last_usage = {"thinking": "last turn's thinking"}
    agent._last_trace = {"accumulated_tool_calls": [{"tool": "read_file"}]}

    text = await agent.process(
        "hi", CID, session_state={"tokens_limit": 1000, "tokens_after_last_response": 999})

    assert text.startswith("⚠️ Context window full")
    assert agent._last_trace == {"turn_end": "guard"}
    assert agent._last_usage == {}


# --- what lands in history.json --------------------------------------------------------


@pytest.fixture(autouse=True)
def _always_persist(monkeypatch):
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))


def _monitor(root):
    m = ConversationMonitor(
        conversation_id=CID,
        participants=[{"node_id": "dpc-node-me", "name": "User", "context": "local"},
                      {"node_id": CID, "name": "Ark", "context": "agent"}],
        llm_manager=None,
    )
    m._get_history_path = lambda: root / CID / "history.json"
    return m


class _FakeAgent:
    """Ends its turn as the loop does: text returned, the reason on the trace."""

    def __init__(self, text, trace):
        self.text, self.trace = text, trace
        self._last_usage, self._last_trace = {}, {}
        self.calls = 0

    async def process(self, **kwargs):
        self.calls += 1
        await kwargs["on_stream_chunk"]("partial streamed text", kwargs["conversation_id"])
        self._last_usage = {"thinking": "the model's reasoning"}
        self._last_trace = {
            **self.trace,
            "accumulated_tool_calls": [{"tool": "read_file", "input": "{}", "output": "x",
                                        "is_error": False, "duration_ms": 1, "round": 1}],
        }
        return self.text


def _manager(agent, monitor, config=None):
    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager.agent_id = CID
    manager.firewall = MagicMock()
    manager.config = config or {}
    manager._agent = agent
    manager._agents = {}
    manager._memory_indexes_initialized = True
    manager._agent_display_name = "Ark"
    manager._interrupt_events = {}
    manager._kill_events = {}
    manager._daily_tokens_used = 0
    manager._daily_tokens_date = ""
    manager.service = SimpleNamespace(
        p2p_manager=SimpleNamespace(node_id="dpc-node-me"),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
    )
    manager.ensure_started = AsyncMock(return_value=manager)
    manager._get_or_create_agent_monitor = lambda conv: monitor
    manager.get_session_state = lambda conv: {}
    manager._resolve_reasoning_effort = lambda conv, effort: (None, "none")
    manager._resolve_context_window = lambda: None
    return manager


def _on_disk(tmp_path):
    return json.loads((tmp_path / CID / "history.json").read_text(encoding="utf-8"))["messages"]


def _recomputed_content_hash(rec):
    """The preimage as add_message computed it before turn_end existed."""
    return message_content_hash(
        conversation_id=CID, message_id=rec["id"], sender_node_id=rec.get("sender_node_id"),
        sender_name=rec.get("sender_name"), sender_type=None, agent_owner=None,
        timestamp=rec["timestamp"], content=rec["content"], tool_calls=rec.get("tool_calls"),
        mention_depth=None,
    )


CASES = [
    ("⚠️ Stopped by user after 2 rounds.", {"stopped_by_user": True, "turn_end": "stopped"}, "stopped"),
    ("⚠️ Stopped by user: killed mid-call in round 1 (the model's answer was not waited for).",
     {"stopped_by_user": True, "killed_mid_call": True, "turn_end": "killed"}, "killed"),
    ("⚠️ LLM error: Peer dpc-node-x is not connected", {"turn_end": "failed"}, "failed"),
    ("No answer this time: Linus did not take the request (busy).",
     {"peer_refused": {"code": "provider_unavailable"}, "turn_end": "refused"}, "refused"),
    ("[ROUND_LIMIT] Task exceeded MAX_ROUNDS (200).", {"turn_end": "guard"}, "guard"),
    (NOT_DONE, {}, None),
    ("A normal answer.", {}, None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("text, trace, expected", CASES,
                         ids=["stopped", "killed", "failed", "refused", "guard", "not-done", "answer"])
async def test_the_turn_is_saved_and_says_why(tmp_path, signing_identity, text, trace, expected):
    monitor = _monitor(tmp_path)
    manager = _manager(_FakeAgent(text, trace), monitor)

    assert await manager._process_message_guarded("hello", CID) == text

    user, rec = _on_disk(tmp_path)
    assert user["role"] == "user" and user["content"] == "hello"
    assert rec["role"] == "assistant" and rec["content"] == text
    assert rec["thinking"] == "the model's reasoning"
    assert rec["streaming_raw"] == "partial streamed text"
    assert rec["tool_calls"][0]["tool"] == "read_file"
    if expected is None:
        assert "turn_end" not in rec
        assert set(rec) == ANSWER_KEYS
    else:
        assert rec["turn_end"] == expected
        assert set(rec) == ANSWER_KEYS | {"turn_end"}
    # Outside the preimage and the chain, marked or not.
    assert rec["content_hash"] == _recomputed_content_hash(rec)
    assert rec["chain_hash"] == chain_hash_for(rec, user["chain_hash"])
    assert rec["chain_hash"] == chain_hash_for(
        {k: v for k, v in rec.items() if k != "turn_end"}, user["chain_hash"])


@pytest.mark.asyncio
async def test_a_marked_record_reloads_with_its_chain_intact(tmp_path, signing_identity, caplog):
    monitor = _monitor(tmp_path)
    await _manager(_FakeAgent("⚠️ LLM error: boom", {"turn_end": "failed"}), monitor
                   )._process_message_guarded("hello", CID)

    fresh = _monitor(tmp_path)
    assert fresh.load_history() is True
    assert fresh.message_history[-1]["turn_end"] == "failed"
    assert not [r for r in caplog.records if "Chain broken" in r.getMessage()]


@pytest.mark.asyncio
async def test_an_exceeded_quota_is_a_guard_record_and_runs_nothing(tmp_path, signing_identity):
    monitor = _monitor(tmp_path)
    agent = _FakeAgent("unused", {})
    agent._last_usage = {"thinking": "previous turn"}
    agent._last_trace = {"accumulated_tool_calls": [{"tool": "old"}]}
    manager = _manager(agent, monitor, config={"quota_tokens_per_day": 10})
    manager._daily_tokens_used = 10
    manager._daily_tokens_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    text = await manager._process_message_guarded("hello", CID)

    assert text.startswith("⚠️ Agent quota exceeded") and agent.calls == 0
    user, rec = _on_disk(tmp_path)
    assert rec["turn_end"] == "guard" and rec["content"] == text
    assert "thinking" not in rec and "tool_calls" not in rec
    assert agent._last_trace == {"turn_end": "guard"} and agent._last_usage == {}


def test_a_value_outside_the_closed_set_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _monitor(tmp_path).add_message(role="assistant", content="x", turn_end="cancelled")
    assert TURN_END_VALUES == {"stopped", "killed", "failed", "refused", "guard"}


# --- knowledge sees only the conversation ---------------------------------------------


@pytest.mark.asyncio
async def test_a_marked_record_stays_out_of_the_knowledge_buffers(tmp_path):
    monitor = _monitor(tmp_path)
    await _manager(_FakeAgent("⚠️ Stopped by user after 1 rounds.", {"turn_end": "stopped"}),
                   monitor)._process_message_guarded("first question", CID)
    await _manager(_FakeAgent("The answer.", {}), monitor
                   )._process_message_guarded("second question", CID)

    texts = [m.text for m in monitor.full_conversation]
    assert texts == ["first question", "second question", "The answer."]
    assert [m.text for m in monitor.message_buffer] == texts

    fresh = _monitor(tmp_path)
    fresh.load_history()
    assert [m.text for m in fresh.full_conversation] == texts
    assert len(fresh.message_history) == 4
