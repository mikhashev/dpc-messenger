"""The embedded-agent provider serialises turns per provider alias. When one
agent waits for another's whole turn, an INFO line names both and the wait;
a call that waits for nothing logs none, and a failed call frees the holder."""

import asyncio
import logging

import pytest

from dpc_client_core.providers import dpc_agent_provider as module
from dpc_client_core.providers.dpc_agent_provider import DpcAgentProvider

LOGGER = "dpc_client_core.providers.dpc_agent_provider"


def _provider(monkeypatch, hold_s=0.0, fail=False):
    monkeypatch.setattr(module, "PROVIDER_LOCK_WAIT_LOGGED_S", 0.05)
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.utils.load_agent_config",
        lambda agent_id: {"provider_alias": "shared"},
    )
    provider = DpcAgentProvider("dpc_agent", {})

    async def impl(prompt, conversation_id=None, agent_llm_provider=None, **kwargs):
        await asyncio.sleep(hold_s)
        if fail:
            raise RuntimeError("boom")
        return "ok"

    monkeypatch.setattr(provider, "_generate_response_impl", impl)
    return provider


def _waits(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO and "waited" in r.getMessage()]


@pytest.mark.asyncio
async def test_the_second_agent_logs_who_held_the_lock_and_for_how_long(monkeypatch, caplog):
    provider = _provider(monkeypatch, hold_s=0.3)
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    first = asyncio.create_task(provider.generate_response("a", conversation_id="agent_001"))
    await asyncio.sleep(0.05)
    await provider.generate_response("b", conversation_id="agent_002")
    await first
    lines = _waits(caplog)
    assert len(lines) == 1
    assert "agent 'agent_002'" in lines[0] and "held by agent 'agent_001'" in lines[0]
    assert "provider 'shared'" in lines[0]
    assert any("held provider 'shared'" in r.getMessage() for r in caplog.records
               if r.levelno == logging.DEBUG)


@pytest.mark.asyncio
async def test_a_lone_call_logs_no_wait(monkeypatch, caplog):
    provider = _provider(monkeypatch)
    caplog.set_level(logging.INFO, logger=LOGGER)
    await provider.generate_response("a", conversation_id="agent_001")
    assert _waits(caplog) == []


@pytest.mark.asyncio
async def test_a_call_that_raises_clears_the_holder(monkeypatch):
    provider = _provider(monkeypatch, fail=True)
    with pytest.raises(RuntimeError):
        await provider.generate_response("a", conversation_id="agent_001")
    assert provider._provider_lock_holders == {}
