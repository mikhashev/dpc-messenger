"""A-GROUP-TRIGGER-RENDERED-AFTER-LATER-REPLIES-REORDERS-THE-NEXT-PROMPT-AND-COLD-PREFILLS-THE-ROOM,
second break.

The extended sandbox paths were written into the cached system block, ahead of all
history. Adding a read-only path inserted a line mid-list, so every agent on that
firewall profile re-prefilled its whole conversation on the next turn (42,931 tokens
observed 2026-09-26). The list now travels in the per-turn tail: everything before
the current message is the same whatever the list holds, and the list still reaches
the model.
"""

import pytest

from dpc_client_core.dpc_agent import context as ctx
from dpc_client_core.dpc_agent.context import build_llm_messages
from dpc_client_core.dpc_agent.memory import Memory

GROUP = "group-882898238aa6"
READER = {"agent_id": "agent_johnny", "display_name": "Johnny", "node_id": "dpc-node-me"}
HISTORY = [
    {"id": "m1", "msg_index": 1, "timestamp": "2026-09-26T10:00:00+00:00",
     "sender_name": "Mike", "sender_type": "human", "role": "user", "content": "hello"},
    {"id": "m2", "msg_index": 2, "timestamp": "2026-09-26T10:00:05+00:00",
     "sender_name": "Johnny", "sender_type": "agent", "agent_owner": "dpc-node-me",
     "role": "assistant", "content": "hi"},
]
TRIGGER = {"msg_index": 3, "timestamp": "2026-09-26T10:01:00+00:00",
           "sender_name": "Mike", "content": "read the new folder"}

RO_BEFORE = [r"C:\work\ai-studio"]
RO_AFTER = [r"C:\work\ai-studio", r"C:\work\dpc-research\dpc-nethackers"]
RW = [r"C:\Users\me\.dpc\temp"]


class _NoEmbedder:
    model_name = "no-model-in-this-test"


@pytest.fixture()
def agent_root(tmp_path, monkeypatch):
    monkeypatch.setattr(ctx, "utc_now_iso", lambda: "2026-09-26T10:01:01+00:00")
    root = tmp_path / "agent_johnny"
    Memory(root).ensure_files()
    return root


def _build(agent_root, read_only):
    messages, _ = build_llm_messages(
        agent_root=agent_root,
        memory=Memory(agent_root),
        task={"id": GROUP, "type": "chat", "text": "[Mike]: read the new folder",
              "trigger_record": TRIGGER},
        conversation_history=HISTORY,
        allowed_tools={"read_file"},
        all_tools={"read_file": True, "write_file": False},
        sandbox_read_only=read_only,
        sandbox_read_write=RW,
        embedding_provider=_NoEmbedder(),
        reader_identity=READER,
    )
    return messages


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(str(b.get("text", "")) for b in content if isinstance(b, dict))


def test_adding_a_sandbox_path_changes_nothing_before_the_current_message(agent_root):
    before = _build(agent_root, RO_BEFORE)
    after = _build(agent_root, RO_AFTER)

    # System block and history: byte for byte, whatever the path list holds.
    assert before[:-1] == after[:-1]

    # The list still reaches the model, every path of it.
    prompt = "".join(_text(m["content"]) for m in after)
    for path in RO_AFTER + RW:
        assert path in prompt
    assert r"C:\work\dpc-research\dpc-nethackers" in _text(after[-1]["content"])
