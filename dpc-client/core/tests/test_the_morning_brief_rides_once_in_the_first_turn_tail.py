"""THE-ONE-SHOT-MORNING-BRIEF-CHANGES-THE-SYSTEM-BLOCK-FOR-ONE-TURN.

The 1:1 agent's morning brief used to join the system block once and be marked
consumed: the system prefix changed for one turn and again on the next, two cold
prefills, and the model lost the brief after one turn. Decided 2026-11-10 (ADR-014
amendment): the brief rides once at the end of the first turn's tail after sleep
(ADR-040 F2), the system block never carries it, and with L5 closed (ADR-048) it is
neither shown nor consumed. Corrected 2026-11-11 after review: the brief is consumed when
the first request carrying it has been answered, not when the prompt is built. Option A
(Mike, 2026-11-11): the tail is recorded exactly as sent, brief included, and replayed
with that message; a later turn with L5 closed replays it without the brief.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dpc_protocol.protocol import PeerRefused

from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent import context as ctx
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.context import (
    TURN_CONTEXT_CLOSE, TURN_CONTEXT_NOTE, TURN_CONTEXT_OPEN, _mark_morning_brief_consumed,
    build_llm_messages, history_prefix,
)
from dpc_client_core.dpc_agent.loop import run_llm_loop
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.sent_annotations import SentAnnotationStore

AGENT = "agent_001"
READER = {"agent_id": AGENT, "display_name": "Ark", "node_id": "dpc-node-me"}
BRIEF = {
    "summary": "BRIEF-SUMMARY-MARK",
    "key_decisions": [{"decision": "BRIEF-DECISION-MARK", "session": "s1"}],
    "unresolved": [{"topic": "BRIEF-OPEN-MARK"}],
}
MARKS = ("BRIEF-SUMMARY-MARK", "BRIEF-DECISION-MARK", "BRIEF-OPEN-MARK")
HEADING = "## Morning Brief (Sleep Consolidation)"
ALL_LAYERS = frozenset({"L5", "L6", "EXT"})


@pytest.fixture()
def world(tmp_path, monkeypatch):
    # The brief is read from <home>/conversations/<agent>, two levels above the agent
    # root's folder, so the root sits at <home>/agents/<agent>.
    root = tmp_path / "agents" / AGENT
    Memory(root).ensure_files()
    brief_path = tmp_path / "conversations" / AGENT / "morning_brief.json"
    brief_path.parent.mkdir(parents=True)
    ticks = iter(f"2026-11-10T10:{m:02d}:00+00:00" for m in range(0, 60, 5))
    monkeypatch.setattr(ctx, "utc_now_iso", lambda: next(ticks))
    return root, brief_path


def _write_brief(path, consumed=False):
    path.write_text(json.dumps(dict(BRIEF, consumed=consumed)), encoding="utf-8")


def _consumed(path):
    return json.loads(path.read_text(encoding="utf-8"))["consumed"]


def _everything(messages):
    return json.dumps(messages, ensure_ascii=False)


REC1 = {"id": "u1", "msg_index": 1, "timestamp": "2026-11-10T10:00:00+00:00",
        "sender_name": "Mike", "content": "good morning"}
HISTORY = [
    {"id": "u1", "role": "user", "content": "good morning", "sender_type": "human",
     "sender_name": "Mike", "msg_index": 1, "timestamp": REC1["timestamp"]},
    {"id": "a1", "role": "assistant", "content": "morning", "sender_type": "agent",
     "sender_name": "Ark", "agent_owner": "dpc-node-me", "msg_index": 2,
     "timestamp": "2026-11-10T10:01:00+00:00"},
]
REC2 = {"id": "u2", "msg_index": 3, "timestamp": "2026-11-10T10:05:00+00:00",
        "sender_name": "Mike", "content": "next question"}


def _first_turn(root, open_layers=None):
    return build_llm_messages(
        agent_root=root, memory=Memory(root),
        task={"id": AGENT, "type": "chat", "text": "good morning", "trigger_record": REC1},
        reader_identity=READER, recall_results=[], open_layers=open_layers)


def _second_turn(root, annotations, open_layers=None):
    return build_llm_messages(
        agent_root=root, memory=Memory(root),
        task={"id": AGENT, "type": "chat", "text": "next question", "trigger_record": REC2},
        conversation_history=HISTORY, reader_identity=READER, sent_annotations=annotations,
        recall_results=[], open_layers=open_layers)


def test_the_system_block_is_byte_identical_with_and_without_a_pending_brief(world):
    root, brief_path = world
    _write_brief(brief_path)
    with_brief, _ = _first_turn(root)
    _write_brief(brief_path, consumed=True)
    without_brief, _ = _first_turn(root)
    assert with_brief[0] == without_brief[0]
    system = json.dumps(with_brief[0], ensure_ascii=False)
    assert HEADING not in system and not any(mark in system for mark in MARKS)


def test_the_first_turn_carries_the_brief_at_the_end_of_its_tail(world):
    root, brief_path = world
    _write_brief(brief_path)
    messages, cap = _first_turn(root)
    sent = messages[-1]["content"]
    start = sent.index(TURN_CONTEXT_OPEN)
    assert start < sent.index(HEADING) < sent.rindex(TURN_CONTEXT_CLOSE)
    assert sent.endswith(TURN_CONTEXT_CLOSE)
    assert all(mark in sent for mark in MARKS)
    assert sent.index("## Runtime context") < sent.index(HEADING)


def test_what_is_recorded_for_replay_is_the_tail_exactly_as_sent(world):
    root, brief_path = world
    _write_brief(brief_path)
    messages, cap = _first_turn(root)
    sent = messages[-1]["content"]
    recorded = cap["turn_context"]
    assert recorded.startswith("\n\n" + TURN_CONTEXT_OPEN) and recorded.endswith(TURN_CONTEXT_CLOSE)
    assert all(mark in recorded for mark in MARKS)
    assert recorded.index("## Runtime context") < recorded.index(HEADING)
    assert sent == history_prefix(REC1) + "good morning" + recorded


def test_the_second_turn_replays_the_brief_once_inside_the_first_message(world):
    root, brief_path = world
    _write_brief(brief_path)
    first, cap1 = _first_turn(root)
    store = SentAnnotationStore(root)
    store.record(AGENT, "u1", cap1["turn_context"], live_message_ids=["u1"])
    _mark_morning_brief_consumed(Memory(root), cap1["morning_brief"])

    second, cap2 = _second_turn(root, store.load(AGENT))
    assert cap2["replayed_tails"] == 1 and "morning_brief" not in cap2
    replayed = second[1]["content"]
    assert replayed == first[-1]["content"]
    assert all(mark in replayed for mark in MARKS)
    assert _everything(second).count(HEADING) == 1
    assert HEADING not in second[-1]["content"]
    assert second[0] == first[0]
    assert second[1]["content"].startswith(first[-1]["content"])


def test_a_later_turn_with_l5_closed_replays_the_first_message_without_the_brief(world):
    root, brief_path = world
    _write_brief(brief_path)
    first, cap1 = _first_turn(root)
    store = SentAnnotationStore(root)
    store.record(AGENT, "u1", cap1["turn_context"], live_message_ids=["u1"])
    _mark_morning_brief_consumed(Memory(root), cap1["morning_brief"])

    closed, cap = _second_turn(root, store.load(AGENT), open_layers=frozenset({"L6"}))
    assert HEADING not in _everything(closed)
    assert not any(mark in _everything(closed) for mark in MARKS)
    replayed = closed[1]["content"]
    assert cap["replayed_tails"] == 1
    assert replayed.endswith(TURN_CONTEXT_CLOSE) and "## Runtime context" in replayed
    sent = first[-1]["content"]
    at = sent.index("\n\n" + HEADING)
    assert replayed == sent[:at] + "\n" + TURN_CONTEXT_CLOSE


def test_building_the_prompt_does_not_consume_the_brief_marking_after_the_answer_does(world):
    root, brief_path = world
    _write_brief(brief_path)
    first, cap = _first_turn(root)
    assert HEADING in first[-1]["content"]
    assert _consumed(brief_path) is False
    again, _ = _first_turn(root)
    assert HEADING in again[-1]["content"]
    _mark_morning_brief_consumed(Memory(root), cap["morning_brief"])
    assert _consumed(brief_path) is True
    later, later_cap = _second_turn(root, None)
    assert HEADING not in _everything(later) and "morning_brief" not in later_cap


def test_an_already_consumed_brief_is_nowhere(world):
    root, brief_path = world
    _write_brief(brief_path, consumed=True)
    messages, cap = _first_turn(root)
    assert HEADING not in _everything(messages) and HEADING not in cap["turn_context"]


@pytest.mark.parametrize("layers", [frozenset({"L6"}), frozenset()])
def test_with_l5_closed_the_brief_is_nowhere_and_not_consumed(world, layers):
    root, brief_path = world
    _write_brief(brief_path)
    messages, cap = _first_turn(root, open_layers=layers)
    assert HEADING not in _everything(messages) and HEADING not in cap["turn_context"]
    assert not any(mark in _everything(messages) for mark in MARKS)
    assert _consumed(brief_path) is False
    assert "morning_brief" not in cap
    owner, owner_cap = _first_turn(root, open_layers=ALL_LAYERS)
    assert HEADING in owner[-1]["content"] and "morning_brief" in owner_cap


def test_no_brief_file_leaves_the_tail_as_it_was(world):
    root, brief_path = world
    messages, cap = _first_turn(root)
    assert messages[-1]["content"] == history_prefix(REC1) + "good morning" + cap["turn_context"]


# -- when the brief is consumed: after the first request carrying it was answered ----------


def test_a_tail_without_a_brief_reports_none_to_mark(world):
    root, brief_path = world
    _write_brief(brief_path, consumed=True)
    _, cap = _first_turn(root)
    assert "morning_brief" not in cap


class _Llm:
    """A model double: replies from a script; an exception in it is raised, an Event waited on."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, asyncio.Event):
            await item.wait()
            item = {"role": "assistant", "content": "late", "tool_calls": []}
        return item, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}


