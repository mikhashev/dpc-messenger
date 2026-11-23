"""A tool that runs on its own event loop gets its own provider client.

An async agent tool runs its handler on a fresh loop in a worker thread
(tools/registry.py). The providers kept one `AsyncOpenAI` and reused it from every
loop, so `describe_image` sent its request on the tool's loop over a connection the
service loop had opened: llama-server answered in 12 s and the tool timed out at
300 s (2026-11-23); the next time the read died with ConnectionResetError after 88 s
and a retry on a fresh connection answered in 6 s. The owner's fix (2026-11-23): one
client per event loop (`providers/loop_clients.py`), in every provider that caches one.

Everything here is offline: a stdlib HTTP/1.1 keep-alive server on 127.0.0.1 stands
in for the engine, and every wait is bounded by a few seconds.
"""
from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI

from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolEntry, ToolRegistry
from dpc_client_core.providers import llamacpp_server_provider as lsp
from dpc_client_core.providers.anthropic_provider import AnthropicProvider
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.loop_clients import LoopClients
from dpc_client_core.providers.neuraldeep_provider import NeuralDeepProvider
from dpc_client_core.providers.openai_provider import OpenAICompatibleProvider
from dpc_client_core.providers.strata_provider import StrataProvider
from dpc_client_core.providers.zai_provider import ZaiProvider

WITHIN = 3.0  # an answer from the stub takes milliseconds; a hang takes the whole bound

_BODY = json.dumps({
    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}).encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, as llama-server

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_BODY)))
        self.end_headers()
        self.wfile.write(_BODY)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_port():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()


def _openai_at(port):
    return AsyncOpenAI(api_key="local", base_url=f"http://127.0.0.1:{port}/v1", max_retries=0)


async def _ask(client):
    reply = await asyncio.wait_for(
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}]),
        WITHIN,
    )
    return reply.choices[0].message.content


def _on_a_tool_loop(coro_factory):
    """What registry.py does: a new loop, in a worker thread, closed afterwards."""
    def run():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro_factory())
        finally:
            loop.close()
            asyncio.set_event_loop(None)
    return run


def _service_then_tool_then_service(get_client):
    """The call from the service loop, the call from a tool's loop, the service loop again.
    `get_client` is an async callable answering the client for the running loop."""
    async def ask_here():
        return await _ask(await get_client())

    async def main():
        out = [await ask_here()]
        with ThreadPoolExecutor(1) as pool:
            out.append(await asyncio.get_running_loop().run_in_executor(
                pool, _on_a_tool_loop(ask_here)))
        out.append(await ask_here())
        return out

    return asyncio.run(main())


# --- (a) the reproduction -------------------------------------------------------

def test_a_client_per_loop_answers_on_the_service_loop_the_tool_loop_and_the_service_loop_again(stub_port):
    clients = LoopClients(lambda: _openai_at(stub_port))

    async def get():
        return clients.get()

    assert _service_then_tool_then_service(get) == ["ok", "ok", "ok"]


def test_a_real_provider_answers_from_a_tool_loop_over_the_client_the_service_loop_used(stub_port):
    provider = OpenAICompatibleProvider("o", {
        "model": "m", "api_key": "k", "base_url": f"http://127.0.0.1:{stub_port}/v1", "max_retries": 0,
    })

    async def get():
        return provider.client

    assert _service_then_tool_then_service(get) == ["ok", "ok", "ok"]


@pytest.mark.parametrize("engine", [lsp.LlamaServerProvider, StrataProvider], ids=["llama", "strata"])
def test_the_engine_provider_answers_from_a_tool_loop_through_its_own_ensure(stub_port, monkeypatch, engine):
    provider = _engine(engine, stub_port, monkeypatch)

    async def get():
        return await provider._ensure()

    assert _service_then_tool_then_service(get) == ["ok", "ok", "ok"]


# --- (b) through the registry ----------------------------------------------------

