"""The gateway routes a `remote:<node>:<alias>` model to a connected peer.

ADR-041 D4 step 4: a colleague on another DPC node points his IDE at his own
loopback gateway and gets this node's model. On the consumer's side the
gateway resolves the name, checks that the peer is connected on a connection
whose key was proved (D2 — direct TLS only; WebRTC, relay and gossip prove no
sender), checks that the peer's menu carries the alias, and makes one
`request_inference_from_peer` call. What comes back whole is answered in the
OpenAI or the Messages shape, and one requester row is written under the
wire's `request_id` (D3): the host's counts, the host's billing model and the
host's cost, copied or left absent — this node did not run the call and does
not price it.

The peer side is a fake `p2p_coordinator.request_inference_from_peer`
returning the result dict `RemoteInferenceResponseHandler` builds, a fake
`p2p_manager.peers` holding `PeerConnection`-like objects with a
`connection_type`, and `service.peer_metadata` seeded with the provider rows
a `PROVIDERS_RESPONSE` leaves. The listener is a real `aiohttp` `TCPSite` on
port 0; the stand-in service, the running listener and the key are the ones
the OpenAI-shape test file builds. Cross-platform: pure asyncio.
"""

import asyncio
import json
import types

import aiohttp
import pytest

from dpc_client_core.node_ledger import NodeLedger
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    BOTH_LISTS,
    LOCAL,
    NODE_ID,
    VENDOR,
    _chat,
    _key,
    _request,
    _running,
    _service,
)
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _anthropic_error,
    _messages,
    _post_messages,
    _sse_events,
)

PEER = "dpc-node-" + "c" * 32
SILENT_PEER = "dpc-node-" + "d" * 32  # connected, sent no PROVIDERS_RESPONSE
GONE_PEER = "dpc-node-" + "e" * 32  # sent one once, no longer connected
REMOTE_ALIAS = "mythos"
REMOTE_VISION_ALIAS = "mythos_vision"
HOST_MODEL = "qwen3.8-27b-mythos"
REMOTE_MODEL = f"remote:{PEER}:{REMOTE_ALIAS}"
PEER_ANSWER = "hello from the peer's card"
WIRE_ID = "req-from-the-wire"
OPENAI_PROMPT = "[SYSTEM]\nbe brief\n\n[USER]\nhi"

PEER_ROWS = [
    {"alias": REMOTE_ALIAS, "model": HOST_MODEL, "type": "llamacpp_server",
     "supports_vision": False, "context_window": 32768},
    {"alias": REMOTE_VISION_ALIAS, "model": HOST_MODEL, "type": "llamacpp_server",
     "supports_vision": True, "context_window": 32768},
]


def _priced_result(**extra):
    """What the response handler hands the requester when the host priced the call."""
    result = {
        "request_id": WIRE_ID, "response": PEER_ANSWER,
        "tokens_used": 30, "model_max_tokens": 32768, "prompt_tokens": 20, "response_tokens": 10,
        "model": HOST_MODEL, "provider": REMOTE_ALIAS, "thinking": None, "thinking_tokens": None,
        "cost_usd": 0.0041, "billing": "pay_per_use",
    }
    result.update(extra)
    return result


def _unpriced_result():
    """A host that counted tokens and sent no price and no billing model."""
    result = _priced_result()
    del result["cost_usd"], result["billing"]
    return result


class _Connection:
    """What the gateway reads off `p2p_manager.peers[peer_id]`."""

    def __init__(self, node_id, connection_type="direct_tls"):
        self.node_id = node_id
        self.connection_type = connection_type


def _peer_service(tmp_path, compute=BOTH_LISTS, *, result=None, fail=None,
                  connection_type="direct_tls", connected=True):
    """The OpenAI-shape stand-in, grown a P2P side: one proved peer serving two
    aliases, one connected peer that sent no menu, one menu whose peer is gone."""
    service = _service(tmp_path, compute)
    peers = {SILENT_PEER: _Connection(SILENT_PEER)}
    if connected:
        peers[PEER] = _Connection(PEER, connection_type)
    service.p2p_manager.peers = peers
    service.peer_metadata = {
        PEER: {"name": "the colleague", "providers": PEER_ROWS},
        SILENT_PEER: {"name": "quiet"},
        GONE_PEER: {"name": "gone", "providers": PEER_ROWS},
    }
    peer_calls = []

    async def request_inference_from_peer(peer_id, prompt, model=None, provider=None,
                                          images=None, reasoning_effort=None, timeout=1200.0):
        peer_calls.append({"peer_id": peer_id, "prompt": prompt, "model": model,
                           "provider": provider, "timeout": timeout})
        if fail is not None:
            raise fail
        return _priced_result() if result is None else result

    service.p2p_coordinator = types.SimpleNamespace(request_inference_from_peer=request_inference_from_peer)
    service.peer_calls = peer_calls
    return service


