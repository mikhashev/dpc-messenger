"""A client that leaves the door ends its call, and nothing is generated again for nobody.

ADR-041 D4, amendment 2026-11-04. A chunk callback runs inside the provider's
stream, so a write into a closing transport (`ClientConnectionResetError`, an
`OSError`) looked like a provider failure: `_is_retryable` said yes and the
whole answer was made again, per Zcode seven times in 600 s, for a client that
had gone.

Nothing is stood in for between the door and the socket to the engine: a real
`aiohttp` listener, the real `LLMManager`, the real `LlamaServerProvider` with
its real supervisor and the real OpenAI SDK, against `_Engine`, an HTTP server
on port 0 that speaks what the engine speaks. `_Engine`'s frames are checked
against the provider's reader (`generate_response_stream`, `generate_with_tools`)
and against the Strata launcher's writer (`serve/server.py`, v0.2.11, the
`chunk(...)` calls and the final `last["usage"]`), not against this file's asserts.
The client is a raw socket, so leaving is a socket closing and not an SDK's idea
of it. Cross-platform: pure asyncio on loopback.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from openai import AsyncOpenAI

from dpc_client_core import gateway as gateway_module
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.llm_manager import LLMManager
from dpc_client_core.managers.llama_server_supervisor import _CARD_HOLDS, LlamaServerSupervisor
from dpc_client_core.message_handlers.inference_handler import RemoteInferenceChunkHandler
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.providers import LlamaServerProvider
from dpc_client_core.providers import base as provider_base
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.providers.strata_provider import StrataProvider
from dpc_client_core.providers.zai_provider import ZaiProvider
from tests.test_p2p_coordinator import ProvedConnection, make_coordinator
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER,
    REMOTE_MODEL,
    _Connection,
    _peer_service,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    MAX_IMAGE_MB,
    NODE_ID,
    _key,
    _running,
    _write_rules,
)

ALIAS = "local_card"
TOOLS_OPENAI = [{"type": "function", "function": {
    "name": "read_file", "description": "read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
}}]
TOOLS_ANTHROPIC = [{"name": "read_file", "description": "read a file",
                    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]
CHAT_PATH = "/v1/chat/completions"
MESSAGES_PATH = "/v1/messages"
PIECES = tuple(f"piece{i} " for i in range(40))
# aiohttp's own runner, kept from before a test replaces it with one that never
# cancels a handler: the engine double must keep noticing its client leaving.
_RealRunner = web.AppRunner


class _Engine:
    """llama-server's and the launcher's chat route, on a real socket.

    A stream opens with `{"role", "content": ""}`, then one chunk per piece of
    reasoning and of content, a chunk with the finish word carrying `usage`, and
    `[DONE]`. A whole answer is one `message` with `reasoning_content` beside
    `content`, the launcher's shape. `requests` is every body that arrived,
    which is the number this file is about; `left` counts the calls whose client
    closed before the answer ended, which is what a generation being stopped
    looks like from here; `gate` holds the whole call back, `content_gate` the
    stream between its reasoning and its content.
    """

    def __init__(self, *, reasoning=(), content=PIECES, tool_calls=None, delay=0.01, gate=None,
                 content_gate=None):
        self.reasoning = tuple(reasoning)
        self.content = tuple(content)
        self.tool_calls = tool_calls
        self.delay = delay
        self.gate = gate
        self.content_gate = content_gate
        self.requests = []
        self.left = 0
        self.finished = 0
        self.port = 0
        self._runner = None

    async def __aenter__(self):
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._chat)
        self._runner = _RealRunner(app, access_log=None, handler_cancellation=True)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]
        return self

    async def __aexit__(self, *exc):
        await self._runner.cleanup()
        return False

    def _usage(self):
        return {
            "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
            "prompt_tokens_details": {"cached_tokens": 0},
        }

    def _chunk(self, delta, finish=None):
        return {"id": "chatcmpl-engine", "object": "chat.completion.chunk", "created": 1, "model": "m",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    async def _chat(self, request):
        body = await request.json()
        self.requests.append(body)
        try:
            if self.gate is not None:
                await self.gate.wait()
            response = await (self._stream(request) if body.get("stream") else self._whole())
            self.finished += 1
            return response
        except asyncio.CancelledError:
            self.left += 1
            raise

    async def _whole(self):
        await asyncio.sleep(self.delay)
        message = {"role": "assistant", "content": "".join(self.content) or None}
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return web.json_response({
            "id": "chatcmpl-engine", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if self.tool_calls else "stop"}],
            "usage": self._usage(),
        })

    async def _stream(self, request):
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)

        async def send(payload):
            await response.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
            await asyncio.sleep(self.delay)

        await send(self._chunk({"role": "assistant", "content": ""}))
        for piece in self.reasoning:
            await send(self._chunk({"reasoning_content": piece}))
        if self.content_gate is not None:
            await self.content_gate.wait()
        for piece in self.content:
            await send(self._chunk({"content": piece}))
        await send({**self._chunk({}, "stop"), "usage": self._usage()})
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response


def _local_provider(engine, *, alias=ALIAS, provider_class=LlamaServerProvider, **config):
    """The real provider on its real supervisor, with a child that is up: `_proc`
    and `props` are what `ensure_running` asks about, and the port is the double's."""
    provider = provider_class(alias, {"type": "llamacpp_server", "gguf_path": "D:/m/x.gguf",
                                      "context_window": 4096, **config})
    supervisor = provider.supervisor
    assert isinstance(supervisor, LlamaServerSupervisor)
    supervisor._proc = SimpleNamespace(returncode=None, pid=0)
    supervisor.props = {"total_slots": 4}
    supervisor.port = engine.port
    # Spent fast: without this a real backoff budget is ten minutes on a clock
    # that is not faked, and the defect would show only as a slow test.
    provider.max_retry_seconds = 1.5
    return provider