class _NoTools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return []

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        return "ok"


ANSWER = {"role": "assistant", "content": "good morning", "tool_calls": []}


@pytest.fixture(autouse=True)
def _no_agent_config(monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})


async def _loop(llm, tmp_path, marked, kill=None, mark=None):
    return await run_llm_loop(
        messages=[{"role": "user", "content": "hi"}], tools=_NoTools(), llm=llm,
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t1",
        kill_event=kill, after_first_answer=mark or (lambda: marked.append(1)))


@pytest.mark.asyncio
async def test_the_first_answered_call_marks_the_brief_once(tmp_path):
    marked = []
    text, _u, _t = await _loop(_Llm(ANSWER), tmp_path, marked)
    assert text == "good morning" and marked == [1]


@pytest.mark.asyncio
async def test_a_provider_exception_on_the_first_call_marks_nothing(tmp_path):
    marked = []
    _t, _u, trace = await _loop(_Llm(RuntimeError("boom")), tmp_path, marked)
    assert trace["turn_end"] == "failed" and marked == []


@pytest.mark.asyncio
async def test_a_peer_refusal_on_the_first_call_marks_nothing(tmp_path):
    marked = []
    _t, _u, trace = await _loop(_Llm(PeerRefused("not today", code="")), tmp_path, marked)
    assert trace["turn_end"] == "refused" and marked == []


