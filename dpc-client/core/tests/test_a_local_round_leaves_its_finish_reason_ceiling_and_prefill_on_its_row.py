"""A local round leaves why it stopped, its ceiling and its prefill on its ledger row.

`finish_reason`, `max_tokens`, `prefilled_tokens` and `cached_tokens` were put
into the llama-server provider's usage dict on every round and reached only the
rotating dpc-client.log, so the xhigh check, the parking value and the
wall-seconds series had no durable source
(FINISH-REASON-MAX-TOKENS-AND-PREFILL-REACH-NO-DURABLE-PER-ROUND-RECORD). The row
now carries them beside the engine's raw clock for each phase (`prompt_ms`,
`predicted_ms`): counters and milliseconds, never a rate.

The second half is the fully cached request. Strata sends `prompt_n: 0` and
`prompt_per_second: null` when the conversation cache held the whole prompt, and
the timings reader used to discard the entire block when either rate was
missing — `cache_n` with it, the one number that says the cache worked.

Cross-platform: pure asyncio, no engine, no network.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.node_ledger import ENGINE_FIELDS, NodeLedger, usage_row
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS, _timings_from_response
from tests.test_llamacpp_server_provider import _FakeSupervisor, _chat_resp, _fake_client
from tests.test_strata_supervisor import _install

SEPT_1 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
MESSAGES = [{"role": "user", "content": "read the file"}]
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "read", "parameters": {"type": "object", "properties": {}},
}}]

#: A warm round in Strata's own shape (serve/server.py `request_timings`):
#: 46 976 sent, 45 184 reused, 1 792 read.
WARM = {
    "cache_n": 45184, "prompt_n": 1792, "prompt_ms": 2310.4, "prompt_per_token_ms": 1.289,
    "prompt_per_second": 775.6, "predicted_n": 189, "predicted_ms": 4120.7,
    "predicted_per_token_ms": 21.803, "predicted_per_second": 45.9,
    "draft_n": 120, "draft_n_accepted": 84,
}
#: The whole prompt came from the conversation cache: nothing read, no prefill rate.
FULLY_CACHED = {
    "cache_n": 46976, "prompt_n": 0, "prompt_ms": 3.1, "prompt_per_token_ms": None,
    "prompt_per_second": None, "predicted_n": 26, "predicted_ms": 590.2,
    "predicted_per_token_ms": 22.7, "predicted_per_second": 44.1,
}


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


def _strata(tmp_path, timings, *, prompt_tokens, completion_tokens, finish_reason="tool_calls"):
    config = {
        "type": "strata", "alias": "flash", "model": "qwen3.8-flash-next-iq3_xxs",
        "strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, "context_window": 200000, "max_tokens": 16384,
    }
    p = StrataProvider("flash", config)
    p.supervisor = _FakeSupervisor()
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="read_file", arguments="{}"))
    resp = _chat_resp(
        content="", tool_calls=[tc], finish_reason=finish_reason,
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                              total_tokens=prompt_tokens + completion_tokens,
                              completion_tokens_details=None),
    )
    resp.model_extra = {"timings": timings}
    client, _ = _fake_client(resp)

    async def _ensure():
        return client

    p._ensure = _ensure
    return p


def _adapter(provider, ledger):
    manager = SimpleNamespace(
        token_count_manager=None, providers={"flash": provider},
        agent_provider=None, default_provider="flash",
    )
    return DpcLlmAdapter(manager, provider_alias="flash", caller="agent_test", ledger=ledger)


def _row(**overrides):
    fields = dict(
        request_id="req-1", caller="agent_001", caller_kind="agent",
        alias="ds_flash", model="deepseek-v4-flash", route="local",
        prompt_tokens=1000, completion_tokens=900, thinking_tokens=None,
        counts_source="engine", started_at=SEPT_1, duration_s=1.25,
        billing="pay_per_use", cost_amount=0.0041, cost_currency="USD", cost_basis="charged",
    )
    fields.update(overrides)
    return usage_row(**fields)


# --- the Strata tools round, end to end through the adapter ------------------------


@pytest.mark.asyncio
async def test_a_strata_tools_round_writes_the_engines_word_on_its_row(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    provider = _strata(tmp_path, WARM, prompt_tokens=46976, completion_tokens=189)

    await _adapter(provider, ledger).chat(MESSAGES, tools=TOOLS)

    (row,) = list(ledger.rows())
    assert row["alias"] == "flash" and row["tool_calls"] == 1
    assert row["finish_reason"] == "tool_calls"
    assert row["max_tokens"] == 16384
    assert (row["prefilled_tokens"], row["cached_tokens"]) == (1792, 45184)
    assert (row["prompt_ms"], row["predicted_ms"]) == (2310.4, 4120.7)
    # Counters and clocks, not a rate: the speed stays on the live counter.
    assert not [key for key in row if key.endswith("tok_s")]


@pytest.mark.asyncio
async def test_a_round_cut_at_the_ceiling_says_length_beside_the_ceiling(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    provider = _strata(tmp_path, WARM, prompt_tokens=46976, completion_tokens=16384,
                       finish_reason="length")

    await _adapter(provider, ledger).chat(MESSAGES, tools=TOOLS)

    (row,) = list(ledger.rows())
    assert (row["finish_reason"], row["completion_tokens"], row["max_tokens"]) == ("length", 16384, 16384)


# --- the fully cached request -------------------------------------------------------


def test_a_null_rate_does_not_throw_away_the_counters_beside_it():
    out = _timings_from_response(FULLY_CACHED)

    assert out["engine_cached_tokens"] == 46976
    assert out["engine_prompt_tokens"] == 0
    assert out["prompt_ms"] == 3.1
    assert "prefill_tok_s" not in out, "a rate the engine did not state is not made up"
    assert out["decode_tok_s"] == 44


@pytest.mark.asyncio
async def test_a_fully_cached_strata_round_still_records_its_cache_hit(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    provider = _strata(tmp_path, FULLY_CACHED, prompt_tokens=46976, completion_tokens=26)

    await _adapter(provider, ledger).chat(MESSAGES, tools=TOOLS)

    (row,) = list(ledger.rows())
    assert (row["prefilled_tokens"], row["cached_tokens"]) == (0, 46976)
    assert row["prompt_ms"] == 3.1
    # The live counter gets no engine split it was not given.
    speed = provider.get_last_usage()["speed"]
    assert speed.get("speed_source") != "engine"


# --- a row without them -------------------------------------------------------------


def test_a_row_without_the_engines_word_validates_and_writes_nulls():
    row = _row()
    assert all(row[name] is None for name in ENGINE_FIELDS)


@pytest.mark.asyncio
async def test_a_provider_that_reports_none_of_them_leaves_them_null(tmp_path):
    """A vendor's usage dict (DeepSeek, Anthropic, Ollama) carries none of the
    keys; its row reads null, never a zero nobody measured."""

    class _Vendor:
        alias, model = "flash", "deepseek-v4-flash"

        async def generate_with_tools(self, messages, tools, **kwargs):
            return {"content": "ok", "tool_calls_raw": [], "thinking": None,
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}

        def get_last_usage(self):
            return {}

    ledger = NodeLedger(tmp_path / "ledger")
    await _adapter(_Vendor(), ledger).chat(MESSAGES, tools=TOOLS)

    (row,) = list(ledger.rows())
    assert all(row[name] is None for name in ENGINE_FIELDS)


def test_a_row_written_before_the_columns_reads_them_as_null(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    old = _row()
    for name in ENGINE_FIELDS:
        del old[name]
    ledger.append(old)

    (row,) = list(ledger.rows())
    assert all(row[name] is None for name in ENGINE_FIELDS)


@pytest.mark.parametrize("bad", [
    dict(finish_reason=1),
    dict(max_tokens=0), dict(max_tokens=-1), dict(max_tokens="8192"), dict(max_tokens=True),
    dict(prefilled_tokens=-1), dict(prefilled_tokens=1.5), dict(cached_tokens="3"), dict(cached_tokens=True),
    dict(prompt_ms=-0.1), dict(prompt_ms=float("nan")), dict(predicted_ms="590"), dict(predicted_ms=False),
])
def test_a_value_of_the_wrong_type_is_refused(bad):
    (name,) = bad
    with pytest.raises(ValueError, match=name):
        _row(**bad)


def test_an_integer_clock_is_written_as_milliseconds_unchanged():
    row = _row(prompt_ms=0, predicted_ms=590)
    assert (row["prompt_ms"], row["predicted_ms"]) == (0.0, 590.0)