def _forget(provider):
    provider.supervisor._proc = None
    _ACTIVE_SUPERVISORS.pop(provider.alias, None)


def _service(tmp_path, provider, *, compute=None):
    rules = _write_rules(tmp_path, compute or {"serving_local": [provider.alias]})
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": []}), encoding="utf-8")
    manager = LLMManager(config_path=config)
    manager.providers = {provider.alias: provider}
    manager.default_provider = provider.alias
    return SimpleNamespace(
        firewall=ContextFirewall(rules),
        llm_manager=manager,
        p2p_manager=SimpleNamespace(node_id=NODE_ID),
        settings=SimpleNamespace(
            get_remote_inference_timeout=lambda: 30.0,
            get_vision_max_image_size_mb=lambda: MAX_IMAGE_MB,
        ),
    )


@pytest.fixture
def fast_backoff(monkeypatch):
    """The backoff's sleep shortened, so a retry shows at once as a second request."""
    async def _soon(seconds, flag):
        await asyncio.sleep(0.01)

    monkeypatch.setattr(provider_base, "sleep_unless_cancelled", _soon)


@pytest.fixture
def no_handler_cancellation(monkeypatch):
    """The listener without `handler_cancellation`: only the failing write can end
    the call. This is the door Zcode measured, and the race where a closing
    transport refuses a write one loop turn before `connection_lost` arrives."""
    def _runner(app, **kwargs):
        kwargs["handler_cancellation"] = False
        return _RealRunner(app, **kwargs)

    monkeypatch.setattr(gateway_module.web, "AppRunner", _runner)


class _Client:
    """A client on a bare socket, so that leaving is the socket closing."""

    def __init__(self, port, key, path, body):
        self.port, self.key, self.path, self.body = port, key, path, body
        self.reader = self.writer = None

    async def send(self):
        payload = json.dumps(self.body).encode("utf-8")
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        head = (f"POST {self.path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Authorization: Bearer {self.key}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n\r\n").encode("ascii")
        self.writer.write(head + payload)
        await self.writer.drain()

    async def read_until(self, needle, timeout=10.0):
        seen = b""
        async with asyncio.timeout(timeout):
            while needle not in seen:
                data = await self.reader.read(4096)
                assert data, f"the door closed before {needle!r}; it sent {seen[:400]!r}"
                seen += data
        return seen

    async def leave(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except OSError:
            pass


async def _until(condition, timeout=8.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def _settled(engine):
    """Long enough for a retry to reach the engine, given `fast_backoff`."""
    await asyncio.sleep(0.25)
    return len(engine.requests)


def _chat(model=ALIAS, **extra):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}


def _messages(model=ALIAS, **extra):
    return {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}], **extra}


