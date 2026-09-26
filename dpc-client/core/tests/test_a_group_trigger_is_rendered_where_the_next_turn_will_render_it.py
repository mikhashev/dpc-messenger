"""A-GROUP-TRIGGER-RENDERED-AFTER-LATER-REPLIES-REORDERS-THE-NEXT-PROMPT-AND-COLD-PREFILLS-THE-ROOM.

An @all in a group queues the embedded agents behind one per-group lock
(service.py `_invoke_agent_in_group_serialized`). The second agent's turn starts
after the first one has answered, so when `DpcAgent.process` reads the room the
first agent's reply is already there. `select_prior_history` drops only the
trigger by id and keeps the later reply; the trigger is then rendered last as the
current message. Next turn the history comes back in index order, so the prompt
diverges at the trigger's position and the engine re-prefills everything after it.

Observed 2026-09-26 in group-882898238aa6: Johnny's #10 turn was built as
[#7 CC, #9 Ark, #8 Mike]; his next turn as [#7, #8, #9, #10, ...]; llama-server
diffed at '[#9 | 10:03:20 | Ark]' vs '[#8 | 10:03:04 | Mike Windows PC]' and
re-prefilled 50,056 tokens.

The test drives the real `DpcAgent.process` up to the point the prompt is built and
the tail is recorded, and stops before the LLM loop.
"""

import pathlib
import re

import pytest

from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent import context as ctx
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.memory import Memory

GROUP = "group-882898238aa6"
NODE = "dpc-node-me"
JOHNNY = {"agent_id": "agent_johnny", "display_name": "Johnny", "node_id": NODE}


def _rec(rid, idx, ts, sender, content, *, kind, owner=None):
    role = "assistant" if kind == "agent" else "user"
    rec = {"id": rid, "msg_index": idx, "timestamp": f"2026-09-26T{ts}+00:00",
           "sender_name": sender, "sender_type": kind, "role": role, "content": content}
    if owner:
        rec["agent_owner"] = owner
    return rec


R7 = _rec("m7", 7, "10:02:45", "CC_windows", "CC answer. Waiting for Mike.", kind="agent", owner=NODE)
R8 = _rec("m8", 8, "10:03:04", "Mike Windows PC", "explain the contest @ALL", kind="human")
R9 = _rec("m9", 9, "10:03:20", "Ark", "Ark explains the contest.", kind="agent", owner=NODE)
R10 = _rec("m10", 10, "10:04:09", "Johnny", "Johnny's answer to #8.", kind="agent", owner=NODE)
R11 = _rec("m11", 11, "10:05:41", "Mike Windows PC", "and the plan? @Johnny", kind="human")


class _Monitor:
    def __init__(self, history):
        self._history = history

    def get_message_history(self):
        return list(self._history)


class _Stop(Exception):
    pass


class _NoEmbedder:
    """A model name no index was built with: recall skips the vector search
    instead of loading an embedding model into this test."""
    model_name = "no-model-in-this-test"


class _Tools:
    def set_context(self, _ctx):
        # The first line after the prompt is built and its tail recorded.
        raise _Stop


def _agent(agent_root: pathlib.Path) -> DpcAgent:
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = agent_root
    agent.memory = Memory(agent_root)
    agent.skill_store = None
    agent._embedding_provider = _NoEmbedder()
    agent._service = None
    agent._firewall = None
    agent._get_allowed_tools = lambda **_kw: None
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    agent.tools = _Tools()
    return agent


@pytest.fixture()
def agent_root(tmp_path):
    root = tmp_path / "agent_johnny"
    Memory(root).ensure_files()
    return root


@pytest.fixture()
def built(monkeypatch):
    """Capture what process() built, from the real builder."""
    seen = []
    real = agent_mod.build_llm_messages

    def capture(**kwargs):
        messages, cap = real(**kwargs)
        seen.append(messages)
        return messages, cap

    monkeypatch.setattr(agent_mod, "build_llm_messages", capture)
    ticks = iter(["2026-09-26T10:03:21+00:00", "2026-09-26T10:05:42+00:00"])
    monkeypatch.setattr(ctx, "utc_now_iso", lambda: next(ticks))
    return seen


async def _turn(agent, history, trigger):
    with pytest.raises(_Stop):
        await agent.process(
            message=f"[{trigger['sender_name']}]: {trigger['content']}",
            conversation_id=GROUP,
            conversation_monitor=_Monitor(history),
            reader_identity=JOHNNY,
            trigger_message_id=trigger["id"],
        )


def _markers(messages):
    """The msg_index each conversation message opens with, in prompt order."""
    out = []
    for m in messages[1:]:
        text = m["content"] if isinstance(m["content"], str) else str(m["content"])
        hit = re.match(r"\[#(\d+)", text)
        out.append(int(hit.group(1)) if hit else None)
    return out


@pytest.mark.asyncio
async def test_the_turn_after_a_queued_group_trigger_starts_with_this_turn_byte_for_byte(
        agent_root, built):
    agent = _agent(agent_root)

    # Turn N: Johnny woken by Mike's #8, run after Ark (queued first) posted #9.
    await _turn(agent, [R7, R8, R9], R8)
    # Turn N+1: Johnny's own #10 is in, Mike asks again in #11.
    await _turn(agent, [R7, R8, R9, R10, R11], R11)

    turn_n, turn_n1 = built
    # Readable first: the order of the rendered records.
    assert _markers(turn_n1)[:len(turn_n) - 1] == _markers(turn_n)
    # The property the engine's cache needs.
    assert turn_n1[:len(turn_n)] == turn_n
