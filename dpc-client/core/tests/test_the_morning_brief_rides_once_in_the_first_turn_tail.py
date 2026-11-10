"""THE-ONE-SHOT-MORNING-BRIEF-CHANGES-THE-SYSTEM-BLOCK-FOR-ONE-TURN.

The 1:1 agent's morning brief used to join the system block once and be marked
consumed: the system prefix changed for one turn and again on the next, two cold
prefills, and the model lost the brief after one turn. Decided 2026-11-10 (ADR-014
amendment): the brief rides once at the end of the first turn's tail after sleep
(ADR-040 F2), it is not recorded for replay (ADR-045 decision 7), the system block
never carries it, and with L5 closed (ADR-048) it is neither shown nor consumed.
"""

import json

import pytest

from dpc_client_core.dpc_agent import context as ctx
from dpc_client_core.dpc_agent.context import (
    TURN_CONTEXT_CLOSE, TURN_CONTEXT_OPEN, build_llm_messages, history_prefix,
)
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


def test_what_is_recorded_for_replay_is_the_tail_without_the_brief(world):
    root, brief_path = world
    _write_brief(brief_path)
    messages, cap = _first_turn(root)
    sent = messages[-1]["content"]
    recorded = cap["turn_context"]
    assert recorded.startswith("\n\n" + TURN_CONTEXT_OPEN) and recorded.endswith(TURN_CONTEXT_CLOSE)
    assert HEADING not in recorded and not any(mark in recorded for mark in MARKS)
    # The sent message is the recorded one with the brief block put in before the close:
    # they differ at that one place and nowhere ahead of it.
    at =sent.index("\n\n" + HEADING)
    assert sent[:at] == (history_prefix(REC1) + "good morning" + recorded)[:at]
    assert sent[at:].endswith("\n" + TURN_CONTEXT_CLOSE)
    assert sent.replace(sent[at:-len("\n" + TURN_CONTEXT_CLOSE)], "") == (
        history_prefix(REC1) + "good morning" + recorded)


def test_the_second_turn_and_the_replayed_first_turn_do_not_carry_the_brief(world):
    root, brief_path = world
    _write_brief(brief_path)
    first, cap1 = _first_turn(root)
    store = SentAnnotationStore(root)
    store.record(AGENT, "u1", cap1["turn_context"], live_message_ids=["u1"])

    second, cap2 = _second_turn(root, store.load(AGENT))
    assert HEADING not in _everything(second)
    assert not any(mark in _everything(second) for mark in MARKS)
    assert cap2["replayed_tails"] == 1
    # The replayed first turn is the tail that was recorded: no brief.
    replayed = second[1]["content"]
    assert replayed == history_prefix(REC1) + "good morning" + cap1["turn_context"]
    # The system block did not move between the two turns.
    assert second[0] == first[0]
    # The prompt ahead of the brief's place is a prefix of the next one: the whole
    # system block and the first user message up to the brief.
    sent = first[-1]["content"]
    assert replayed.startswith(sent[: sent.index("\n\n" + HEADING)])


def test_the_brief_is_consumed_by_the_first_turn_and_not_offered_again(world):
    root, brief_path = world
    _write_brief(brief_path)
    assert _consumed(brief_path) is False
    first, _ = _first_turn(root)
    assert _consumed(brief_path) is True
    assert HEADING in first[-1]["content"]
    # Even a turn with no replayed tail at all does not bring it back.
    again, _ = _second_turn(root, None)
    assert HEADING not in _everything(again)


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
    # The owner's next turn, with L5 open, still gets it, once.
    owner, _ = _first_turn(root, open_layers=ALL_LAYERS)
    assert HEADING in owner[-1]["content"] and _consumed(brief_path) is True


def test_no_brief_file_leaves_the_tail_as_it_was(world):
    root, brief_path = world
    messages, cap = _first_turn(root)
    assert messages[-1]["content"] == history_prefix(REC1) + "good morning" + cap["turn_context"]