def test_a_tool_the_registry_runs_on_its_own_loop_reaches_the_engine_and_the_service_loop_goes_on(stub_port, tmp_path):
    provider = OpenAICompatibleProvider("o", {
        "model": "m", "api_key": "k", "base_url": f"http://127.0.0.1:{stub_port}/v1", "max_retries": 0,
    })
    registry = ToolRegistry(agent_root=tmp_path)
    ctx = ToolContext(agent_root=tmp_path)
    registry.set_context(ctx)

    async def handler(ctx, **kwargs):
        return await _ask(provider.client)

    registry.register(ToolEntry(
        name="describe_like",
        schema={"name": "describe_like", "description": "", "parameters": {"type": "object", "properties": {}}},
        handler=handler,
    ))

    async def main():
        first = await _ask(provider.client)  # the turn, on the service loop
        with ThreadPoolExecutor(1) as pool:  # execute() is called from a worker thread
            tool = await asyncio.get_running_loop().run_in_executor(
                pool, lambda: registry.execute("describe_like", {}, ctx=ctx))
        return [first, tool, await _ask(provider.client)]

    assert asyncio.run(main()) == ["ok", "ok", "ok"]


def test_the_registry_closes_the_client_its_tool_loop_opened_before_the_loop_ends(tmp_path):
    class _Client:
        closed_on = None

        async def close(self):
            _Client.closed_on = asyncio.get_running_loop()

    clients = LoopClients(_Client)
    registry = ToolRegistry(agent_root=tmp_path)
    ctx = ToolContext(agent_root=tmp_path)
    seen = {}

    async def handler(ctx, **kwargs):
        seen["client"] = clients.get()
        seen["loop"] = asyncio.get_running_loop()
        return "done"

    registry.register(ToolEntry(
        name="opens_a_client",
        schema={"name": "opens_a_client", "description": "", "parameters": {"type": "object", "properties": {}}},
        handler=handler,
    ))
    assert registry.execute("opens_a_client", {}, ctx=ctx) == "done"
    assert _Client.closed_on is seen["loop"], "closed on the loop that opened it, not left to die with it"
    assert len(clients._by_loop) == 0, "a finished tool loop does not stay in the helper"


# --- (c) the helper and every provider -------------------------------------------

def _in_two_loops(get):
    """(client on A, client on A again, client on B), loops kept open until all are read."""
    loop_a, loop_b = asyncio.new_event_loop(), asyncio.new_event_loop()
    try:
        async def one():
            return await get()
        return (loop_a.run_until_complete(one()), loop_a.run_until_complete(one()),
                loop_b.run_until_complete(one()))
    finally:
        loop_a.close()
        loop_b.close()


def test_two_loops_get_two_clients_and_one_loop_gets_one():
    clients = LoopClients(object)

    async def get():
        return clients.get()

    on_a, on_a_again, on_b = _in_two_loops(get)
    assert on_a is on_a_again
    assert on_b is not on_a


def test_a_client_made_with_no_loop_running_is_adopted_by_the_first_loop_that_asks():
    clients = LoopClients(object)
    before = clients.get()  # a constructor, sync code

    async def get():
        return clients.get()

    on_a, _, on_b = _in_two_loops(get)
    assert on_a is before
    assert on_b is not before


def test_sync_code_reads_the_client_a_loop_adopted_after_that_loop_has_ended():
    clients = LoopClients(object)
    before = clients.get()

    async def get():
        return clients.get()

    assert asyncio.run(get()) is before
    assert clients.get() is before  # a test configures `provider.client.x` outside any loop


def test_a_client_of_a_closed_loop_is_dropped_at_the_next_lookup_without_being_awaited():
    clients = LoopClients(object)

    async def get():
        return clients.get()

    loop_a = asyncio.new_event_loop()
    loop_a.run_until_complete(get())
    assert len(clients._by_loop) == 1
    loop_a.close()
    loop_b = asyncio.new_event_loop()
    try:
        loop_b.run_until_complete(get())
    finally:
        loop_b.close()
    assert list(clients._by_loop.keys()) == [loop_b]


