"""A usage row says whether its output count already includes reasoning.

`counts_source` says who counted; it does not say what was counted. The tariff
arithmetic needs the second answer, so a row carries `output_includes_thinking`
with three states — `includes`, `excludes`, `unknown` — set where the number is
made (the provider's usage dict, `LLMManager.query`'s recount, the wire), never
where the row is written. A row written before the column reads as `unknown`.

On the wire the two HTTP shapes report the output counter the way their own
schemas define it: the inclusive total, with the reasoning share beside it
(`completion_tokens_details.reasoning_tokens`; `output_tokens_details.thinking_tokens`).
"""

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.node_ledger import NodeLedger, usage_row
from dpc_client_core.message_handlers.inference_handler import RemoteInferenceResponseHandler
from tests.test_p2p_coordinator import make_coordinator
from tests.test_the_gateway_routes_a_peer_alias_over_a_proved_connection_and_writes_the_requester_row import (
    PEER as GATEWAY_PEER,
    REMOTE_MODEL,
    _peer_service,
    _priced_result,
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
from tests.test_the_gateway_speaks_the_anthropic_messages_form_over_the_same_door import (
    _messages,
    _post_messages,
    _sse_events,
)
from tests.test_the_thinking_count_on_screen_is_the_one_the_api_reported import (
    THINKING,
    _Provider as _ThinkingProvider,
    _manager,
)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
PEER = "dpc-node-" + "b" * 32


def _row(**overrides):
    fields = dict(
        request_id="req-1", caller="agent_001", caller_kind="agent",
        alias="ds_flash", model="deepseek-v4-flash", route="local",
        prompt_tokens=8, completion_tokens=1, thinking_tokens=56,
        counts_source="ours", started_at=NOW, duration_s=1.0,
        billing="subscription", cost_usd=0.0,
    )
    fields.update(overrides)
    return usage_row(**fields)


# --- the ledger row ---------------------------------------------------------------


def test_a_row_that_says_nothing_is_unknown():
    assert _row()["output_includes_thinking"] == "unknown"


@pytest.mark.parametrize("state", ["includes", "excludes", "unknown"])
def test_the_three_states_are_written_as_given(state):
    assert _row(output_includes_thinking=state)["output_includes_thinking"] == state


def test_a_fourth_state_is_refused_not_written():
    with pytest.raises(ValueError, match="output_includes_thinking"):
        _row(output_includes_thinking=True)


def test_a_row_written_before_the_column_reads_as_unknown(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    old = {k: v for k, v in _row().items() if k != "output_includes_thinking"}
    ledger.append(old)

    (row,) = ledger.rows()
    assert row["output_includes_thinking"] == "unknown"


# --- where the local count is born: LLMManager's recount of the visible text ---------


@pytest.mark.asyncio
async def test_the_managers_recount_of_the_visible_text_excludes_thinking():
    provider = _ThinkingProvider(THINKING, {"reasoning_tokens": 168, "completion_tokens": 186})
    result = await _manager(provider).query("hi", return_metadata=True)

    assert result["output_includes_thinking"] == "excludes"
    assert result["thinking_tokens"] == 168


# --- the agent path: the provider's usage dict carries the state to the row -----------


class _DeclaringProvider:
    alias = "ds_flash"
    model = "deepseek-v4-flash"

    def __init__(self, usage):
        self._usage = usage

    async def generate_response(self, prompt, **kwargs):
        return "short answer"

    def get_last_usage(self):
        return self._usage


def _adapter(provider, ledger, **kwargs):
    manager = SimpleNamespace(
        token_count_manager=None, providers={"ds_flash": provider},
        agent_provider=None, default_provider="ds_flash",
    )
    return DpcLlmAdapter(manager, provider_alias="ds_flash", caller="agent_test", ledger=ledger, **kwargs)


@pytest.mark.asyncio
async def test_an_engine_row_carries_what_the_provider_declared(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    declared = {"prompt_tokens": 1000, "completion_tokens": 900, "total_tokens": 1900,
                "reasoning_tokens": 850, "output_includes_thinking": "includes"}
    silent = {"prompt_tokens": 1000, "completion_tokens": 900, "total_tokens": 1900}

    await _adapter(_DeclaringProvider(declared), ledger).chat([{"role": "user", "content": "x"}])
    await _adapter(_DeclaringProvider(silent), ledger).chat([{"role": "user", "content": "x"}])

    with_state, without = ledger.rows()
    assert with_state["counts_source"] == "engine" and with_state["output_includes_thinking"] == "includes"
    assert without["counts_source"] == "engine" and without["output_includes_thinking"] == "unknown"


@pytest.mark.asyncio
async def test_the_agents_peer_row_inherits_the_convention_of_the_node_that_counted(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    service = SimpleNamespace(_request_inference_from_peer=AsyncMock(return_value={
        "request_id": "req-from-the-wire", "response": "from afar",
        "prompt_tokens": 40, "response_tokens": 1, "thinking_tokens": 56, "tokens_used": 41,
        "model": "qwen-on-the-peer", "output_includes_thinking": "excludes",
    }))
    adapter = _adapter(_DeclaringProvider({}), ledger, compute_host=PEER)
    adapter._llm_manager.providers["dpc_agent"] = SimpleNamespace(
        peer_id=None, remote_model=None, timeout=5, _service=service,
    )

    await adapter.chat([{"role": "user", "content": "x"}])

    (row,) = ledger.rows()
    assert (row["route"], row["counts_source"]) == ("peer", "engine")
    assert row["output_includes_thinking"] == "excludes"


# --- the peer path: the host's row and the wire ---------------------------------------


@pytest.mark.asyncio
async def test_the_host_writes_the_state_on_its_row_and_sends_it_on_the_wire(tmp_path):
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "pong", "model": "qwen3.8", "provider": "ollama_local",
        "prompt_tokens": 8, "response_tokens": 1, "thinking_tokens": 56, "tokens_used": 9,
        "output_includes_thinking": "excludes",
    })
    coord._ledger = NodeLedger(tmp_path / "ledger")

    await coord.handle_inference_request("peer-1", "req-1", "ping")

    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert sent["payload"]["output_includes_thinking"] == "excludes"
    (row,) = coord._ledger.rows()
    assert row["output_includes_thinking"] == "excludes"


@pytest.mark.asyncio
async def test_the_requester_reads_the_state_off_the_wire_and_an_old_host_leaves_it_absent():
    import asyncio

    handler = RemoteInferenceResponseHandler(SimpleNamespace(_pending_inference_requests={}))
    loop = asyncio.get_running_loop()
    stated, silent = loop.create_future(), loop.create_future()
    handler.service._pending_inference_requests.update({"req-stated": stated, "req-silent": silent})

    await handler.handle("peer-1", {"request_id": "req-stated", "status": "success", "response": "ok",
                                    "output_includes_thinking": "includes"})
    await handler.handle("peer-1", {"request_id": "req-silent", "status": "success", "response": "ok"})

    assert stated.result()["output_includes_thinking"] == "includes"
    assert "output_includes_thinking" not in silent.result()


# --- the gateway: both shapes report the inclusive total with the share beside it --------


def _thinking_service(tmp_path, state="excludes"):
    """The stand-in, answering as the local route does on a thinking model:
    one visible token, fifty-six of reasoning (the live rows of 2026-09-10)."""
    service = _service(tmp_path, BOTH_LISTS)
    metadata = {
        "response": "pong", "provider": LOCAL, "model": "qwen3:8b",
        "tokens_used": 9, "prompt_tokens": 8, "response_tokens": 1,
        "model_max_tokens": 4096, "vision_used": False,
        "thinking": "a long deliberation", "thinking_tokens": 56,
        "output_includes_thinking": state,
    }

    async def query(prompt, provider_alias=None, return_metadata=False, **kwargs):
        return dict(metadata)

    async def query_messages(messages, *, system="", provider_alias=None, return_metadata=False, **kwargs):
        return dict(metadata, streamed=False, flattened=False, tools_used=False, tool_calls=[], finish_reason=None)

    service.llm_manager.query = query
    service.llm_manager.query_messages = query_messages
    return service


@pytest.mark.asyncio
async def test_the_openai_shape_counts_fifty_seven_where_one_is_visible(tmp_path):
    async with _running(tmp_path, _thinking_service(tmp_path)) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200, text
        assert json.loads(text)["usage"] == {
            "prompt_tokens": 8, "completion_tokens": 57, "total_tokens": 65,
            "completion_tokens_details": {"reasoning_tokens": 56},
        }
        (row,) = ledger.rows()
        assert (row["completion_tokens"], row["thinking_tokens"]) == (1, 56)
        assert row["output_includes_thinking"] == "excludes"
        assert json.loads(text)["usage"]["completion_tokens"] == row["completion_tokens"] + row["thinking_tokens"]


@pytest.mark.asyncio
async def test_the_messages_shape_counts_fifty_seven_in_the_body_and_in_the_stream(tmp_path):
    import aiohttp

    async with _running(tmp_path, _thinking_service(tmp_path)) as (server, ledger):
        status, text = await _post_messages(server, _messages(LOCAL), key=_key(tmp_path))
        assert status == 200, text
        assert json.loads(text)["usage"] == {
            "input_tokens": 8, "output_tokens": 57, "output_tokens_details": {"thinking_tokens": 56},
        }

        url = f"http://127.0.0.1:{server.port}/v1/messages"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=_messages(LOCAL, stream=True),
                                    headers={"x-api-key": _key(tmp_path)}) as resp:
                events = dict(_sse_events(await resp.text()))
        # `message_delta` usage is cumulative on the wire and carries the input count too.
        assert events["message_delta"]["usage"] == {
            "input_tokens": 8, "output_tokens": 57, "output_tokens_details": {"thinking_tokens": 56},
        }
        assert events["message_start"]["message"]["usage"] == {"input_tokens": 8, "output_tokens": 0}
        assert all(r["output_includes_thinking"] == "excludes" for r in ledger.rows())


@pytest.mark.asyncio
async def test_a_host_that_already_counted_thinking_inside_is_not_counted_twice(tmp_path):
    result = _priced_result(response_tokens=57, thinking_tokens=56, output_includes_thinking="includes")
    async with _running(tmp_path, _peer_service(tmp_path, result=result)) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text
        assert json.loads(text)["usage"] == {
            "prompt_tokens": 20, "completion_tokens": 57, "total_tokens": 77,
            "completion_tokens_details": {"reasoning_tokens": 56},
        }
        (row,) = ledger.rows()
        assert (row["route"], row["output_includes_thinking"]) == ("peer", "includes")


@pytest.mark.asyncio
async def test_a_host_that_said_nothing_leaves_the_requester_row_unknown_and_the_count_as_sent(tmp_path):
    result = _priced_result(response_tokens=1, thinking_tokens=56)
    async with _running(tmp_path, _peer_service(tmp_path, result=result)) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text
        usage = json.loads(text)["usage"]
        assert (usage["completion_tokens"], usage["completion_tokens_details"]) == (1, {"reasoning_tokens": 56})
        (row,) = ledger.rows()
        assert row["output_includes_thinking"] == "unknown"


# --- the five local recounts: a count of the visible text is `excludes`, not `unknown` ----
#
# Each of these sites counts the answer text with the node's own tokenizer (or
# four characters a token) after the provider or the wire has already taken the
# thinking out of it. The label is known where the count is made, so the row
# says `excludes` — a None `thinking_tokens` changes the arithmetic by nothing.


class _RecountingProvider(_DeclaringProvider):
    """Answers on every local route and reports no usage, so every route counts for itself."""

    def __init__(self):
        super().__init__({})

    def supports_vision(self):
        return True

    async def generate_with_vision(self, prompt, images, **kwargs):
        return "a cat"

    async def generate_with_tools(self, messages, tools, system="", **kwargs):
        return {"content": "no tool needed", "tool_calls_raw": []}


PICTURE = [{"type": "text", "text": "what is this"},
           {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}]
A_TOOL = [{"type": "function", "function": {"name": "noop", "description": "nothing",
                                            "parameters": {"type": "object", "properties": {}}}}]


@pytest.mark.parametrize("route, messages, tools", [
    ("text", [{"role": "user", "content": "x"}], None),
    ("vision", [{"role": "user", "content": PICTURE}], None),
    ("native tools", [{"role": "user", "content": "x"}], A_TOOL),
])
@pytest.mark.asyncio
async def test_a_local_recount_of_the_visible_text_says_excludes(tmp_path, route, messages, tools):
    ledger = NodeLedger(tmp_path / "ledger")
    _, usage = await _adapter(_RecountingProvider(), ledger).chat(messages, tools=tools)

    (row,) = ledger.rows()
    assert row["counts_source"] == "ours", route
    assert usage["output_includes_thinking"] == "excludes", route
    assert row["output_includes_thinking"] == "excludes", route


@pytest.mark.parametrize("counter", [None, SimpleNamespace(count_tokens=lambda text, model: len(text))],
                         ids=["four chars a token", "the node's tokenizer"])
@pytest.mark.asyncio
async def test_the_agents_recount_of_a_peers_answer_says_excludes(tmp_path, counter):
    ledger = NodeLedger(tmp_path / "ledger")
    service = SimpleNamespace(_request_inference_from_peer=AsyncMock(return_value={
        "request_id": "req-uncounted", "response": "from afar", "thinking_tokens": None,
    }))
    adapter = _adapter(_DeclaringProvider({}), ledger, compute_host=PEER)
    adapter._llm_manager.providers["dpc_agent"] = SimpleNamespace(
        peer_id=None, remote_model=None, timeout=5, _service=service,
    )
    adapter._token_counter = counter

    await adapter.chat([{"role": "user", "content": "x"}])

    (row,) = ledger.rows()
    assert (row["route"], row["counts_source"]) == ("peer", "ours")
    assert row["output_includes_thinking"] == "excludes"


@pytest.mark.asyncio
async def test_the_gateways_recount_of_a_peers_answer_says_excludes(tmp_path):
    result = _priced_result(prompt_tokens=None, response_tokens=None, thinking_tokens=None)
    async with _running(tmp_path, _peer_service(tmp_path, result=result)) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text
        (row,) = ledger.rows()
        assert (row["route"], row["counts_source"]) == ("peer", "ours")
        assert row["output_includes_thinking"] == "excludes"


# --- the vocabulary is checked where the wire is read ----------------------------------
#
# A peer can send any string. `usage_row` refuses a fourth word, and until here
# the refusal landed on the requester's row — ERROR on every call, answer
# delivered, row lost. The reading edge now keeps the three words and turns
# anything else into `unknown` with one WARNING naming the peer and the value.


def _warnings_naming(caplog, peer, value):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and peer in r.getMessage() and repr(value) in r.getMessage()]