def _rows(ledger):
    return list(ledger.rows())


def _assert_requester_row(row, *, billing, cost_usd):
    assert (row["caller"], row["caller_kind"], row["route"]) == (NODE_ID, "gateway", "peer")
    assert row["request_id"] == WIRE_ID, "the wire id, never one minted here"
    assert (row["alias"], row["model"]) == (REMOTE_ALIAS, HOST_MODEL)
    assert (row["prompt_tokens"], row["completion_tokens"], row["thinking_tokens"]) == (20, 10, None)
    assert row["counts_source"] == "engine"
    assert row["billing"] == billing
    assert row["cost_usd"] == cost_usd
    assert row["duration_s"] >= 0


# --- (1) /v1/models: the two local lists, then one row per connected peer's alias --------


@pytest.mark.asyncio
async def test_models_lists_the_two_local_lists_and_each_proved_peers_menu_owned_by_the_peer(tmp_path):
    async with _running(tmp_path, _peer_service(tmp_path)) as (server, _):
        status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))
        assert status == 200
        data = json.loads(text)["data"]
        assert [(m["id"], m["owned_by"]) for m in data] == [
            (LOCAL, "local"), (VENDOR, "vendor"),
            (REMOTE_MODEL, PEER), (f"remote:{PEER}:{REMOTE_VISION_ALIAS}", PEER),
        ]
        assert all(m["object"] == "model" for m in data)
        # A connected peer that sent no menu and a menu whose peer is gone list nothing.
        assert not any(SILENT_PEER in m["id"] or GONE_PEER in m["id"] for m in data)


@pytest.mark.asyncio
async def test_models_lists_no_alias_of_a_peer_on_an_unproved_connection(tmp_path):
    """A row on the menu is a promise the door can keep: WebRTC proves no sender (D2)."""
    async with _running(tmp_path, _peer_service(tmp_path, connection_type="webrtc")) as (server, _):
        status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))
        assert status == 200
        assert [m["id"] for m in json.loads(text)["data"]] == [LOCAL, VENDOR]


# --- (2) a peer completion in the OpenAI shape, one requester row -------------------------


@pytest.mark.asyncio
async def test_a_peer_completion_is_openai_shaped_echoes_the_remote_name_and_leaves_one_requester_row(tmp_path):
    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text
        body = json.loads(text)
        assert body["object"] == "chat.completion"
        assert body["model"] == REMOTE_MODEL
        assert body["id"] == "chatcmpl-" + WIRE_ID
        assert body["choices"] == [{"index": 0, "finish_reason": "stop",
                                    "message": {"role": "assistant", "content": PEER_ANSWER}}]
        assert body["usage"] == {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}

        # One call to the peer: the flattened prompt, the alias as the peer names it.
        assert service.calls == [], "the local provider layer is not touched"
        (call,) = service.peer_calls
        assert (call["peer_id"], call["provider"], call["prompt"]) == (PEER, REMOTE_ALIAS, OPENAI_PROMPT)
        assert call["model"] is None, "the host picks the model behind its alias"
        assert call["timeout"] == pytest.approx(service.settings.get_remote_inference_timeout())

        (row,) = _rows(ledger)
        _assert_requester_row(row, billing="pay_per_use", cost_usd=0.0041)


# --- (3) the same through /v1/messages ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_completion_in_the_messages_form_is_messages_shaped_and_leaves_one_requester_row(tmp_path):
    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _post_messages(server, _messages(REMOTE_MODEL), key=_key(tmp_path))
        assert status == 200, text
        body = json.loads(text)
        assert (body["type"], body["role"], body["model"]) == ("message", "assistant", REMOTE_MODEL)
        assert body["id"] == "msg_" + WIRE_ID
        assert body["content"] == [{"type": "text", "text": PEER_ANSWER}]
        assert (body["stop_reason"], body["stop_sequence"]) == ("end_turn", None)
        assert body["usage"] == {"input_tokens": 20, "output_tokens": 10}

        (call,) = service.peer_calls
        assert (call["peer_id"], call["provider"], call["prompt"]) == (PEER, REMOTE_ALIAS, OPENAI_PROMPT)
        (row,) = _rows(ledger)
        _assert_requester_row(row, billing="pay_per_use", cost_usd=0.0041)


