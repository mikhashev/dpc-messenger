"""A thinking count estimated over the reasoning text is bounded by the exact
total it sits inside, and says that it is an estimate.

The pinned llama-server build fills no `completion_tokens_details`, so the
provider estimates the reasoning share at chars/4 and labels the counts
`includes` — `completion_tokens` is the server's exact count of everything it
decoded, the reasoning block included. The estimate was not bounded by that
exact number, and Cyrillic reasoning undercounts nothing by 4 characters a
token: live rows on both nodes on 2026-09-14 read completion 22 with thinking 24
(ab08ff95) and completion 45 with thinking 49 (a92a1661), and `content_tokens`
floored at 0 on both. Money was never wrong — the tariff bills `completion` —
but a part was larger than its whole on disk.

Two answers, at two layers. The provider clamps its own estimate, and every row
carries `thinking_source` (`engine` | `estimated` | absent) so a reader is told
which of the two made the number instead of inferring it from a log marker. The
ledger holds the invariant for counts it did not make: a peer's row arriving with
`includes` and a thinking count over its completion is written clamped, with a
warning, rather than refused — the call was made and paid for either way.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_protocol.protocol import create_remote_inference_response

from dpc_client_core.message_handlers.inference_handler import RemoteInferenceResponseHandler
from dpc_client_core.node_ledger import NodeLedger, usage_row

from tests.test_llamacpp_server_provider import _FakeSupervisor, _provider
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
from tests.test_the_thinking_count_on_screen_is_the_one_the_api_reported import (
    THINKING,
    _Provider as _ThinkingProvider,
    _manager,
)

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
PEER = "dpc-node-" + "c" * 32
# The two rows of 2026-09-14, identical on guest and host.
LIVE_ROWS = [(22, 24), (45, 49)]


def _usage(completion, *, reasoning_chars=None, reported=None):
    """The provider's accounting for one call: a server that reported the split,
    or one that reported none and left the reasoning text to be estimated."""
    details = SimpleNamespace(reasoning_tokens=reported) if reported is not None else None
    return _provider_with_supervisor()._record_usage(
        SimpleNamespace(prompt_tokens=10, completion_tokens=completion,
                        total_tokens=10 + completion, completion_tokens_details=details),
        path="plain", reasoning_text="x" * (reasoning_chars or 0) or None,
    )


def _provider_with_supervisor():
    provider = _provider()
    provider.supervisor = _FakeSupervisor()
    return provider


# --- the provider: the estimate is bounded by the server's own total ----------------


@pytest.mark.parametrize("completion, thinking", LIVE_ROWS)
def test_the_live_rows_of_the_fourteenth_no_longer_hold_a_part_larger_than_its_whole(completion, thinking):
    # chars/4 over the reasoning text that produced those counts.
    usage = _usage(completion, reasoning_chars=thinking * 4)

    assert usage["output_includes_thinking"] == "includes"
    assert usage["reasoning_tokens"] <= usage["completion_tokens"]
    assert usage["reasoning_tokens"] == completion


def test_an_estimate_that_fits_is_left_where_it_lands():
    usage = _usage(100, reasoning_chars=400)

    assert usage["reasoning_tokens"] == 100  # 400 / 4, and the total is 100
    usage = _usage(100, reasoning_chars=40)
    assert usage["reasoning_tokens"] == 10


def test_the_clamped_split_leaves_a_content_count_that_is_not_negative():
    usage = _usage(22, reasoning_chars=96)

    assert usage["content_tokens"] == 0
    assert usage["content_tokens"] + usage["reasoning_tokens"] == usage["completion_tokens"]


def test_an_estimated_split_says_it_is_an_estimate():
    assert _usage(100, reasoning_chars=400)["thinking_source"] == "estimated"


def test_a_split_the_server_reported_is_the_engines_and_passes_through():
    usage = _usage(100, reasoning_chars=4000, reported=50)

    assert (usage["reasoning_tokens"], usage["content_tokens"]) == (50, 50)
    assert usage["thinking_source"] == "engine"


def test_a_call_with_no_reasoning_at_all_claims_no_source():
    assert "thinking_source" not in _usage(100)


def test_the_clamp_is_named_in_the_usage_line(caplog):
    with caplog.at_level(logging.INFO, logger="dpc_client_core.providers.llamacpp_server_provider"):
        _usage(22, reasoning_chars=96)
    assert any("split=estimated (clamped to completion)" in r.getMessage() for r in caplog.records)


# --- the door: the word travels with the count it describes --------------------------


@pytest.mark.asyncio
async def test_the_door_carries_the_provider_word_for_a_reported_count():
    provider = _ThinkingProvider(THINKING, {
        "reasoning_tokens": 168, "completion_tokens": 186, "thinking_source": "engine",
    })
    result = await _manager(provider).query("hi", return_metadata=True)

    assert (result["thinking_tokens"], result["thinking_source"]) == (168, "engine")


@pytest.mark.asyncio
async def test_the_doors_own_recount_of_the_thinking_text_says_estimated():
    provider = _ThinkingProvider(THINKING, {"completion_tokens": 186})
    result = await _manager(provider).query("hi", return_metadata=True)

    assert result["thinking_tokens"] == len(THINKING) // 4
    assert result["thinking_source"] == "estimated"


@pytest.mark.asyncio
async def test_a_count_with_no_word_behind_it_is_not_credited_to_the_engine():
    provider = _ThinkingProvider(THINKING, {"reasoning_tokens": 168, "completion_tokens": 186})
    result = await _manager(provider).query("hi", return_metadata=True)

    assert (result["thinking_tokens"], result["thinking_source"]) == (168, None)


@pytest.mark.asyncio
async def test_the_other_door_of_the_same_manager_carries_it_too():
    """`query` and `query_messages` hold two copies of this rule and the two
    have to move together — the agent's tool rounds go through the second."""
    provider = _ThinkingProvider(THINKING, {"completion_tokens": 186})
    result = await _manager(provider).query_messages(
        [{"role": "user", "content": "hi"}], return_metadata=True,
    )

    assert result["thinking_source"] == "estimated"