@pytest.mark.asyncio
async def test_a_kill_before_the_first_answer_marks_nothing(tmp_path):
    marked = []
    kill = asyncio.Event()
    llm = _Llm(asyncio.Event())
    task = asyncio.ensure_future(_loop(llm, tmp_path, marked, kill=kill))
    while llm.calls == 0:
        await asyncio.sleep(0.01)
    kill.set()
    _t, _u, trace = await asyncio.wait_for(task, 2)
    assert trace["turn_end"] == "killed" and marked == []


@pytest.mark.asyncio
async def test_a_failing_mark_does_not_end_the_turn(tmp_path):
    def _boom():
        raise OSError("disk")

    text, _u, _t = await _loop(_Llm(ANSWER), tmp_path, [], mark=_boom)
    assert text == "good morning"


# The agent wired to it: the real prompt build and the real guard, the loop a double.


class _LoopDouble:
    def __init__(self, answer=True, error=None):
        self.answer, self.error, self.calls, self.mark = answer, error, 0, "unset"

    async def __call__(self, **kwargs):
        self.calls += 1
        self.mark = kwargs.get("after_first_answer")
        if self.error:
            raise self.error
        if self.answer and self.mark:
            self.mark()
        return "answer", {"rounds": 1}, {}


def _agent(world, monkeypatch, loop):
    root, _ = world
    monkeypatch.setattr(agent_mod, "run_llm_loop", loop)

    async def _no_recall(*a, **k):
        return []

    monkeypatch.setattr(agent_mod, "recall_off_loop", _no_recall)
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = root
    agent.memory = Memory(root)
    agent.skill_store = agent._embedding_provider = agent._firewall = None
    agent._firewall_profile = None
    agent._service = None
    agent.tools = MagicMock()
    agent.llm = SimpleNamespace()
    agent.skill_reflector = MagicMock()
    agent.skill_reflector.record_outcome.return_value = []
    agent.config = SimpleNamespace(billing_model="pay_per_use", budget_usd=None, max_rounds=1)
    agent._get_allowed_tools = lambda **kw: set()
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    agent._update_usage_stats = lambda **kw: None
    agent._last_usage = {}
    agent._last_trace = {}
    return agent


@pytest.mark.asyncio
async def test_the_pre_flight_window_refusal_leaves_the_brief_unconsumed(world, monkeypatch):
    _, brief_path = world
    _write_brief(brief_path)
    loop = _LoopDouble()
    agent = _agent(world, monkeypatch, loop)
    text = await agent.process(
        "good morning", AGENT, message_source="ui", session_state={"tokens_limit": 1000})
    assert "Context window full" in text
    assert loop.calls == 0 and _consumed(brief_path) is False


