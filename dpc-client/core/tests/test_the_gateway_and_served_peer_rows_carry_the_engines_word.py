"""The gateway's rows and the host's served row carry the engine's word on the call.

Board entry GATEWAY-AND-SERVED-PEER-ROWS-CARRY-NO-ENGINE-FIELDS. Since ff68c996
and 23d1d445 the agent's row carries `node_ledger.ENGINE_FIELDS` and a vendor's
prompt cache split, while the other three `usage_row` writers passed none of it:
the gateway's local row, the gateway's peer row and the host's row of a served
peer call. An IDE call or a peer call on llama-server or Strata showed no
finish_reason, ceiling, prefill or cache split although the provider reported
them.

Pinned here: both `LLMManager` doors hand back the provider's engine fields
(`reported_engine`), the vendor split computed from the provider's own usage;
the gateway's local row and the host's row copy them; the host puts nothing new
on the wire, only the `finish_reason` it already carried, now also on the prompt
path; the gateway's peer row and the agent's peer row copy that one field from
the wire, on both branches, and a non-string there is dropped rather than left
to void the row. Cross-platform: pure asyncio, files only in tmp_path.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.node_ledger import ENGINE_FIELDS, NodeLedger
from tests.test_a_guest_agent_sends_its_tools_natively_where_the_host_serves_them import (
    CALL,
    TOOLS,
    TURNS,
    _answer,
    _guest,
)
from tests.test_p2p_coordinator import make_coordinator
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    REMOTE_MODEL,
    _peer_service,
    _priced_result,
    _rows,
)
from tests.test_the_gateway_serves_only_the_two_lists_on_loopback import (
    BOTH_LISTS,
    LOCAL,
    _chat,
    _key,
    _request,
    _running,
    _service,
)
from tests.test_the_message_shaped_door_reaches_the_provider_unflattened import (
    FLAT,
    MESSAGES,
    SYSTEM,
    _Plain,
    _manager,
)

# What llama-server's provider records on a round cut at its ceiling.
LLAMA_USAGE = {
    "prompt_tokens": 900, "completion_tokens": 512,
    "finish_reason": "length", "max_tokens": 512,
    "prefilled_tokens": 120, "cached_tokens": 780,
    "prompt_ms": 85.5, "predicted_ms": 6400.0,
}
ENGINE = {name: LLAMA_USAGE[name] for name in ENGINE_FIELDS}
# What a vendor records: no engine names, a cache hit and miss it priced on.
VENDOR_USAGE = {"prompt_tokens": 1000, "completion_tokens": 20,
                "prompt_cache_hit_tokens": 700, "prompt_cache_miss_tokens": 300}


class _Reporting(_Plain):
    """A prompt-door provider that records its usage as a real one does."""

    def __init__(self, usage):
        super().__init__()
        self.usage = usage

    async def generate_response(self, prompt, **kwargs):
        self._record_last_usage(dict(self.usage))
        return await super().generate_response(prompt, **kwargs)


def _engine(row):
    return {name: row.get(name) for name in ENGINE_FIELDS}


# --- the two doors hand the engine's word back -------------------------------------


@pytest.mark.asyncio
async def test_both_doors_hand_back_the_engines_word_on_the_call(tmp_path):
    manager = _manager(tmp_path, _Reporting(LLAMA_USAGE))

    by_prompt = await manager.query(FLAT, return_metadata=True)
    by_messages = await manager.query_messages(MESSAGES, system=SYSTEM, return_metadata=True)

    assert _engine(by_prompt) == ENGINE, "the prompt path too: a peer's text call goes through it"
    assert _engine(by_messages) == ENGINE


@pytest.mark.asyncio
async def test_a_vendors_cache_split_comes_from_its_own_usage_not_a_recount(tmp_path):
    manager = _manager(tmp_path, _Reporting(VENDOR_USAGE))

    result = await manager.query(FLAT, return_metadata=True)

    assert (result["prefilled_tokens"], result["cached_tokens"]) == (300, 700)
    assert result["finish_reason"] is None, "a vendor that said nothing stays silent"


# --- the gateway's local row --------------------------------------------------------


def _engine_service(tmp_path, engine):
    """The loopback stand-in, with a door that reports the engine's word."""
    service = _service(tmp_path, BOTH_LISTS)
    inner = service.llm_manager.query_messages

    async def query_messages(messages, **kwargs):
        return dict(await inner(messages, **kwargs), **engine)

    service.llm_manager.query_messages = query_messages
    return service


