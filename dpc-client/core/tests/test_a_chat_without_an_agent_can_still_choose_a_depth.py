"""Local AI Chat renders a Reasoning control that reached nothing.

Two independent halves, measured through the running service on 2026-08-15:
the value was written to `updateAgentConfig("local_ai")`, which answers «Agent
not found» inside an envelope that says OK — and even a stored value would not
have travelled, because the query path a chat without an agent takes carried no
effort at all. `d8480074` taught every provider to receive one; this covers the
path from the command to them.

Real fakes rather than mocks: what has to be observed is the keyword the next
layer actually received, so each double records its own call.
"""

import pytest

from dpc_client_core.inference_orchestrator import InferenceOrchestrator


class _RecordingManager:
    """Stands in for LLMManager, and records the kwargs `query` was called with.

    `LLMManager.query` hands its extra kwargs straight to
    `provider.generate_response(prompt, **kwargs)`, so a keyword that arrives
    here arrives at the provider — which is the property this file is about.
    """

    def __init__(self):
        self.calls = []

    async def query(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return {"response": "ok", "model": "m", "provider": "p"}


class _Service:
    """The two attributes the orchestrator reads at construction.

    `p2p_manager` is only needed by the remote path and is never dialled here.
    """

    def __init__(self, manager):
        self.llm_manager = manager
        self.p2p_manager = None


def _orchestrator():
    manager = _RecordingManager()
    return InferenceOrchestrator(_Service(manager)), manager


@pytest.mark.asyncio
async def test_the_level_reaches_the_manager_that_hands_it_to_the_provider():
    orch, manager = _orchestrator()

    await orch.execute_inference(prompt="hi", reasoning_effort="low")

    assert manager.calls[0]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_absent_stays_absent_so_the_alias_config_keeps_deciding():
    """`None` is not a level. A chat that never touched the selector must leave
    the provider alias's own configuration in charge, exactly as before — the
    fix adds a way to speak, not a new default."""
    orch, manager = _orchestrator()

    await orch.execute_inference(prompt="hi")

    assert manager.calls[0]["reasoning_effort"] is None


@pytest.mark.asyncio
async def test_off_travels_like_any_other_word():
    """`off` is the one value every provider accepts, and it is a switch rather
    than an amount — so it is the level most likely to be dropped by a layer
    that treats falsy as absent."""
    orch, manager = _orchestrator()

    await orch.execute_inference(prompt="hi", reasoning_effort="off")

    assert manager.calls[0]["reasoning_effort"] == "off"


@pytest.mark.asyncio
async def test_the_remote_path_is_left_alone():
    """Remote inference runs on somebody else's machine under their own alias
    config; nothing here claims to set a depth there, and pretending otherwise
    would be a promise this code cannot keep."""
    orch, manager = _orchestrator()
    sent = {}

    async def _remote(**kwargs):
        sent.update(kwargs)
        return {"response": "ok", "model": "m", "provider": "p"}

    orch._execute_remote_inference = _remote

    await orch.execute_inference(
        prompt="hi", compute_host="dpc-node-somebody", reasoning_effort="max"
    )

    assert manager.calls == []
    assert "reasoning_effort" not in sent