@pytest.mark.asyncio
async def test_a_turn_ended_before_the_answer_leaves_the_brief_unconsumed(world, monkeypatch):
    _, brief_path = world
    _write_brief(brief_path)
    loop = _LoopDouble(answer=False)
    await _agent(world, monkeypatch, loop).process("good morning", AGENT, message_source="ui")
    assert loop.calls == 1 and callable(loop.mark) and _consumed(brief_path) is False


@pytest.mark.asyncio
async def test_a_loop_that_raises_before_the_answer_leaves_the_brief_unconsumed(world, monkeypatch):
    _, brief_path = world
    _write_brief(brief_path)
    loop = _LoopDouble(answer=False, error=RuntimeError("gone"))
    with pytest.raises(RuntimeError):
        await _agent(world, monkeypatch, loop).process("good morning", AGENT, message_source="ui")
    assert _consumed(brief_path) is False


@pytest.mark.asyncio
async def test_the_first_answered_request_consumes_the_brief_and_the_next_turn_has_none(
        world, monkeypatch):
    _, brief_path = world
    _write_brief(brief_path)
    loop = _LoopDouble()
    agent = _agent(world, monkeypatch, loop)
    assert await agent.process("good morning", AGENT, message_source="ui") == "answer"
    assert _consumed(brief_path) is True
    assert agent._last_cap_info["morning_brief"]["summary"] == "BRIEF-SUMMARY-MARK"
    # The next turn has no brief to carry, so the loop is given nothing to mark.
    await agent.process("next question", AGENT, message_source="ui")
    assert loop.mark is None and "morning_brief" not in agent._last_cap_info


@pytest.mark.asyncio
async def test_a_failed_record_of_the_tail_leaves_nothing_replayable_and_the_brief_still_goes_once(
        world, monkeypatch):
    # Recording happens after the answer. If it fails nothing of that tail is replayed;
    # the request still carried the brief and the answer consumed it.
    root, brief_path = world
    _write_brief(brief_path)

    def _fail(self, *a, **k):
        raise OSError("read-only")

    monkeypatch.setattr(SentAnnotationStore, "record", _fail)
    loop = _LoopDouble()
    agent = _agent(world, monkeypatch, loop)
    history = SimpleNamespace(get_message_history=lambda: [dict(HISTORY[0])])
    await agent.process("good morning", AGENT, message_source="ui",
                        conversation_monitor=history, trigger_message_id="u1")
    assert _consumed(brief_path) is True
    assert SentAnnotationStore(root).load(AGENT) == {}


def _recorded_after(world, monkeypatch, loop):
    root, _ = world
    agent = _agent(world, monkeypatch, loop)
    history = SimpleNamespace(get_message_history=lambda: [dict(HISTORY[0])])
    asyncio.run(_process_u1(agent, history))
    return SentAnnotationStore(root).load(AGENT)


async def _process_u1(agent, history):
    try:
        await agent.process("good morning", AGENT, message_source="ui",
                            conversation_monitor=history, trigger_message_id="u1")
    except RuntimeError:
        pass


def test_an_answered_turn_records_the_tail_with_the_brief(world, monkeypatch):
    _, brief_path = world
    _write_brief(brief_path)
    stored = _recorded_after(world, monkeypatch, _LoopDouble())
    assert set(stored) == {"u1"} and HEADING in stored["u1"]
    assert all(mark in stored["u1"] for mark in MARKS)


@pytest.mark.parametrize("loop", [_LoopDouble(answer=False),
                                  _LoopDouble(answer=False, error=RuntimeError("gone"))],
                         ids=["ended", "raised"])
def test_a_turn_with_no_answer_records_no_tail_so_the_next_offer_is_not_doubled(
        world, monkeypatch, loop):
    _, brief_path = world
    _write_brief(brief_path)
    assert _recorded_after(world, monkeypatch, loop) == {}
    assert _consumed(brief_path) is False


def test_a_turn_without_a_brief_records_its_tail_before_the_call(world, monkeypatch):
    _, brief_path = world
    _write_brief(brief_path, consumed=True)
    stored = _recorded_after(world, monkeypatch, _LoopDouble(answer=False))
    assert set(stored) == {"u1"} and HEADING not in stored["u1"]


# -- the system block's note names the brief truthfully ---------------------------------


def test_the_turn_context_note_says_the_brief_normally_arrives_as_a_chat_message():
    note = " ".join(TURN_CONTEXT_NOTE.split())
    assert "morning brief" in note
    assert "normally arrives as a chat message" in note
    assert "only if that post did not happen" in note
    assert "first message after the sleep" in note
    assert "stay in that message for the rest of the session" in note
    # The old claim, that every first message after sleep carries it, is gone.
    assert "also carries the" not in note