# --- (1) a stream on the local route, both forms --------------------------------------


@pytest.mark.parametrize("path, body, needle", [
    (CHAT_PATH, _chat(stream=True), b"piece1 "),
    (MESSAGES_PATH, _messages(stream=True), b"piece1 "),
], ids=["openai", "messages"])
@pytest.mark.asyncio
async def test_a_stream_client_that_leaves_stops_the_engine_asks_it_once_and_releases_all_it_held(
    tmp_path, fast_backoff, path, body, needle,
):
    lock = asyncio.Semaphore(1)
    async with _Engine() as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider), inference_lock=lock) as (server, ledger):
                client = _Client(server.port, _key(tmp_path), path, body)
                await client.send()
                await client.read_until(needle)
                await client.leave()
                await _until(lambda: engine.left or engine.finished)
                assert await _settled(engine) == 1, "the answer was generated again for nobody"
                assert engine.left == 1 and engine.finished == 0, "the engine was left generating"
                assert not lock.locked(), "the card's semaphore is still held"
                assert provider.supervisor._in_flight == 0, "the call slot is still held"
                assert not _CARD_HOLDS.get(ALIAS), "the card hold outlived the call"
                (row,) = list(ledger.rows())
                assert row["counts_source"] == "ours", "what was made is counted here, once"
        finally:
            _forget(provider)


@pytest.mark.parametrize("path, body, needle", [
    (CHAT_PATH, _chat(stream=True), b"piece1 "),
    (MESSAGES_PATH, _messages(stream=True), b"piece1 "),
], ids=["openai", "messages"])
@pytest.mark.asyncio
async def test_a_write_into_a_closing_transport_is_not_read_as_a_provider_failure(
    tmp_path, fast_backoff, no_handler_cancellation, path, body, needle,
):
    """The reproduction. Red at the commit before the fix: two requests or more."""
    lock = asyncio.Semaphore(1)
    async with _Engine(delay=0.02) as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider), inference_lock=lock) as (server, _):
                client = _Client(server.port, _key(tmp_path), path, body)
                await client.send()
                await client.read_until(needle)
                await client.leave()
                # The lock is taken for as long as the call runs, retries and all.
                await _until(lambda: not lock.locked())
                assert await _settled(engine) == 1, "the answer was generated again for nobody"
                assert engine.left == 1, "the stream to the engine was not closed"
                assert provider.supervisor._in_flight == 0
        finally:
            _forget(provider)


@pytest.mark.asyncio
async def test_the_tools_path_generates_once_when_the_write_after_it_finds_the_client_gone(
    tmp_path, fast_backoff, no_handler_cancellation,
):
    """The path that made the whole answer again: a tools call was not a stream, so
    the first write came after the engine had finished, and the failure of that write
    threw the finished answer away and asked for it again. On a vendor's class it is
    still not a stream; on this class the first write comes with the first delta."""
    gate = asyncio.Event()
    lock = asyncio.Semaphore(1)
    async with _Engine(gate=gate, content=("the whole answer",)) as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider), inference_lock=lock) as (server, _):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH,
                                 _chat(stream=True, tools=TOOLS_OPENAI))
                await client.send()
                await _until(lambda: engine.requests)
                await client.leave()
                await asyncio.sleep(0.05)
                gate.set()
                await _until(lambda: not lock.locked())
                assert await _settled(engine) == 1, "the answer was generated again for nobody"
        finally:
            _forget(provider)


# --- (2) a generation under way, which no write has caught yet ------------------------


