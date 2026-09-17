"""Where the engine reported counts, the row and the wire carry them.

`LLMManager.query` counted every call for itself — `count_tokens` over the
prompt string and over the visible answer — and handed that estimate to both
doors, whatever the provider had reported. On 2026-09-14 the host's rows for two
peer calls carrying a 64x64 PNG read `prompt_tokens: 13, completion_tokens: 0,
counts_source: ours` while the provider's own usage line for the same calls read
`prompt=38, completion=2`: the recount sees neither the chat template nor the
image, and the answer it counts has already had the thinking taken out of it. A
tariff would be charged on the estimate.

The rule here is the one `llm_adapter` has always applied: the engine's numbers
where the engine spoke, `counts_source: engine`; the recount only where it did
not, `counts_source: ours`. The convention travels with the counts —
`output_includes_thinking` comes from the engine's own dict, because a count
made under `includes` and billed as `excludes` pays for the reasoning twice.

Cross-platform: pure asyncio, no engine, no network.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.node_ledger import NodeLedger
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
from tests.test_p2p_coordinator import make_coordinator

# What the engine said about the two live image calls, and what the recount
# over the visible text said instead.
ENGINE = {"prompt_tokens": 38, "completion_tokens": 2, "output_includes_thinking": "includes"}


class _Counting(_Plain):
    """A provider whose engine reports its own counts, as llama-server does."""

    def __init__(self, usage=None):
        super().__init__(answer="Red")
        self.usage = ENGINE if usage is None else usage

    async def generate_response(self, prompt, **kwargs):
        self._record_last_usage(self.usage)
        return await super().generate_response(prompt, **kwargs)

    async def generate_with_vision(self, prompt, images, **kwargs):
        return await self.generate_response(prompt, **kwargs)

    def supports_vision(self):
        return True


# --- the door prefers the engine ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_prompt_door_carries_the_engines_counts_and_its_convention(tmp_path):
    manager = _manager(tmp_path, _Counting())

    result = await manager.query(FLAT, return_metadata=True)

    assert (result["prompt_tokens"], result["response_tokens"]) == (38, 2)
    assert result["tokens_used"] == 40
    assert result["counts_source"] == "engine"
    assert result["output_includes_thinking"] == "includes"


@pytest.mark.asyncio
async def test_an_image_call_carries_the_tokens_the_picture_cost(tmp_path):
    """The live case: the recount saw 13 where the engine had prefilled 38."""
    manager = _manager(tmp_path, _Counting())

    result = await manager.query("what colour?", return_metadata=True,
                                 images=[{"base64": "AAAA", "mime_type": "image/png"}])

    assert (result["prompt_tokens"], result["response_tokens"]) == (38, 2)
    assert result["counts_source"] == "engine"


@pytest.mark.asyncio
async def test_the_message_door_follows_the_same_rule(tmp_path):
    manager = _manager(tmp_path, _Counting())

    result = await manager.query_messages(MESSAGES, system=SYSTEM, return_metadata=True)

    assert (result["prompt_tokens"], result["response_tokens"]) == (38, 2)
    assert result["counts_source"] == "engine"
    assert result["output_includes_thinking"] == "includes"


@pytest.mark.asyncio
async def test_a_silent_provider_is_still_counted_here(tmp_path):
    manager = _manager(tmp_path, _Plain())

    result = await manager.query(FLAT, return_metadata=True)

    assert result["counts_source"] == "ours"
    assert result["output_includes_thinking"] == "excludes"
    assert result["prompt_tokens"] > 0


@pytest.mark.asyncio
async def test_an_engine_that_named_no_convention_leaves_it_unknown(tmp_path):
    """`unknown` is «nothing may be billed from this count», and it is the
    engine's own silence — never the door's `excludes`, which is a claim about
    a recount that did not happen."""
    manager = _manager(tmp_path, _Counting({"prompt_tokens": 38, "completion_tokens": 2}))

    result = await manager.query(FLAT, return_metadata=True)

    assert result["counts_source"] == "engine"
    assert result["output_includes_thinking"] == "unknown"


# --- the gateway's local row ---------------------------------------------------------


def _engine_service(tmp_path, **counts):
    """The loopback stand-in whose door reports the engine's numbers."""
    service = _service(tmp_path, BOTH_LISTS)
    inner = service.llm_manager.query_messages

    async def query_messages(messages, **kwargs):
        return dict(await inner(messages, **kwargs), **counts)

    service.llm_manager.query_messages = query_messages
    return service


@pytest.mark.asyncio
async def test_a_local_row_carries_the_engines_counts_under_its_own_name(tmp_path):
    service = _engine_service(tmp_path, prompt_tokens=38, response_tokens=2,
                              counts_source="engine", output_includes_thinking="includes")
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200, text

        (row,) = ledger.rows()
        assert (row["prompt_tokens"], row["completion_tokens"]) == (38, 2)
        assert row["counts_source"] == "engine"
        assert row["output_includes_thinking"] == "includes"


@pytest.mark.asyncio
async def test_a_local_row_says_ours_where_the_door_counted(tmp_path):
    service = _engine_service(tmp_path, counts_source="ours", output_includes_thinking="excludes")
    async with _running(tmp_path, service) as (server, ledger):
        status, text = await _request(server, "POST", "/v1/chat/completions",
                                      key=_key(tmp_path), body=_chat(LOCAL))
        assert status == 200, text

        (row,) = ledger.rows()
        assert row["counts_source"] == "ours"


# --- the host's row and the wire ------------------------------------------------------


def _host(tmp_path, **counts):
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.providers = {"ollama_local": SimpleNamespace(config={})}
    answer = {"response": "Red", "model": "qwen3.8", "provider": "ollama_local",
              "tokens_used": 40, "thinking_tokens": None}
    answer.update(counts)
    svc.llm_manager.query = AsyncMock(return_value=answer)
    coord._ledger = NodeLedger(tmp_path / "ledger")
    return coord, svc


@pytest.mark.asyncio
async def test_the_hosts_row_and_the_wire_carry_the_engines_counts(tmp_path):
    coord, svc = _host(tmp_path, prompt_tokens=38, response_tokens=2,
                       counts_source="engine", output_includes_thinking="includes")

    await coord.handle_inference_request("peer-1", "req-1", "what colour?",
                                         images=[{"base64": "AAAA"}])

    (row,) = coord._ledger.rows()
    assert (row["prompt_tokens"], row["completion_tokens"]) == (38, 2)
    assert row["counts_source"] == "engine"
    assert row["output_includes_thinking"] == "includes"
    payload = svc.p2p_manager.send_message_to_peer.call_args[0][1]["payload"]
    assert (payload["prompt_tokens"], payload["response_tokens"]) == (38, 2)
    assert payload["output_includes_thinking"] == "includes"


@pytest.mark.asyncio
async def test_the_hosts_row_says_ours_where_nothing_was_reported(tmp_path):
    coord, _ = _host(tmp_path, prompt_tokens=13, response_tokens=0,
                     counts_source="ours", output_includes_thinking="excludes")

    await coord.handle_inference_request("peer-1", "req-1", "ping")

    (row,) = coord._ledger.rows()
    assert (row["prompt_tokens"], row["counts_source"]) == (13, "ours")
