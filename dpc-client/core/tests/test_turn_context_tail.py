"""(F) stable prefix — THE-PROMPT-CHANGES-IN-FRONT-OF-THE-HISTORY-SO-EVERY-TURN-IS-A-CACHE-MISS.

Three falsifiers, one per test class:
  1. the prompt of turn n+1 is a pure append of the prompt of turn n (byte-identical
     prefix through the previous user message and its tail), even though the per-turn
     content itself changed between the two builds;
  2. what the model is shown is honest — the user's words open the message, the tail is
     delimited and announced, an image message keeps its image;
  3. the side store does not outlive the history it annotates.
"""

import json
import pathlib

import pytest

from dpc_client_core.dpc_agent import context as ctx
from dpc_client_core.dpc_agent.context import (
    TURN_CONTEXT_CLOSE, TURN_CONTEXT_NOTE, TURN_CONTEXT_OPEN, build_llm_messages,
    format_turn_context, append_turn_context, history_prefix,
)
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.sent_annotations import SentAnnotationStore


READER = {"agent_id": "agent_001", "display_name": "Ark", "node_id": "dpc-node-me"}


def _serialize(messages):
    return json.dumps(messages, ensure_ascii=False, sort_keys=True)


@pytest.fixture()
def agent_root(tmp_path):
    root = tmp_path / "agent_001"
    Memory(root).ensure_files()
    return root


@pytest.fixture()
def clocks(monkeypatch):
    """Two builds must see two different clocks, or the test proves nothing."""
    ticks = iter(["2026-08-19T10:00:00+00:00", "2026-08-19T10:05:00+00:00",
                  "2026-08-19T10:10:00+00:00", "2026-08-19T10:15:00+00:00"])
    monkeypatch.setattr(ctx, "utc_now_iso", lambda: next(ticks))


def _build(agent_root, conversation_id, task_text, history, *, annotations=None,
           trigger_record=None, reader=READER, session_state=None):
    task = {"id": conversation_id, "type": "chat", "text": task_text}
    if trigger_record:
        task["trigger_record"] = trigger_record
    return build_llm_messages(
        agent_root=agent_root, memory=Memory(agent_root), task=task,
        conversation_history=history, reader_identity=reader,
        sent_annotations=annotations, session_state=session_state,
    )


class TestPromptIsAPureAppend:
    def test_next_turn_starts_with_previous_turn_byte_for_byte(self, agent_root, clocks):
        store = SentAnnotationStore(agent_root)
        conv = "agent_001"
        # The dispatcher's form, not a tidy one: the group path hands the agent
        # "[sender]: text" while the history keeps "text" (service.py, group_handler).
        rec1 = {"id": "u1", "msg_index": 1, "timestamp": "2026-08-19T10:00:00+00:00",
                "sender_name": "Mike", "content": "hello"}

        # turn 1: no history, the trigger record is known
        m1, cap1 = _build(agent_root, conv, "[Mike]: hello", None, trigger_record=rec1)
        assert m1[-1]["content"].startswith("[#1 | 10:00:00 | Mike] hello\n\n")
        tail1 = cap1["turn_context"]
        assert tail1.startswith("\n\n" + TURN_CONTEXT_OPEN) and tail1.endswith(TURN_CONTEXT_CLOSE)
        assert "10:00:00" in tail1  # the runtime block carries the first clock
        store.record(conv, "u1", tail1, live_message_ids=["u1"])

        # turn 2: the record above is history, plus the agent's answer, plus a new question
        history = [
            {"id": "u1", "role": "user", "content": "hello", "sender_type": "human",
             "sender_name": "Mike", "msg_index": 1, "timestamp": rec1["timestamp"]},
            {"id": "a1", "role": "assistant", "content": "hi there", "sender_type": "agent",
             "sender_name": "Ark", "agent_owner": "dpc-node-me", "msg_index": 2,
             "timestamp": "2026-08-19T10:04:00+00:00"},
        ]
        rec2 = {"id": "u2", "msg_index": 3, "timestamp": "2026-08-19T10:05:00+00:00",
                "sender_name": "Mike", "content": "second question"}
        m2, cap2 = _build(agent_root, conv, "[Mike]: second question", history,
                          annotations=store.load(conv), trigger_record=rec2)

        # The whole of turn 1 is a prefix of turn 2 — system block, and the user
        # message with the tail it was sent with, although the clock has moved on.
        assert m2[:len(m1)] == m1
        assert cap2["replayed_tails"] == 1
        assert "10:05:00" in cap2["turn_context"] and "10:00:00" not in cap2["turn_context"]
        # and the new turn is behind, not in front
        assert m2[len(m1)]["role"] == "assistant"
        assert m2[-1]["role"] == "user" and m2[-1]["content"].endswith(TURN_CONTEXT_CLOSE)

    def test_without_the_store_the_replay_is_cold(self, agent_root, clocks):
        """The control: same two builds, no annotations — the previous user message
        comes back bare, so the prefix breaks exactly where the tail was."""
        conv = "agent_001"
        rec1 = {"id": "u1", "msg_index": 1, "timestamp": "2026-08-19T10:00:00+00:00",
                "sender_name": "Mike"}
        m1, _ = _build(agent_root, conv, "hello", None, trigger_record=rec1)
        history = [{"id": "u1", "role": "user", "content": "hello", "sender_type": "human",
                    "sender_name": "Mike", "msg_index": 1, "timestamp": rec1["timestamp"]}]
        m2, cap2 = _build(agent_root, conv, "second", history, annotations=None)
        assert m2[:len(m1)] != m1
        assert m2[1]["content"] == history_prefix(rec1) + "hello"
        assert cap2["replayed_tails"] == 0

    def test_system_block_carries_nothing_per_turn(self, agent_root, clocks):
        m1, _ = _build(agent_root, "agent_001", "a", None)
        m2, _ = _build(agent_root, "agent_001", "b", None)
        assert m1[0] == m2[0]
        assert len(m1[0]["content"]) == 2
        joined = "".join(b["text"] for b in m1[0]["content"])
        assert "## Runtime context" not in joined
        assert TURN_CONTEXT_NOTE in joined


