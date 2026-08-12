"""Active Recall has to be offered on the turns where the model has least to go on.

The block was entered on `task["content"]` and searched on `task["text"]`, while the
only producer of that dictionary — `Agent.process` — writes `text` and never
`content`. So entry depended entirely on conversation history existing, which it does
not for a scheduled task (no monitor is passed on that path) and does not for the
opening message of a conversation. The skip has no log line of its own, so it read as
"nothing matched" rather than "never asked".

Measured on iris, 2026-08-12: a scheduled task at 17:14:56 and an opening group
message at 17:17:42 produced no recall line at all; her second message in the same
conversation at 17:19:02 produced all three channels.
"""

import pathlib

import pytest

from dpc_client_core.dpc_agent.context import task_query


def test_the_query_is_read_from_the_key_the_producer_writes():
    """`Agent.process` builds `{"id", "type", "text", …}` — see agent.py."""
    assert task_query({"id": "c1", "type": "chat", "text": "where is my data"}) == "where is my data"


def test_content_is_still_accepted():
    assert task_query({"content": "from some other caller"}) == "from some other caller"


def test_text_wins_when_both_are_present():
    assert task_query({"text": "asked", "content": "stale"}) == "asked"


def test_an_empty_task_asks_nothing_rather_than_raising():
    assert task_query({}) == ""
    assert task_query({"text": None, "content": None}) == ""


def test_the_gate_and_the_search_read_the_same_thing():
    """The defect was not a wrong key but two keys for one intent.

    A unit test cannot reach the branch — `build_llm_messages` needs an agent root, a
    memory, an embedding provider — so the property is stated where it lives: the
    condition that decides whether to recall is the same expression the recall then
    searches on.
    """
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "dpc_client_core" / "dpc_agent" / "context.py").read_text(encoding="utf-8")

    assert "_human_text = task_query(task)" in source
    assert "if conversation_history or _human_text:" in source, (
        "the recall block must be entered on the task's own query — gating on a key "
        "nothing writes made every scheduled task and every opening message skip recall"
    )
    # Counted rather than positioned: `content` may be read in exactly one place,
    # whatever that place ends up being called. A rename of task_query, or a comment
    # that mentions the key, leaves this assertion meaning what it meant.
    assert source.count('task.get("content"') == 1, (
        "`content` must be read in one place only — two readers of one intent is how "
        "the gate and the search came to disagree"
    )
