"""The live agent header said how fast and how full, and never how much of the
prompt the engine did not have to read again.

Today's ledger showed agent calls at about 98% reuse and a peer's calls at 0%,
and the header showed neither. The header's «cache hit rate: N%» is made of two counts
of the round that just finished: `cached_tokens` and `prompt_tokens`. They travel
as counts, not as a percentage, so the finished header can add a turn up instead
of averaging rounds — the first call of a turn often reuses nothing, and an
average of percentages hides it.

Two decisions the tests hold in place:

- absent stays absent: a provider that did not state the reused count, a prompt
  of 0 tokens and a count larger than its prompt all leave the pair out — a
  percentage nobody reported would be made up;
- a real 0 is a number: a provider that said `cached_tokens` = 0 sends 0.
"""

import asyncio

from dpc_client_core.dpc_agent.loop import round_progress_payload, run_llm_loop

WINDOW = 262144
RESERVE = 16384


def _payload(speed=None, prompt_tokens=113343, cached_tokens=111593, window=WINDOW):
    return round_progress_payload(
        speed,
        round_idx=4,
        prompt_tokens=prompt_tokens,
        context_window=window,
        context_reserve=RESERVE,
        cached_tokens=cached_tokens,
    )


class TestThePayloadCarriesTheTwoCounts:

    def test_the_round_reports_what_it_reused_against_what_it_sent(self):
        payload = _payload()
        assert payload["cached_tokens"] == 111593
        assert payload["prompt_tokens"] == 113343

    def test_the_local_engines_speed_half_stays_beside_it(self):
        speed = {"prompt_tokens": 113343, "prefill_tok_s": 845, "decode_tok_s": 54}
        payload = _payload(dict(speed))
        assert payload["prefill_tok_s"] == 845
        assert payload["prompt_tokens"] == 113343
        assert payload["cached_tokens"] == 111593

    def test_a_provider_without_speed_still_carries_the_pair(self):
        """A cloud provider that states a cache split has no speed dict; the
        counts must not depend on one."""
        payload = _payload(None)
        assert (payload["cached_tokens"], payload["prompt_tokens"]) == (111593, 113343)

    def test_the_pair_alone_is_a_strip(self):
        """No window known: the cache half is still worth sending."""
        payload = _payload(None, window=None)
        assert payload["cached_tokens"] == 111593
        assert "context_used" not in payload

    def test_the_providers_own_dict_is_not_mutated(self):
        speed = {"prefill_tok_s": 845}
        _payload(speed)
        assert "cached_tokens" not in speed


class TestAbsentStaysAbsentAndZeroStaysZero:

    def test_a_real_zero_is_sent(self):
        payload = _payload(cached_tokens=0)
        assert payload["cached_tokens"] == 0
        assert payload["prompt_tokens"] == 113343

    def test_an_unstated_count_sends_no_pair(self):
        payload = _payload(cached_tokens=None)
        assert "cached_tokens" not in payload

    def test_an_unstated_count_and_no_window_is_silent(self):
        assert _payload(None, cached_tokens=None, window=None) is None

    def test_a_prompt_of_zero_sends_no_pair(self):
        assert _payload(prompt_tokens=0, cached_tokens=0) is None

    def test_a_count_larger_than_its_prompt_is_not_believed(self):
        payload = _payload(prompt_tokens=1000, cached_tokens=1001)
        assert "cached_tokens" not in payload

    def test_a_bool_is_not_a_count(self):
        payload = _payload(prompt_tokens=1000, cached_tokens=True)
        assert "cached_tokens" not in payload


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, message, tool=None, round=None, tool_calls=None, speed=None):
        self.calls.append({"message": message, "round": round, "speed": speed})

    @property
    def strips(self):
        return [c["speed"] for c in self.calls if c["speed"]]


class _Llm:
    def __init__(self, usage):
        self._usage = usage

    async def chat(self, messages, tools=None, on_stream_chunk=None,
                   conversation_id=None, reasoning_effort=None, **kwargs):
        return {"content": "done", "tool_calls": []}, self._usage


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return []


def _run(usage, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.loop.load_agent_config", lambda _name: {},
    )
    recorder = _Recorder()
    response, _usage, _trace = asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=_Tools(),
        llm=_Llm(usage),
        agent_root=tmp_path,
        emit_progress=recorder,
        context_window=WINDOW,
        context_reserve=RESERVE,
    ))
    assert response == "done"
    return recorder


class TestItReachesTheCallbackTheUiListensOn:
    """Computed and correct and never delivered is this repo's most frequent
    defect shape: the pair is worth something only at the far end of the wire."""

    def test_a_local_engine_round_hands_over_its_reused_count(self, tmp_path, monkeypatch):
        usage = {
            "prompt_tokens": 113343, "completion_tokens": 400, "total_tokens": 113743,
            "prefilled_tokens": 1750, "cached_tokens": 111593,
            "speed": {"prompt_tokens": 113343, "prefill_tok_s": 845, "decode_tok_s": 54},
        }
        strip = _run(usage, tmp_path, monkeypatch).strips[-1]
        assert (strip["cached_tokens"], strip["prompt_tokens"]) == (111593, 113343)

    def test_a_vendor_round_hands_over_the_hit_it_was_priced_on(self, tmp_path, monkeypatch):
        """The same helper the ledger row uses: DeepSeek's hit/miss fields."""
        usage = {
            "prompt_tokens": 50000, "completion_tokens": 300, "total_tokens": 50300,
            "prompt_cache_hit_tokens": 45000, "prompt_cache_miss_tokens": 5000,
        }
        strip = _run(usage, tmp_path, monkeypatch).strips[-1]
        assert (strip["cached_tokens"], strip["prompt_tokens"]) == (45000, 50000)

    def test_a_real_zero_reaches_the_callback(self, tmp_path, monkeypatch):
        usage = {"prompt_tokens": 800, "completion_tokens": 30, "total_tokens": 830,
                 "cached_tokens": 0, "prefilled_tokens": 800}
        strip = _run(usage, tmp_path, monkeypatch).strips[-1]
        assert strip["cached_tokens"] == 0

    def test_a_round_that_stated_no_split_carries_no_pair(self, tmp_path, monkeypatch):
        usage = {"prompt_tokens": 3030, "completion_tokens": 120, "total_tokens": 3150}
        strip = _run(usage, tmp_path, monkeypatch).strips[-1]
        assert "cached_tokens" not in strip
        assert strip["context_used"] == 3030