class TestWhatTheModelIsShown:
    def test_user_words_first_and_tail_delimited(self, agent_root, clocks):
        rec = {"id": "u1", "msg_index": 1, "timestamp": "2026-08-19T10:00:00+00:00",
               "sender_name": "Mike"}
        m, cap = _build(agent_root, "agent_001", "what is up", None, trigger_record=rec)
        content = m[-1]["content"]
        assert content.startswith("[#1 | 10:00:00 | Mike] what is up\n\n" + TURN_CONTEXT_OPEN)
        assert content.count(TURN_CONTEXT_OPEN) == 1 and content.endswith(TURN_CONTEXT_CLOSE)
        assert "## Runtime context" in cap["turn_context"]

    def test_body_comes_from_the_record_that_becomes_history(self, agent_root, clocks):
        """Johnny's repro on b43c44ec: the group dispatcher sends "[sender]: text",
        the history keeps "text" — the marker matched, the body did not, and the
        prefix broke a few bytes before the tail every turn."""
        rec = {"id": "m14", "msg_index": 14, "timestamp": "2026-08-19T18:14:33+00:00",
               "sender_name": "Mike Windows PC", "content": "@ALL ревью того что CC сделал"}
        m, _ = _build(agent_root, "group-x", "[Mike Windows PC]: @ALL ревью того что CC сделал",
                      None, trigger_record=rec)
        current = m[-1]["content"].split("\n\n" + TURN_CONTEXT_OPEN)[0]
        as_history = history_prefix(rec) + rec["content"]
        assert current == as_history
        assert "[Mike Windows PC]: [" not in current and current.count("Mike Windows PC") == 1

    def test_image_message_keeps_its_image_and_gets_the_tail_in_text(self, agent_root, clocks):
        task = {"id": "agent_001", "type": "chat", "text": "look",
                "image_base64": "AAAA", "image_mime": "image/png"}
        m, cap = build_llm_messages(agent_root=agent_root, memory=Memory(agent_root), task=task)
        content = m[-1]["content"]
        assert isinstance(content, list)
        kinds = [b["type"] for b in content]
        assert kinds == ["text", "image_url"]
        assert content[0]["text"].startswith("look") and content[0]["text"].endswith(TURN_CONTEXT_CLOSE)

    def test_soft_cap_prunes_the_tail_and_the_recorded_tail_is_what_was_sent(self, agent_root, clocks):
        # a fat progress log makes "## Recent progress" appear in the tail
        logs = agent_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "progress.jsonl").open("w", encoding="utf-8") as fh:
            for i in range(30):
                fh.write(json.dumps({"ts": "2026-08-19T09:00:00+00:00", "task_id": "agent_001",
                                     "message": "step %d " % i + "x" * 400}) + "\n")
        m_free, cap_free = _build(agent_root, "agent_001", "q", None,
                                  session_state={"tokens_limit": 0})
        assert "## Recent progress" in cap_free["turn_context"]
        m_cap, cap_cap = _build(agent_root, "agent_001", "q", None,
                                session_state={"tokens_limit": 1})
        assert "## Recent progress" in cap_cap["trimmed_sections"]
        assert "## Recent progress" not in cap_cap["turn_context"]
        # what went out is what would be recorded
        assert m_cap[-1]["content"].endswith(cap_cap["turn_context"])

    def test_empty_turn_context_leaves_the_message_alone(self):
        assert format_turn_context([]) == ""
        assert append_turn_context("hi", "") == "hi"
        assert append_turn_context([{"type": "image_url", "image_url": {"url": "x"}}], "\n\nT") == [
            {"type": "text", "text": "T"}, {"type": "image_url", "image_url": {"url": "x"}}]


class TestTheStoreFollowsTheHistory:
    def test_record_prunes_ids_the_history_no_longer_holds(self, tmp_path):
        store = SentAnnotationStore(tmp_path)
        store.record("group-x", "m1", "\n\n<turn_context>one</turn_context>", live_message_ids=["m1"])
        store.record("group-x", "m2", "\n\n<turn_context>two</turn_context>", live_message_ids=["m1", "m2"])
        assert set(store.load("group-x")) == {"m1", "m2"}
        # the conversation was reset: only m3 survives in the history
        store.record("group-x", "m3", "\n\n<turn_context>three</turn_context>", live_message_ids=["m3"])
        assert set(store.load("group-x")) == {"m3"}

    def test_prune_to_nothing_removes_the_file(self, tmp_path):
        store = SentAnnotationStore(tmp_path)
        store.record("agent_001", "m1", "tail", live_message_ids=["m1"])
        assert store.path_for("agent_001").exists()
        assert store.prune("agent_001", live_message_ids=[]) == 1
        assert not store.path_for("agent_001").exists()
        assert store.load("agent_001") == {}

    def test_unreadable_file_reads_as_empty(self, tmp_path):
        store = SentAnnotationStore(tmp_path)
        path = store.path_for("agent_001")
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        assert store.load("agent_001") == {}

    def test_conversation_ids_are_safe_file_names(self, tmp_path):
        store = SentAnnotationStore(tmp_path)
        p = store.path_for("group-abc/../weird id")
        assert p.parent == store.root
        assert "/" not in p.name and "\\" not in p.name and " " not in p.name