@pytest.mark.asyncio
async def test_the_last_frame_finding_the_client_gone_is_the_same_ending_and_raises_nothing():
    """The whole answer was written and the client left before the end of the body:
    a `ConnectionError` out of the handler would reach aiohttp as a 500 to log."""
    async def _gone():
        raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")

    async def _handler():
        stream = gateway_module._EventStream(SimpleNamespace())
        stream.response = SimpleNamespace(write_eof=_gone)
        await stream.close()
        await asyncio.sleep(5)

    task = asyncio.create_task(_handler())
    await asyncio.wait({task}, timeout=2)
    assert task.cancelled(), f"the handler was not ended: {task}"


@pytest.mark.asyncio
async def test_a_call_ended_this_way_says_so_on_this_machine(tmp_path, fast_backoff, caplog):
    async with _Engine() as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, _):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(stream=True))
                with caplog.at_level("INFO", logger="dpc_client_core.gateway"):
                    await client.send()
                    await client.read_until(b"piece1 ")
                    await client.leave()
                    await _until(lambda: engine.left)
                    await asyncio.sleep(0.05)
            assert f"gateway POST {CHAT_PATH} ended before its answer" in caplog.text
        finally:
            _forget(provider)


@pytest.mark.parametrize("path, body", [
    (CHAT_PATH, _chat()),
    (CHAT_PATH, _chat(stream=True, tools=TOOLS_OPENAI)),
    (CHAT_PATH, _chat(stream=True)),
    (MESSAGES_PATH, _messages()),
    (MESSAGES_PATH, _messages(stream=True, tools=TOOLS_ANTHROPIC)),
    (MESSAGES_PATH, _messages(stream=True)),
], ids=["chat-whole", "chat-tools-stream", "chat-silent-stream",
        "messages-whole", "messages-tools-stream", "messages-silent-stream"])
@pytest.mark.asyncio
async def test_a_client_that_leaves_before_any_byte_stops_the_generation_at_the_engine(
    tmp_path, fast_backoff, path, body,
):
    """No write would ever catch these: a client gone during the reasoning, or from
    a call that writes only at its end."""
    gate = asyncio.Event()
    lock = asyncio.Semaphore(1)
    async with _Engine(gate=gate) as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider), inference_lock=lock) as (server, _):
                client = _Client(server.port, _key(tmp_path), path, body)
                await client.send()
                await _until(lambda: engine.requests)
                await client.leave()
                await _until(lambda: engine.left)
                assert engine.finished == 0
                assert await _settled(engine) == 1
                assert not lock.locked() and provider.supervisor._in_flight == 0
        finally:
            gate.set()
            _forget(provider)


@pytest.mark.asyncio
async def test_a_client_that_leaves_while_waiting_for_the_semaphore_never_reaches_the_engine(tmp_path, fast_backoff):
    lock = asyncio.Semaphore(1)
    await lock.acquire()
    async with _Engine() as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider), inference_lock=lock) as (server, ledger):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(stream=True))
                await client.send()
                await asyncio.sleep(0.1)
                await client.leave()
                await asyncio.sleep(0.1)
                lock.release()
                assert await _settled(engine) == 0, "a gone client's call took the card"
                assert not lock.locked(), "the waiter it left behind took the semaphore"
                assert list(ledger.rows()) == [], "a call that never reached a provider left a row"
        finally:
            _forget(provider)


# --- (3) what stays as it was ---------------------------------------------------------


@pytest.mark.parametrize("provider_class", [
    provider_base.AIProvider, DeepSeekProvider, LlamaServerProvider, StrataProvider, ZaiProvider,
])
def test_a_providers_own_connection_failure_is_still_retryable(provider_class):
    """The fix is at the door: `_is_retryable` is not narrowed for what a provider
    itself raises, in the base or in any class that overrides it."""
    assert provider_class._is_retryable(ConnectionResetError("the engine went away"))
    assert provider_class._is_retryable(OSError("the engine went away"))