def test_a_client_assigned_to_a_provider_answers_for_every_loop(monkeypatch):
    provider = OpenAICompatibleProvider("o", {"model": "m", "api_key": "k"})
    double = SimpleNamespace(name="double")
    provider.client = double  # what the suite's mocks do

    async def get():
        return provider.client

    assert all(c is double for c in _in_two_loops(get))


def _anthropic(monkeypatch):
    monkeypatch.setenv("DPC_TEST_LOOP_KEY", "k")
    return AnthropicProvider("a", {"model": "claude-sonnet-4-20250514", "api_key_env": "DPC_TEST_LOOP_KEY"})


_CACHING = {
    "anthropic": _anthropic,
    "deepseek": lambda mp: DeepSeekProvider("d", {"model": "deepseek-v4-flash", "api_key": "k"}),
    "neuraldeep": lambda mp: NeuralDeepProvider("n", {"model": "m", "api_key": "k"}),
    "openai": lambda mp: OpenAICompatibleProvider("o", {"model": "gpt-4o", "api_key": "k"}),
    "zai": lambda mp: ZaiProvider("z", {"model": "glm-4.6", "api_key": "k"}),
}


@pytest.mark.parametrize("name", sorted(_CACHING))
def test_each_provider_that_caches_a_client_hands_each_loop_its_own(monkeypatch, name):
    provider = _CACHING[name](monkeypatch)

    async def get():
        return provider.client

    on_a, on_a_again, on_b = _in_two_loops(get)
    assert on_a is on_a_again
    assert on_b is not on_a
    assert type(on_b) is type(on_a)


def _engine(cls, port, monkeypatch):
    """An engine provider with a supervisor double: no child, no card, only the port."""
    provider = cls.__new__(cls)
    provider.alias = "engine"
    provider.supervisor = SimpleNamespace(port=port)
    provider._port = None

    async def nothing(self, *args, **kwargs):
        return {}

    for step in ("_ensure_running", "_wait_for_the_slot", "_note_engine_call"):
        monkeypatch.setattr(cls, step, nothing)
    return provider


@pytest.mark.parametrize("engine", [lsp.LlamaServerProvider, StrataProvider], ids=["llama", "strata"])
def test_a_port_change_replaces_the_engine_client_of_every_loop(monkeypatch, engine):
    provider = _engine(engine, 5001, monkeypatch)

    async def get():
        return await provider._ensure()

    loop_a, loop_b = asyncio.new_event_loop(), asyncio.new_event_loop()
    try:
        a1 = loop_a.run_until_complete(get())
        b1 = loop_b.run_until_complete(get())
        assert a1 is loop_a.run_until_complete(get())
        assert b1 is not a1
        assert ":5001/" in str(a1.base_url) and ":5001/" in str(b1.base_url)

        provider.supervisor.port = 5002
        a2 = loop_a.run_until_complete(get())
        b2 = loop_b.run_until_complete(get())
        assert a2 is not a1 and b2 is not b1 and a2 is not b2
        assert ":5002/" in str(a2.base_url) and ":5002/" in str(b2.base_url)
    finally:
        loop_a.close()
        loop_b.close()


@pytest.mark.parametrize("engine", [lsp.LlamaServerProvider, StrataProvider], ids=["llama", "strata"])
def test_a_client_an_engine_test_pins_answers_while_its_port_is_current(monkeypatch, engine):
    provider = _engine(engine, 5001, monkeypatch)
    double = SimpleNamespace(name="double")
    provider._port = 5001
    provider._openai = double

    async def get():
        return await provider._ensure()

    assert all(c is double for c in _in_two_loops(get))
    provider._openai = None
    assert provider._openai is None