@pytest.mark.asyncio
async def test_a_local_gateway_row_carries_the_engines_word(tmp_path):
    async with _running(tmp_path, _engine_service(tmp_path, ENGINE)) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200, text

        (row,) = _rows(ledger)
        assert (row["route"], row["caller_kind"]) == ("local", "gateway")
        assert _engine(row) == ENGINE


# --- the gateway's peer row ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_gateway_row_copies_the_hosts_stop_word_and_nothing_else(tmp_path):
    service = _peer_service(tmp_path, result=_priced_result(finish_reason="length"))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text

        (row,) = _rows(ledger)
        assert row["route"] == "peer"
        assert _engine(row) == dict.fromkeys(ENGINE_FIELDS) | {"finish_reason": "length"}


@pytest.mark.asyncio
async def test_a_stop_word_that_is_not_a_string_is_dropped_and_the_peer_row_is_kept(tmp_path):
    service = _peer_service(tmp_path, result=_priced_result(finish_reason=7))
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text

        (row,) = _rows(ledger)
        assert row["finish_reason"] is None


# --- the host's row of a served call ------------------------------------------------


def _host(tmp_path, result):
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.providers = {"ollama_local": SimpleNamespace(config={})}
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "pong", "model": "qwen3:8b", "provider": "ollama_local",
        "prompt_tokens": 900, "response_tokens": 512, "tokens_used": 1412, **result,
    })
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


@pytest.mark.asyncio
async def test_the_hosts_served_row_carries_the_engines_word_and_the_wire_only_the_stop_word(tmp_path):
    coord, svc = _host(tmp_path, ENGINE)

    await coord.handle_inference_request("peer-1", "req-1", "ping")

    (row,) = coord._ledger.rows()
    assert (row["route"], row["caller_kind"]) == ("local", "peer")
    assert _engine(row) == ENGINE
    payload = svc.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]
    assert payload["status"] == "success"
    assert payload["finish_reason"] == "length"
    assert not set(ENGINE_FIELDS[1:]) & set(payload), "nothing new goes on the wire"


# --- the agent's peer row, on both branches ------------------------------------------


@pytest.mark.asyncio
async def test_the_agents_peer_row_keeps_the_hosts_stop_word_on_the_text_branch(tmp_path):
    adapter, asks, ledger = _guest(tmp_path, [_answer(response="cut", finish_reason="length")])

    await adapter.chat(TURNS, tools=TOOLS)

    assert "tools" not in asks[0], "the text branch"
    (row,) = ledger.rows()
    assert row["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_the_agents_peer_row_keeps_the_hosts_stop_word_on_the_native_branch(tmp_path):
    adapter, asks, ledger = _guest(
        tmp_path, [_answer(tool_calls=[CALL], finish_reason="tool_calls")], supports_tools=True)

    await adapter.chat(TURNS, tools=TOOLS)

    assert asks[0]["tools"], "the native branch"
    (row,) = ledger.rows()
    assert row["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_a_stop_word_that_is_not_a_string_does_not_cost_the_agent_its_row(tmp_path):
    adapter, _, ledger = _guest(tmp_path, [_answer(response="ok", finish_reason=["stop"])])

    await adapter.chat(TURNS)

    (row,) = ledger.rows()
    assert row["finish_reason"] is None