@pytest.mark.asyncio
async def test_a_call_that_did_no_thinking_names_no_source():
    provider = _ThinkingProvider("", {"completion_tokens": 186})
    result = await _manager(provider).query("hi", return_metadata=True)

    assert (result["thinking_tokens"], result["thinking_source"]) == (None, None)


# --- the ledger: the invariant holds on disk whatever a peer's provider does ----------


def _row(**overrides):
    fields = dict(
        request_id="req-1", caller="agent_001", caller_kind="agent",
        alias="local_qwen38", model="qwen3:8b", route="local",
        prompt_tokens=8, completion_tokens=22, thinking_tokens=24,
        counts_source="engine", started_at=NOW, duration_s=1.0,
        billing="subscription", cost_usd=0.0,
    )
    fields.update(overrides)
    return usage_row(**fields)


def test_a_row_that_says_nothing_about_the_source_writes_a_null():
    assert _row(output_includes_thinking="excludes")["thinking_source"] is None


@pytest.mark.parametrize("word", ["engine", "estimated"])
def test_the_two_words_are_written_as_given(word):
    assert _row(output_includes_thinking="excludes", thinking_source=word)["thinking_source"] == word


def test_a_third_word_is_refused_not_written():
    with pytest.raises(ValueError, match="thinking_source"):
        _row(output_includes_thinking="excludes", thinking_source="guessed")


