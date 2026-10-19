"""The engine's counters reach the row whether or not our own clock read anything.

`prefilled_tokens`, `cached_tokens`, `prompt_ms` and `predicted_ms` are the
engine's word about the request (ff68c996). They were copied onto usage inside
the branch that builds the client-side speed payload, which waits for
`elapsed_s > 0` — so a call whose wall-clock read zero, or was never taken,
lost counters that depend on no clock of ours
(ENGINE-TIMINGS-ARE-DROPPED-WHEN-THE-CLIENT-CLOCK-READS-ZERO). Only the speed
payload is ours, and it stays absent without a clock.

Cross-platform: no engine, no network.
"""

import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.providers import LlamaServerProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from tests.test_llamacpp_server_provider import _FakeSupervisor

#: A warm round in the response body's shape: 46 976 sent, 45 184 reused.
WARM = {
    "cache_n": 45184, "prompt_n": 1792, "prompt_ms": 2310.4,
    "prompt_per_second": 775.6, "predicted_n": 189, "predicted_ms": 4120.7,
    "predicted_per_second": 45.9, "draft_n": 120, "draft_n_accepted": 84,
}
ENGINE_KEYS = ("prefilled_tokens", "cached_tokens", "prompt_ms", "predicted_ms")


@pytest.fixture(autouse=True)
def _clean_registry():
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


def _record(elapsed_s, engine_timings, *, log_timings=None):
    p = LlamaServerProvider("local", {"type": "llamacpp_server", "gguf_path": "D:/m/q.gguf"})
    p.supervisor = _FakeSupervisor()
    p.supervisor.last_task_timings = lambda: log_timings
    raw = SimpleNamespace(
        prompt_tokens=46976, completion_tokens=189, total_tokens=47165,
        completion_tokens_details=None,
    )
    out = p._record_usage(raw, path="tools", elapsed_s=elapsed_s, engine_timings=engine_timings)
    return p, out


@pytest.mark.parametrize("clock", [0, 0.0, None])
def test_a_missing_client_clock_keeps_the_engines_counters(clock):
    p, out = _record(clock, WARM)

    assert (out["prefilled_tokens"], out["cached_tokens"]) == (1792, 45184)
    assert (out["prompt_ms"], out["predicted_ms"]) == (2310.4, 4120.7)
    last = p.get_last_usage()
    assert (last["prefilled_tokens"], last["cached_tokens"]) == (1792, 45184)


@pytest.mark.parametrize("clock", [0, None])
def test_a_missing_client_clock_still_invents_no_speed(clock):
    _, out = _record(clock, WARM)

    assert "speed" not in out, "the speed payload is ours and waits for our clock"


@pytest.mark.parametrize("clock", [0, None])
def test_the_log_fallback_survives_a_missing_clock_too(clock):
    _, out = _record(clock, None, log_timings={
        "prefill_tok_s": 800, "decode_tok_s": 40, "engine_prompt_tokens": 4000,
    })

    assert (out["prefilled_tokens"], out["cached_tokens"]) == (4000, 46976 - 4000)


@pytest.mark.parametrize("clock", [0, None, 12.5])
def test_no_timings_means_nothing_is_invented(clock):
    _, out = _record(clock, None)

    assert not [key for key in ENGINE_KEYS if key in out]


def test_the_usage_line_agrees_with_the_row_without_a_clock(caplog):
    with caplog.at_level(logging.INFO, logger="dpc_client_core.providers.llamacpp_server_provider"):
        _record(0, WARM)

    assert any("prefilled=1792 of 46976 (reuse=96.2%)" in r.getMessage() for r in caplog.records)


def test_with_a_clock_the_row_and_the_speed_are_as_before():
    _, out = _record(12.5, WARM)

    assert (out["prefilled_tokens"], out["cached_tokens"]) == (1792, 45184)
    assert (out["prompt_ms"], out["predicted_ms"]) == (2310.4, 4120.7)
    assert out["speed"]["speed_source"] == "engine"
    assert (out["speed"]["prefill_tok_s"], out["speed"]["decode_tok_s"]) == (775, 45)
    assert out["speed"]["draft_acceptance"] == pytest.approx(0.7)