@pytest.mark.asyncio
async def test_with_the_client_still_there_a_connection_failure_of_the_engine_is_retried(tmp_path, fast_backoff):
    answer = "".join(PIECES[:2])
    async with _Engine(content=PIECES[:2]) as engine:
        provider = _local_provider(engine)
        real_ensure = provider._ensure
        failures = [ConnectionResetError("the engine went away")]

        async def ensure():
            if failures:
                raise failures.pop()
            return await real_ensure()

        provider._ensure = ensure
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, _):
                url = f"http://127.0.0.1:{server.port}{CHAT_PATH}"
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=_chat(stream=True),
                                            headers={"x-api-key": _key(tmp_path)}) as response:
                        text = await response.text()
                deltas = [json.loads(line[6:]) for line in text.split("\n")
                          if line.startswith("data: {")]
                got = "".join(d["choices"][0]["delta"].get("content", "") for d in deltas if d["choices"])
                assert got == answer
                assert engine.finished == 1
        finally:
            _forget(provider)


@pytest.mark.asyncio
async def test_a_client_that_stays_gets_the_whole_stream_and_one_row(tmp_path):
    """A connection that does not close is cancelled by nothing."""
    async with _Engine(content=PIECES[:3]) as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, ledger):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(stream=True))
                await client.send()
                seen = await client.read_until(b"[DONE]")
                await client.leave()
                assert b"piece2 " in seen
                assert engine.finished == 1 and engine.left == 0
                (row,) = list(ledger.rows())
                assert row["caller_kind"] == "gateway"
        finally:
            _forget(provider)


# --- (4) the peer route: the guest's door ---------------------------------------------


def _guest_door(tmp_path):
    """The door's stand-in on the guest, with the real coordinator's
    `request_inference_from_peer` and the real chunk handler behind it."""
    service = _peer_service(tmp_path, connected=True)
    coordinator, wire = make_coordinator({PEER: _Connection(PEER)})
    wire._pending_inference_chunks = {}
    service.p2p_coordinator = coordinator
    return service, wire, RemoteInferenceChunkHandler(wire)


@pytest.mark.parametrize("handler_cancellation", [True, False], ids=["connection-lost", "failing-write"])
@pytest.mark.asyncio
async def test_a_guest_whose_client_leaves_stops_waiting_for_the_host_and_writes_no_row(
    tmp_path, monkeypatch, handler_cancellation,
):
    if not handler_cancellation:
        monkeypatch.setattr(gateway_module.web, "AppRunner",
                            lambda app, **kw: _RealRunner(app, **{**kw, "handler_cancellation": False}))
    service, wire, chunks = _guest_door(tmp_path)
    service.settings.get_remote_inference_timeout = lambda: 30.0
    async with _running(tmp_path, service) as (server, ledger):
        client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(REMOTE_MODEL, stream=True))
        await client.send()
        await _until(lambda: wire.p2p_manager.send_message_to_peer.await_count)
        frame = wire.p2p_manager.send_message_to_peer.await_args.args[1]
        request_id = frame["payload"]["request_id"]

        def chunk(seq, text):
            # A task of its own, as `p2p_manager` gives every frame it reads.
            return asyncio.create_task(chunks.handle(PEER, {"request_id": request_id, "seq": seq, "delta": text}))

        await chunk(0, "piece0 ")
        await client.read_until(b"piece0 ")
        await client.leave()
        await asyncio.sleep(0.05)
        seq = 1
        while wire._pending_inference_requests and seq < 200:
            await chunk(seq, "piece ")
            seq += 1
            await asyncio.sleep(0.01)
        assert wire._pending_inference_requests == {}, "the guest is still waiting for a client that left"
        assert wire._pending_inference_chunks == {}
        # What the host goes on sending finds nothing and raises nothing.
        await chunk(seq, "late ")
        assert list(ledger.rows()) == [], "the guest's half of a call it stopped reading"


# --- (5) the peer route: the host's own write to a guest that is gone ------------------