def test_a_row_written_before_the_column_reads_as_none(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    old = {k: v for k, v in _row(output_includes_thinking="excludes").items() if k != "thinking_source"}
    ledger.append(old)

    (row,) = ledger.rows()
    assert row["thinking_source"] is None


@pytest.mark.parametrize("completion, thinking", LIVE_ROWS)
def test_a_count_over_the_total_it_is_inside_is_clamped_and_marked(completion, thinking, caplog):
    with caplog.at_level(logging.WARNING):
        row = _row(completion_tokens=completion, thinking_tokens=thinking,
                   output_includes_thinking="includes", thinking_source="engine")

    assert (row["thinking_tokens"], row["thinking_source"]) == (completion, "estimated")
    assert row["completion_tokens"] == completion, "the exact total is never touched"
    named = [r for r in caplog.records
             if r.levelno == logging.WARNING and "req-1" in r.getMessage()]
    assert len(named) == 1 and str(thinking) in named[0].getMessage()


def test_a_count_that_fits_inside_the_total_is_left_alone(caplog):
    with caplog.at_level(logging.WARNING):
        row = _row(completion_tokens=57, thinking_tokens=56,
                   output_includes_thinking="includes", thinking_source="engine")

    assert (row["thinking_tokens"], row["thinking_source"]) == (56, "engine")
    assert not caplog.records


def test_under_excludes_the_thinking_sits_outside_the_total_and_is_not_clamped(caplog):
    with caplog.at_level(logging.WARNING):
        row = _row(completion_tokens=1, thinking_tokens=56, output_includes_thinking="excludes")

    assert (row["completion_tokens"], row["thinking_tokens"]) == (1, 56)
    assert not caplog.records


def test_a_row_with_no_thinking_count_passes_the_guard_untouched():
    row = _row(thinking_tokens=None, output_includes_thinking="includes")

    assert row["thinking_tokens"] is None


# --- both doors and the wire ---------------------------------------------------------


def _thinking_service(tmp_path, **metadata):
    """The local route answering as a thinking model, with whatever the door
    was told about its own counts."""
    service = _service(tmp_path, BOTH_LISTS)
    facts = {
        "response": "pong", "provider": LOCAL, "model": "qwen3:8b",
        "tokens_used": 30, "prompt_tokens": 8, "response_tokens": 22,
        "model_max_tokens": 4096, "vision_used": False,
        "thinking": "a long deliberation", "thinking_tokens": 22,
        "output_includes_thinking": "includes", "counts_source": "engine",
        "thinking_source": "estimated",
    }
    facts.update(metadata)

    async def query(prompt, provider_alias=None, return_metadata=False, **kwargs):
        return dict(facts)

    async def query_messages(messages, *, system="", provider_alias=None, return_metadata=False, **kwargs):
        return dict(facts, streamed=False, flattened=False, tools_used=False,
                    tool_calls=[], finish_reason=None)

    service.llm_manager.query = query
    service.llm_manager.query_messages = query_messages
    return service


@pytest.mark.asyncio
async def test_the_gateways_local_row_carries_the_word_the_door_was_given(tmp_path):
    async with _running(tmp_path, _thinking_service(tmp_path)) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200, text
        (row,) = ledger.rows()
        assert row["thinking_source"] == "estimated"
        assert row["thinking_tokens"] <= row["completion_tokens"]


@pytest.mark.asyncio
async def test_the_hosts_row_and_its_answer_both_name_the_source(tmp_path):
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "pong", "model": "qwen3:8b", "provider": "local_qwen38",
        "prompt_tokens": 8, "response_tokens": 22, "thinking_tokens": 22, "tokens_used": 30,
        "output_includes_thinking": "includes", "thinking_source": "estimated",
    })
    coord._ledger = NodeLedger(tmp_path / "ledger")

    await coord.handle_inference_request("peer-1", "req-1", "ping")

    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert sent["payload"]["thinking_source"] == "estimated"
    (row,) = coord._ledger.rows()
    assert row["thinking_source"] == "estimated"


def test_the_wire_carries_the_word_only_where_there_is_one():
    with_word = create_remote_inference_response(
        request_id="req-1", response="ok", thinking_tokens=22,
        output_includes_thinking="includes", thinking_source="estimated",
    )
    without = create_remote_inference_response(request_id="req-2", response="ok", thinking_tokens=22)

    assert with_word["payload"]["thinking_source"] == "estimated"
    assert "thinking_source" not in without["payload"]


@pytest.mark.asyncio
async def test_the_requester_reads_the_word_off_the_wire_and_an_old_host_leaves_it_absent():
    handler = RemoteInferenceResponseHandler(SimpleNamespace(_pending_inference_requests={}))
    loop = asyncio.get_running_loop()
    stated, silent = loop.create_future(), loop.create_future()
    handler.service._pending_inference_requests.update({"req-stated": stated, "req-silent": silent})

    await handler.handle("peer-1", {"request_id": "req-stated", "status": "success",
                                    "response": "ok", "thinking_source": "engine"})
    await handler.handle("peer-1", {"request_id": "req-silent", "status": "success", "response": "ok"})

    assert stated.result()["thinking_source"] == "engine"
    assert "thinking_source" not in silent.result()