@pytest.mark.asyncio
async def test_a_fourth_word_from_a_peer_reads_as_unknown_on_the_requester(caplog):
    import asyncio

    handler = RemoteInferenceResponseHandler(SimpleNamespace(_pending_inference_requests={}))
    future = asyncio.get_running_loop().create_future()
    handler.service._pending_inference_requests["req-yes"] = future

    with caplog.at_level(logging.WARNING):
        await handler.handle("peer-1", {"request_id": "req-yes", "status": "success", "response": "ok",
                                        "output_includes_thinking": "yes"})

    assert future.result()["output_includes_thinking"] == "unknown"
    assert len(_warnings_naming(caplog, "peer-1", "yes")) == 1
    assert _row(output_includes_thinking=future.result()["output_includes_thinking"])["output_includes_thinking"] == "unknown"


@pytest.mark.asyncio
async def test_a_fourth_word_reaching_the_gateway_leaves_the_row_written_and_unknown(tmp_path, caplog):
    result = _priced_result(output_includes_thinking="yes")
    with caplog.at_level(logging.WARNING):
        async with _running(tmp_path, _peer_service(tmp_path, result=result)) as (server, ledger):
            status, text = await _request(server, "POST", "/v1/chat/completions",
                                          key=_key(tmp_path), body=_chat(REMOTE_MODEL))
            assert status == 200, text
            (row,) = ledger.rows()
    assert row["output_includes_thinking"] == "unknown"
    assert not [r for r in caplog.records if "was not built" in r.getMessage()]
    assert len(_warnings_naming(caplog, GATEWAY_PEER, "yes")) == 1


@pytest.mark.asyncio
async def test_a_fourth_word_reaching_the_agent_leaves_the_row_written_and_unknown(tmp_path, caplog):
    ledger = NodeLedger(tmp_path / "ledger")
    service = SimpleNamespace(_request_inference_from_peer=AsyncMock(return_value={
        "request_id": "req-yes", "response": "from afar", "prompt_tokens": 40, "response_tokens": 1,
        "tokens_used": 41, "model": "qwen-on-the-peer", "output_includes_thinking": "yes",
    }))
    adapter = _adapter(_DeclaringProvider({}), ledger, compute_host=PEER)
    adapter._llm_manager.providers["dpc_agent"] = SimpleNamespace(
        peer_id=None, remote_model=None, timeout=5, _service=service,
    )

    with caplog.at_level(logging.WARNING):
        await adapter.chat([{"role": "user", "content": "x"}])

    (row,) = ledger.rows()
    assert row["output_includes_thinking"] == "unknown"
    assert not [r for r in caplog.records if "was not built" in r.getMessage()]
    assert len(_warnings_naming(caplog, PEER, "yes")) == 1