def _host(tmp_path, provider):
    coordinator, wire = make_coordinator({"peer-1": ProvedConnection("peer-1")})
    wire.firewall.can_request_inference.return_value = True
    wire.firewall.compute_serving_alias = provider.alias
    wire.firewall.compute_serving_local = [provider.alias]
    wire.firewall.compute_serving_vendor = []
    coordinator._ledger = NodeLedger(tmp_path / "ledger")
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": []}), encoding="utf-8")
    manager = LLMManager(config_path=config)
    manager.providers = {provider.alias: provider}
    manager.default_provider = provider.alias
    wire.llm_manager = manager
    return coordinator, wire


@pytest.mark.asyncio
async def test_a_host_whose_guest_is_gone_ends_the_call_once_and_frees_the_lock(tmp_path, fast_backoff):
    """The same shape one hop on: the chunk sender runs inside the provider's stream
    and a dead connection raises `ConnectionError` from it."""
    async with _Engine(delay=0.02) as engine:
        provider = _local_provider(engine)
        coordinator, wire = _host(tmp_path, provider)
        sent = []

        async def send(peer_id, frame):
            sent.append(frame)
            if frame["command"] == "REMOTE_INFERENCE_CHUNK" and len(sent) >= 2:
                raise ConnectionError(f"Not connected to peer {peer_id}.")

        wire.p2p_manager.send_message_to_peer = send
        try:
            # A task of its own, as `p2p_manager` gives every frame it reads: the
            # call ends by cancelling the task that serves this one request.
            task = asyncio.create_task(coordinator.handle_inference_request(
                "peer-1", "req-1", "hi", provider=provider.alias,
                messages=[{"role": "user", "content": "hi"}], stream=True,
            ))
            await _until(task.done)
            assert task.cancelled(), f"the host did not end the call: {task}"
            assert await _settled(engine) == 1, "the host generated the answer again for a gone guest"
            assert engine.left == 1
            assert not coordinator._peer_inference_lock.locked()
            assert coordinator._in_flight_requests == {}, "the request id stays in flight for ever"
            assert provider.supervisor._in_flight == 0
            assert [f["command"] for f in sent].count("REMOTE_INFERENCE_RESPONSE") == 0
        finally:
            _forget(provider)


# --- (6) a call that ended this way still counts --------------------------------------


GUEST = "dpc-node-" + "c" * 32
VENDOR_ALIAS = "ds_flash"


def _vendor_provider(engine, monkeypatch):
    """The real class and the real SDK, its client aimed at the double."""
    monkeypatch.setenv("TEST_PROVIDER_KEY", "not-a-real-key")
    provider = DeepSeekProvider(VENDOR_ALIAS, {"type": "deepseek", "api_key_env": "TEST_PROVIDER_KEY",
                                               "model": "deepseek-v4-flash"})
    provider.client = AsyncOpenAI(api_key="k", base_url=f"http://127.0.0.1:{engine.port}/v1")
    provider.max_retry_seconds = 1.5
    provider.get_balance = None
    return provider


def _vendor_host(tmp_path, provider, ceiling):
    rules = _write_rules(tmp_path, {"allow_nodes": [GUEST], "serving_vendor": [VENDOR_ALIAS],
                                    "vendor_quotas": {VENDOR_ALIAS: ceiling}})
    coordinator, wire = make_coordinator({GUEST: ProvedConnection(GUEST)})
    wire.firewall = ContextFirewall(rules)
    wire.gateway = None
    coordinator._ledger = NodeLedger(tmp_path / "ledger")
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": []}), encoding="utf-8")
    manager = LLMManager(config_path=config)
    manager.providers = {provider.alias: provider}
    manager.default_provider = provider.alias
    wire.llm_manager = manager
    return coordinator, wire


async def _guest_takes_a_chunk_and_drops_the_link(coordinator, wire, request_id):
    sent = []

    async def send(peer_id, frame):
        sent.append(frame)
        if frame["command"] == "REMOTE_INFERENCE_CHUNK" and len(sent) >= 2:
            raise ConnectionError(f"Not connected to peer {peer_id}.")

    wire.p2p_manager.send_message_to_peer = send
    task = asyncio.create_task(coordinator.handle_inference_request(
        GUEST, request_id, "[USER]\nhi", provider=VENDOR_ALIAS,
        messages=[{"role": "user", "content": "hi"}], stream=True,
    ))
    await _until(task.done, timeout=15.0)
    return task, sent