@pytest.mark.asyncio
async def test_a_third_word_from_a_peer_is_dropped_with_one_warning(caplog):
    handler = RemoteInferenceResponseHandler(SimpleNamespace(_pending_inference_requests={}))
    future = asyncio.get_running_loop().create_future()
    handler.service._pending_inference_requests["req-guessed"] = future

    with caplog.at_level(logging.WARNING):
        await handler.handle("peer-1", {"request_id": "req-guessed", "status": "success",
                                        "response": "ok", "thinking_source": "guessed"})

    assert "thinking_source" not in future.result()
    assert len([r for r in caplog.records
                if "peer-1" in r.getMessage() and "'guessed'" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_the_guest_row_copies_the_hosts_word(tmp_path):
    result = _priced_result(response_tokens=57, thinking_tokens=56,
                            output_includes_thinking="includes", thinking_source="estimated")
    async with _running(tmp_path, _peer_service(tmp_path, result=result)) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(REMOTE_MODEL))
        assert status == 200, text
        (row,) = ledger.rows()
        assert (row["route"], row["thinking_source"]) == ("peer", "estimated")


@pytest.mark.asyncio
async def test_a_hosts_part_larger_than_its_whole_is_written_clamped_on_the_guest_row(tmp_path, caplog):
    """The host end of this fix is ours; the peer's provider is not. A guest
    receiving the rows of 2026-09-14 from an unfixed host writes them with the
    invariant held, and the answer is still delivered."""
    result = _priced_result(response_tokens=22, thinking_tokens=24,
                            output_includes_thinking="includes", thinking_source="engine")
    with caplog.at_level(logging.WARNING):
        async with _running(tmp_path, _peer_service(tmp_path, result=result)) as (server, ledger):
            status, text = await _request(server, "POST", "/v1/chat/completions",
                                          key=_key(tmp_path), body=_chat(REMOTE_MODEL))
            assert status == 200, text
            (row,) = ledger.rows()

    assert json.loads(text)["choices"][0]["message"]["content"]
    assert (row["completion_tokens"], row["thinking_tokens"]) == (22, 22)
    assert row["thinking_source"] == "estimated"
    assert not [r for r in caplog.records if "was not built" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_third_word_reaching_the_gateway_leaves_the_row_written_and_silent(tmp_path, caplog):
    result = _priced_result(thinking_source="guessed")
    with caplog.at_level(logging.WARNING):
        async with _running(tmp_path, _peer_service(tmp_path, result=result)) as (server, ledger):
            status, text = await _request(server, "POST", "/v1/chat/completions",
                                          key=_key(tmp_path), body=_chat(REMOTE_MODEL))
            assert status == 200, text
            (row,) = ledger.rows()

    assert row["thinking_source"] is None
    assert not [r for r in caplog.records if "was not built" in r.getMessage()]
    assert len([r for r in caplog.records
                if GATEWAY_PEER in r.getMessage() and "'guessed'" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_the_agents_row_carries_the_word_from_the_providers_usage_dict(tmp_path):
    from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter

    class _Provider:
        alias = "local_qwen38"
        model = "qwen3:8b"

        async def generate_response(self, prompt, **kwargs):
            return "short answer"

        def get_last_usage(self):
            return {"prompt_tokens": 8, "completion_tokens": 22, "total_tokens": 30,
                    "reasoning_tokens": 22, "output_includes_thinking": "includes",
                    "thinking_source": "estimated"}

    ledger = NodeLedger(tmp_path / "ledger")
    manager = SimpleNamespace(
        token_count_manager=None, providers={"local_qwen38": _Provider()},
        agent_provider=None, default_provider="local_qwen38",
    )
    adapter = DpcLlmAdapter(manager, provider_alias="local_qwen38", caller="agent_test", ledger=ledger)

    await adapter.chat([{"role": "user", "content": "x"}])

    (row,) = ledger.rows()
    assert row["thinking_source"] == "estimated"
    assert row["thinking_tokens"] <= row["completion_tokens"]
