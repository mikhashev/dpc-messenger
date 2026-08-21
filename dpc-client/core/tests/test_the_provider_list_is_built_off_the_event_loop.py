"""Listing providers asks a daemon, so it must not be asked on the loop.

`supports_vision()` reaches the Ollama daemon over the network. Built inline
in an `async def`, the wait is paid by every other command; and once it runs
in a thread, the dict it walks may be rebuilt underneath it by a config save.
"""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace

import pytest

from dpc_client_core.service import CoreService


class Provider:
    def __init__(self, model="m:latest", on_vision=None):
        self.model = model
        self.config = {"type": "ollama"}
        self._on_vision = on_vision

    def supports_vision(self) -> bool:
        if self._on_vision is not None:
            self._on_vision()
        return False


def _fake_service(providers: dict):
    """Enough of CoreService for get_providers_list and nothing more."""
    fake = SimpleNamespace(
        llm_manager=SimpleNamespace(
            providers=providers,
            default_provider="a",
            vision_provider="",
            voice_provider="",
            agent_provider="",
            get_context_window=lambda model: 4096,
        ),
        _provider_supports_voice=lambda provider: False,
    )
    fake._provider_rows = MethodType(CoreService._provider_rows, fake)
    return fake


@pytest.mark.asyncio
async def test_a_slow_provider_does_not_stop_the_event_loop():
    import time as real_time

    fake = _fake_service({"a": Provider(on_vision=lambda: real_time.sleep(0.3))})

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        result = await CoreService.get_providers_list(fake)
    finally:
        beat.cancel()

    assert len(result["providers"]) == 1
    assert ticks > 5, f"the loop ran {ticks} times while a provider blocked for 0.3s"


@pytest.mark.asyncio
async def test_a_config_save_during_the_read_does_not_break_it():
    providers = {"a": Provider(), "b": Provider(), "c": Provider()}

    def rebuild_the_registry():
        # What save_providers_config does: clear and reload.
        providers.clear()
        providers["d"] = Provider()

    providers["a"] = Provider(on_vision=rebuild_the_registry)

    result = await CoreService.get_providers_list(_fake_service(providers))

    assert [p["alias"] for p in result["providers"]] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_the_rows_still_say_what_they_said_before():
    agent = Provider(model="remote:latest")
    agent.peer_id = "dpc-node-abc"
    agent.remote_model = "qwen3.8"
    agent.remote_provider = "llama.cpp"
    fake = _fake_service({"ollama_text": Provider(), "dpc_agent": agent})

    rows = {p["alias"]: p for p in (await CoreService.get_providers_list(fake))["providers"]}

    assert rows["ollama_text"] == {
        "alias": "ollama_text",
        "model": "m:latest",
        "type": "ollama",
        "supports_vision": False,
        "context_window": 4096,
        "supports_voice": False,
    }
    assert rows["dpc_agent"]["peer_id"] == "dpc-node-abc"
    assert rows["dpc_agent"]["remote_model"] == "qwen3.8"
    assert rows["dpc_agent"]["remote_provider"] == "llama.cpp"