@pytest.mark.asyncio
async def test_a_guest_that_drops_the_link_is_charged_for_what_was_made_and_stops_at_its_ceiling(
    tmp_path, fast_backoff, monkeypatch,
):
    """The sequence the ceiling exists for: send, take the first chunk, drop, repeat.
    The row says what is known, in the ledger's own words: the host counted it."""
    thinking = tuple(f"thought{i} " for i in range(30))
    async with _Engine(reasoning=thinking, content=PIECES, delay=0.001) as engine:
        provider = _vendor_provider(engine, monkeypatch)
        coordinator, wire = _vendor_host(tmp_path, provider, ceiling=0.0000001)

        task, _ = await _guest_takes_a_chunk_and_drops_the_link(coordinator, wire, "req-1")
        assert task.cancelled()
        (row,) = list(coordinator._ledger.rows())
        assert row["request_id"] == "req-1" and row["caller"] == GUEST and row["alias"] == VENDOR_ALIAS
        assert row["counts_source"] == "ours", "this node counted it; no engine reported"
        assert row["prompt_tokens"] > 0
        assert row["thinking_tokens"] > 0 and row["thinking_source"] == "estimated"
        assert row["output_includes_thinking"] == "includes"
        assert row["completion_tokens"] > row["thinking_tokens"], "the output holds the reasoning and the text"
        assert row["cost_amount"] > 0 and row["cost_currency"] == "USD", "the money reached the row"
        assert coordinator._ledger.spent_today(VENDOR_ALIAS, caller=GUEST, caller_kind="peer") > 0

        made = len(engine.requests)
        _, sent = await _guest_takes_a_chunk_and_drops_the_link(coordinator, wire, "req-2")
        assert len(engine.requests) == made, "the ceiling did not stop the second call"
        (refused,) = [f for f in sent if f["command"] == "REMOTE_INFERENCE_RESPONSE"]
        assert refused["payload"]["code"] == "insufficient_quota"
        assert len(list(coordinator._ledger.rows())) == 1, "a refused call left a row"


@pytest.mark.asyncio
async def test_a_host_call_seen_through_writes_the_engines_counts_and_one_row(tmp_path, fast_backoff, monkeypatch):
    async with _Engine(content=PIECES[:2]) as engine:
        provider = _vendor_provider(engine, monkeypatch)
        coordinator, wire = _vendor_host(tmp_path, provider, ceiling=2.0)
        wire.p2p_manager.send_message_to_peer = AsyncMock()
        await asyncio.create_task(coordinator.handle_inference_request(
            GUEST, "req-1", "[USER]\nhi", provider=VENDOR_ALIAS,
            messages=[{"role": "user", "content": "hi"}], stream=True,
        ))
        (row,) = list(coordinator._ledger.rows())
        assert row["counts_source"] == "engine", "a finished call has the engine's counts"
        assert row["prompt_tokens"] == 11


def _vendor_door_service(tmp_path, provider, ceiling=2.0):
    return _service(tmp_path, provider, compute={"serving_vendor": [provider.alias],
                                                 "vendor_quotas": {provider.alias: ceiling}})


