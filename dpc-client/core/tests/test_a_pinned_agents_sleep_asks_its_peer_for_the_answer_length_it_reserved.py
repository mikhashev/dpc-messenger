"""A pinned agent's sleep asks its peer for the answer length and the wait it reserved.

Sleep's synthesis calls `llm_manager.query` with `max_tokens` (a tenth of the
window, up to 16384) and `timeout` (900 s). For an agent pinned to a peer's
model `_PinnedAgentQueries.query` forwarded only `reasoning_effort`, so the host
answered at its alias's own ceiling and this node waited the dpc_agent
provider's time, not sleep's. Mike's call, 2026-11-06: both reach the host.

`max_tokens` travels as REMOTE_INFERENCE_REQUEST's optional field (DPTP v1.14);
the host lowers it to the `max_output_tokens` its menu row states and sends
nothing where the row states none. `timeout` is this node's wait and does not
travel. A call that names neither - the agent's own turns - is the request it
always was, and so is an older guest's request at the host.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from dpc_protocol.protocol import create_remote_inference_request

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter, _llm_manager_for_agent
from dpc_client_core.message_handlers.inference_handler import RemoteInferenceRequestHandler
from dpc_client_core.node_ledger import NodeLedger
from tests.test_p2p_coordinator import make_coordinator

PEER = "dpc-node-bob-allowed-profile"
PEER_ALIAS = "llamacpp_qwen38_27b"
AGENT = "agent_muse_test"
PROVIDER_TIMEOUT = 5


class _Service:
    """What the peer route reads off CoreService; the fake stops at the wire call."""

    def __init__(self):
        self.firewall = None
        self.device_context = None
        self.p2p_manager = SimpleNamespace(local_context=None, peers={})
        self.sent = []

    async def _request_inference_from_peer(self, peer_id, prompt, **kwargs):
        self.sent.append({"peer_id": peer_id, "prompt": prompt, **kwargs})
        return {"response": "{}", "model": "Qwen3.8-27B"}


class _Manager:
    def __init__(self, service):
        self.providers = {
            "dpc_agent": SimpleNamespace(peer_id=None, remote_model=None, remote_provider="x",
                                         timeout=PROVIDER_TIMEOUT, _service=service),
        }
        self.default_provider = None
        self.token_count_manager = None

    async def query(self, prompt, provider_alias=None, **kwargs):
        raise AssertionError("a peer's alias is never asked of this node")

    def count_tokens(self, text, model):
        return len(text) // 4


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


# --- (a) the guest: what sleep asked for is what the peer request carries ------------


@pytest.mark.asyncio
async def test_a_sleep_style_query_sends_its_answer_length_and_its_wait_to_the_peer():
    service = _Service()
    pinned = _llm_manager_for_agent(_Manager(service), PEER, AGENT)

    await pinned.query("synthesise", provider_alias=PEER_ALIAS, timeout=900, max_tokens=16384,
                       reasoning_budget_tokens=4096, reasoning_effort="high")

    (sent,) = service.sent
    assert sent["peer_id"] == PEER and sent["provider"] == PEER_ALIAS
    assert sent["max_tokens"] == 16384
    assert sent["timeout"] == 900
    assert sent["reasoning_effort"] == "high"
    assert "reasoning_budget_tokens" not in sent, "the wire has no field for it"


@pytest.mark.asyncio
async def test_a_query_that_names_neither_keeps_the_providers_wait_and_sends_no_length():
    service = _Service()
    pinned = _llm_manager_for_agent(_Manager(service), PEER, AGENT)

    await pinned.query("analyse one session", provider_alias=PEER_ALIAS,
                       reasoning_budget_tokens=2048, reasoning_effort="low")

    (sent,) = service.sent
    assert "max_tokens" not in sent
    assert sent["timeout"] == PROVIDER_TIMEOUT


@pytest.mark.asyncio
async def test_the_agents_own_turn_on_its_peer_is_the_request_it_always_was():
    """The loop names no length: the 4096 that `chat` used to default to never
    travelled, and must not start to now."""
    service = _Service()
    adapter = DpcLlmAdapter(_Manager(service), provider_alias=PEER_ALIAS,
                            compute_host=PEER, caller=AGENT)

    await adapter.chat([{"role": "user", "content": "hello"}])

    (sent,) = service.sent
    assert "max_tokens" not in sent
    assert sent["timeout"] == PROVIDER_TIMEOUT


@pytest.mark.asyncio
async def test_the_wire_frame_carries_the_length_and_not_the_wait():
    coord, svc = make_coordinator()
    asked = asyncio.ensure_future(coord.request_inference_from_peer(
        "peer-1", "synthesise", provider=PEER_ALIAS, timeout=900, max_tokens=16384,
        request_id="req-1",
    ))
    for _ in range(50):
        if svc.p2p_manager.send_message_to_peer.await_count:
            break
        await asyncio.sleep(0)
    frame = svc.p2p_manager.send_message_to_peer.await_args.args[1]
    svc._pending_inference_requests["req-1"].set_result({"response": "ok"})
    await asked

    assert frame["payload"]["max_tokens"] == 16384
    assert "timeout" not in frame["payload"]


def test_a_request_without_a_length_is_byte_identical_to_a_v1_13_request():
    assert (json.dumps(create_remote_inference_request("req-1", "hi", max_tokens=None))
            == json.dumps(create_remote_inference_request("req-1", "hi")))
    assert "max_tokens" not in create_remote_inference_request("req-1", "hi")["payload"]


# --- (b) the host: the length reaches its query, under its own ceiling ---------------


class _Provider:
    """A serving alias whose menu row states `ceiling` as max_output_tokens."""

    def __init__(self, ceiling):
        self.config = {"type": "llamacpp_server"}
        self.model = "qwen3.8-27b"
        self._ceiling = ceiling

    def effective_settings(self):
        return {} if self._ceiling is None else {"max_output_tokens": self._ceiling}


def _host(tmp_path, ceiling):
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.providers = {"ollama_local": _Provider(ceiling)}
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "{}", "model": "qwen3.8-27b", "provider": "ollama_local",
        "prompt_tokens": 8, "response_tokens": 7, "tokens_used": 15,
    })
    svc.llm_manager.query_messages = AsyncMock()
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


def _answer(svc):
    frames = [call.args[1] for call in svc.p2p_manager.send_message_to_peer.await_args_list]
    (final,) = [f for f in frames if f["command"] == "REMOTE_INFERENCE_RESPONSE"]
    return final["payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling, served", [(32768, 16384), (8192, 8192)],
                         ids=["under the ceiling", "lowered to the ceiling"])
async def test_the_host_hands_the_length_to_its_query_under_its_rows_ceiling(tmp_path, ceiling, served):
    coord, svc = _host(tmp_path, ceiling)

    await coord.handle_inference_request("peer-1", "req-1", "synthesise", max_tokens=16384)

    assert svc.llm_manager.query.await_args.kwargs["max_tokens"] == served
    assert _answer(svc)["status"] == "success"


@pytest.mark.asyncio
async def test_an_alias_that_states_no_ceiling_is_sent_no_length(tmp_path):
    coord, svc = _host(tmp_path, None)

    await coord.handle_inference_request("peer-1", "req-1", "synthesise", max_tokens=16384)

    assert "max_tokens" not in svc.llm_manager.query.await_args.kwargs
    assert _answer(svc)["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, "16384", 1.5, True])
async def test_a_length_that_is_not_a_number_of_tokens_is_refused_before_anything_runs(tmp_path, bad):
    coord, svc = _host(tmp_path, 32768)

    await coord.handle_inference_request("peer-1", "req-1", "synthesise", max_tokens=bad)

    svc.llm_manager.query.assert_not_awaited()
    payload = _answer(svc)
    assert payload["status"] == "error" and payload["code"] == "invalid_value"
    assert list(NodeLedger(tmp_path / "ledger").rows()) == []


@pytest.mark.asyncio
async def test_a_conversation_reaches_query_messages_without_a_length(tmp_path):
    """`query_messages` takes no per-call length; the spec says such a call is
    served at the alias's ceiling."""
    coord, svc = _host(tmp_path, 32768)
    svc.llm_manager.query_messages = AsyncMock(return_value=svc.llm_manager.query.return_value)

    await coord.handle_inference_request(
        "peer-1", "req-1", "flattened", messages=[{"role": "user", "content": "hi"}],
        max_tokens=16384,
    )

    assert "max_tokens" not in svc.llm_manager.query_messages.await_args.kwargs


@pytest.mark.asyncio
async def test_the_handler_hands_the_length_on_when_it_was_sent():
    service = MagicMock()
    service._handle_inference_request = AsyncMock()

    await RemoteInferenceRequestHandler(service).handle(
        "peer-1", {"request_id": "req-1", "prompt": "synthesise", "max_tokens": 16384})

    assert service._handle_inference_request.await_args.kwargs["max_tokens"] == 16384


# --- (c) an older guest's request is served as today ---------------------------------


@pytest.mark.asyncio
async def test_a_request_without_the_field_reaches_the_query_as_before(tmp_path):
    coord, svc = _host(tmp_path, 32768)
    service = MagicMock()
    service._handle_inference_request = AsyncMock()

    await RemoteInferenceRequestHandler(service).handle(
        "peer-1", {"request_id": "req-1", "prompt": "synthesise"})
    await coord.handle_inference_request("peer-1", "req-1", "synthesise")

    assert "max_tokens" not in service._handle_inference_request.await_args.kwargs
    assert "max_tokens" not in svc.llm_manager.query.await_args.kwargs
    assert _answer(svc)["status"] == "success"