# --- (4) a host that sent no price: null, never 0.0 ---------------------------------------


@pytest.mark.asyncio
async def test_a_host_that_sent_no_price_leaves_cost_null_and_billing_from_the_fallback(tmp_path, caplog):
    """This node did not run the call and does not price it (D3): `cost_usd`
    stays null — a zero would read as free — and `billing` falls back to this
    node's own table for the model the host named."""
    service = _peer_service(tmp_path, result=_unpriced_result())
    async with _running(tmp_path, service) as (server, ledger):
        status, _ = await _request(server, "POST", "/v1/chat/completions",
                                   key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200
        (row,) = _rows(ledger)
        assert row["cost_usd"] is None
        assert row["billing"] == "subscription"
        assert row["request_id"] == WIRE_ID


@pytest.mark.asyncio
async def test_a_host_that_sent_no_counts_leaves_counts_marked_ours(tmp_path):
    """Counted here from the characters, as the adapter counts when the host
    did not, and the row says so: `counts_source=ours`."""
    result = _unpriced_result()
    result.update(prompt_tokens=None, response_tokens=None, tokens_used=None)
    service = _peer_service(tmp_path, result=result)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200
        (row,) = _rows(ledger)
        assert row["counts_source"] == "ours"
        assert row["prompt_tokens"] == len(OPENAI_PROMPT) // 4
        assert row["completion_tokens"] == len(PEER_ANSWER) // 4
        usage = json.loads(text)["usage"]
        assert (usage["prompt_tokens"], usage["completion_tokens"]) == (row["prompt_tokens"], row["completion_tokens"])


# --- (5) refusals: named, never a fallback, no row ------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_that_is_not_connected_is_503_and_reaches_nothing(tmp_path):
    service = _peer_service(tmp_path, connected=False)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 503
        message = json.loads(text)["error"]["message"]
        assert PEER in message and "not connected" in message
        assert service.peer_calls == [] and service.calls == [] and _rows(ledger) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_type", ["webrtc", "udp_dtls", "relay", "gossip", "unknown"])
async def test_a_peer_on_an_unproved_connection_is_503_naming_the_rule(tmp_path, connection_type):
    service = _peer_service(tmp_path, connection_type=connection_type)
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 503, text
        message = json.loads(text)["error"]["message"]
        assert "proved connection" in message and connection_type in message and PEER in message
        assert service.peer_calls == [] and _rows(ledger) == []

        status, text = await _post_messages(server, _messages(REMOTE_MODEL), key=_key(tmp_path))
        assert status == 503
        assert _anthropic_error(text)["type"] == "api_error"
        assert service.peer_calls == [] and _rows(ledger) == []


@pytest.mark.asyncio
async def test_an_alias_the_peer_does_not_serve_us_is_404_and_reaches_nothing(tmp_path):
    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        for name in (f"remote:{PEER}:not_on_the_menu", f"remote:{SILENT_PEER}:{REMOTE_ALIAS}"):
            status, text = await _request(server, "POST", "/v1/chat/completions",
                                          key=_key(tmp_path), body=_chat(name))
            assert status == 404, name
            message = json.loads(text)["error"]["message"]
            assert "does not serve" in message and name.split(":", 2)[2] in message
        status, text = await _post_messages(server, _messages(f"remote:{PEER}:not_on_the_menu"), key=_key(tmp_path))
        assert status == 404 and _anthropic_error(text)["type"] == "not_found_error"
        assert service.peer_calls == [] and _rows(ledger) == []


@pytest.mark.asyncio
async def test_a_malformed_remote_name_is_404_not_a_local_lookup(tmp_path):
    service = _peer_service(tmp_path)
    async with _running(tmp_path, service) as (server, ledger):
        for name in ("remote:", f"remote:{PEER}", f"remote:{PEER}:", "remote::mythos"):
            status, text = await _request(server, "POST", "/v1/chat/completions",
                                          key=_key(tmp_path), body=_chat(name))
            assert status == 404, name
            assert "remote:<node_id>:<alias>" in json.loads(text)["error"]["message"]
        assert service.peer_calls == [] and service.calls == [] and _rows(ledger) == []


@pytest.mark.asyncio
async def test_a_host_refusal_is_502_carrying_the_hosts_words_and_no_row(tmp_path):
    service = _peer_service(tmp_path, fail=RuntimeError("compute sharing disabled"))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 502
        assert "compute sharing disabled" in json.loads(text)["error"]["message"]
        assert len(service.peer_calls) == 1, "the refusal is the host's, so the host was asked"
        assert _rows(ledger) == [], "a call that produced nothing is not a row"


@pytest.mark.asyncio
async def test_a_timeout_is_504_naming_the_timeout_and_no_row(tmp_path):
    timeout = 0.2
    service = _peer_service(tmp_path, fail=TimeoutError(f"Inference request to {PEER} timed out after {timeout}s"))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 504
        message = json.loads(text)["error"]["message"]
        assert PEER in message and "0.2" in message and "remote_inference_timeout" in message
        assert len(service.peer_calls) == 1 and _rows(ledger) == []

        status, text = await _post_messages(server, _messages(REMOTE_MODEL), key=_key(tmp_path))
        assert status == 504 and _anthropic_error(text)["type"] == "api_error"


@pytest.mark.asyncio
async def test_a_peer_that_disconnects_mid_call_is_503_and_no_row(tmp_path):
    service = _peer_service(tmp_path, fail=ConnectionError(f"Peer {PEER} is not connected"))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 503 and PEER in json.loads(text)["error"]["message"]
        assert _rows(ledger) == []


# --- (6) the peer route takes no local lock and reads no local quota ---------------------


@pytest.mark.asyncio
async def test_the_peer_route_waits_on_no_local_card_and_needs_no_local_list(tmp_path):
    """The card is the host's and the quota is the host's: a held local lock
    and empty local serving lists neither block nor bound a peer call."""
    lock = asyncio.Semaphore(1)
    await lock.acquire()  # a peer is on this node's card
    service = _peer_service(tmp_path, compute={"enabled": False})
    try:
        async with _running(tmp_path, service, inference_lock=lock) as (server, ledger):
            status, text = await _request(server, "GET", "/v1/models", key=_key(tmp_path))
            assert [m["owned_by"] for m in json.loads(text)["data"]] == [PEER, PEER]

            status, text = await _request(server, "POST", "/v1/chat/completions",
                                          key=_key(tmp_path), body=_chat(REMOTE_MODEL))
            assert status == 200, text
            assert len(service.peer_calls) == 1
            (row,) = _rows(ledger)
            assert row["route"] == "peer"
            # The local alias still queues behind the held lock, as before.
            status, text = await _request(server, "POST", "/v1/chat/completions",
                                          key=_key(tmp_path), body=_chat(LOCAL))
            assert status == 404, "with the lists empty a local alias is not served"
    finally:
        lock.release()


# --- (7) stream: true on a peer alias is one chunk and [DONE] ------------------------------


@pytest.mark.asyncio
async def test_stream_true_on_a_peer_alias_yields_one_chunk_and_done(tmp_path):
    """The answer arrives from the peer whole (ADR-041 M1), so the stream is
    the whole text in one chunk: the caveat is the shape, and this pins it."""
    async with _running(tmp_path, _peer_service(tmp_path)) as (server, ledger):
        url = f"http://127.0.0.1:{server.port}/v1/chat/completions"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=_chat(REMOTE_MODEL, stream=True),
                                    headers={"Authorization": f"Bearer {_key(tmp_path)}"}) as resp:
                assert resp.status == 200
                assert resp.content_type == "text/event-stream"
                text = await resp.text()
        events = [line[len("data: "):] for line in text.split("\n") if line.startswith("data: ")]
        assert len(events) == 2 and events[1] == "[DONE]"
        chunk = json.loads(events[0])
        assert chunk["object"] == "chat.completion.chunk" and chunk["model"] == REMOTE_MODEL
        assert chunk["id"] == "chatcmpl-" + WIRE_ID
        assert chunk["choices"] == [{"index": 0, "finish_reason": "stop",
                                     "delta": {"role": "assistant", "content": PEER_ANSWER}}]
        assert len(_rows(ledger)) == 1

        url = f"http://127.0.0.1:{server.port}/v1/messages"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=_messages(REMOTE_MODEL, stream=True),
                                    headers={"x-api-key": _key(tmp_path)}) as resp:
                assert resp.status == 200
                text = await resp.text()
        events = _sse_events(text)
        assert [name for name, _ in events][2] == "content_block_delta"
        assert events[2][1]["delta"] == {"type": "text_delta", "text": PEER_ANSWER}
        assert events[0][1]["message"]["model"] == REMOTE_MODEL
        assert len(_rows(ledger)) == 2