@pytest.mark.parametrize("path, body", [
    (CHAT_PATH, _chat(VENDOR_ALIAS, stream=True)),
    (MESSAGES_PATH, _messages(VENDOR_ALIAS, stream=True)),
], ids=["openai", "messages"])
@pytest.mark.asyncio
async def test_a_door_call_on_a_vendor_alias_counts_what_was_seen_on_either_form(
    tmp_path, fast_backoff, monkeypatch, path, body,
):
    """The Messages form hands no reasoning to its client, and it was still paid for."""
    thinking = tuple(f"thought{i} " for i in range(30))
    async with _Engine(reasoning=thinking, delay=0.005) as engine:
        provider = _vendor_provider(engine, monkeypatch)
        async with _running(tmp_path, _vendor_door_service(tmp_path, provider)) as (server, ledger):
            client = _Client(server.port, _key(tmp_path), path, body)
            await client.send()
            await client.read_until(b"piece1 ")
            await client.leave()
            await _until(lambda: list(ledger.rows()))
            (row,) = list(ledger.rows())
            assert row["caller_kind"] == "gateway" and row["counts_source"] == "ours"
            assert row["thinking_tokens"] > 0 and row["thinking_source"] == "estimated"
            assert row["completion_tokens"] > row["thinking_tokens"], "the pieces seen after the reasoning count too"
            assert row["cost_amount"] > 0
            assert ledger.spent_today(VENDOR_ALIAS, caller=NODE_ID, caller_kind="gateway") > 0


@pytest.mark.parametrize("extra", [{}, {"stream": True, "tools": TOOLS_OPENAI}], ids=["whole", "tools-stream"])
@pytest.mark.asyncio
async def test_a_door_client_gone_before_any_answer_on_a_vendor_alias_is_counted_for_its_prompt(
    tmp_path, fast_backoff, monkeypatch, extra,
):
    """Nothing came back, and the vendor had the prompt."""
    gate = asyncio.Event()
    async with _Engine(gate=gate) as engine:
        provider = _vendor_provider(engine, monkeypatch)
        try:
            async with _running(tmp_path, _vendor_door_service(tmp_path, provider)) as (server, ledger):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(VENDOR_ALIAS, **extra))
                await client.send()
                await _until(lambda: engine.requests)
                await client.leave()
                await _until(lambda: list(ledger.rows()))
                (row,) = list(ledger.rows())
                assert row["prompt_tokens"] > 0 and row["completion_tokens"] == 0
                assert row["counts_source"] == "ours" and row["cost_amount"] > 0
        finally:
            gate.set()


@pytest.mark.asyncio
async def test_a_local_call_gone_before_any_answer_leaves_no_row_and_one_with_an_answer_does(tmp_path, fast_backoff):
    """No money and nothing known is no record of anything; what was made is a record."""
    gate = asyncio.Event()
    async with _Engine(gate=gate) as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, ledger):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(stream=True))
                await client.send()
                await _until(lambda: engine.requests)
                await client.leave()
                await _until(lambda: engine.left)
                await asyncio.sleep(0.05)
                assert list(ledger.rows()) == []
        finally:
            gate.set()
            _forget(provider)
    async with _Engine() as engine:
        provider = _local_provider(engine)
        try:
            async with _running(tmp_path, _service(tmp_path, provider)) as (server, ledger):
                client = _Client(server.port, _key(tmp_path), CHAT_PATH, _chat(stream=True))
                await client.send()
                await client.read_until(b"piece1 ")
                await client.leave()
                await _until(lambda: list(ledger.rows()))
                (row,) = list(ledger.rows())
                assert row["counts_source"] == "ours" and row["completion_tokens"] > 0
                assert row["cost_amount"] == 0.0
        finally:
            _forget(provider)


@pytest.mark.asyncio
async def test_neuraldeep_prices_the_estimate_from_its_own_list_so_a_wallet_ceiling_moves(tmp_path):
    from dpc_client_core.providers.neuraldeep_provider import NeuralDeepProvider

    provider = NeuralDeepProvider("nd", {"api_key": "k", "model": "qwen3.8-27b"})

    async def priced(usage, model):
        usage["cost_amount"] = 0.5 + usage["prompt_tokens"] / 1e6
        usage["cost_currency"] = "RUB"
        usage["cost_basis"] = "charged"

    provider._price_usage = priced
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"providers": []}), encoding="utf-8")
    manager = LLMManager(config_path=config)
    manager.providers = {"nd": provider}
    result = await manager.estimated_usage("nd", "the whole prompt", "made", "thought")
    assert result["cost_currency"] == "RUB" and result["cost_basis"] == "charged" and result["cost_amount"] > 0.5
    assert result["counts_source"] == "ours" and result["output_includes_thinking"] == "includes"
