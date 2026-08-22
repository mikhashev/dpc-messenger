"""The live agent header said how fast a round ran and never how full it was.

The pair that decides whether the next round happens at all — the round's real
input against the model window — was computed once per turn in `agent.py`,
compared against the reserve, and then dropped into a DEBUG line. Nobody
watching an agent work could see the number that stops it.

These cover the payload that now carries it, and the two decisions inside that
a later tidy-up would otherwise reverse without noticing:

- the denominator is the RAW window, so the strip and the provider config agree;
  the reserve is carried beside the pair, never subtracted from it;
- the numerator is the round's measured input, so the pair belongs to the same
  round as the speed shown next to it — and absent stays absent, because a
  "0 / 262 144" would read as an empty context rather than an unknown one.
"""

import asyncio

from dpc_client_core.dpc_agent.loop import round_progress_payload, run_llm_loop

WINDOW = 262144
RESERVE = 16384
SPEED = {
    "alias": "local",
    "model": "qwen 3.8 27b",
    "prompt_tokens": 107431,
    "completion_tokens": 900,
    "elapsed_s": 24.5,
    "prefill_tok_s": 845,
    "decode_tok_s": 54,
}


def _payload(speed=None, prompt_tokens=107431, window=WINDOW, reserve=RESERVE, round_idx=7):
    return round_progress_payload(
        speed,
        round_idx=round_idx,
        prompt_tokens=prompt_tokens,
        context_window=window,
        context_reserve=reserve,
    )


class TestTheStripCarriesThePairTheGuardDecidesOn:

    def test_the_numerator_is_what_the_round_actually_sent(self):
        """Not the pre-turn estimate: the provider counted this one, and it
        belongs to the same round as the speed beside it."""
        assert _payload(dict(SPEED))["context_used"] == 107431

    def test_the_denominator_is_the_raw_window(self):
        """The window minus the reserve would be the number that blocks, but it
        is not the number in the provider config — and a strip that disagrees
        with the configuration reads as broken."""
        payload = _payload(dict(SPEED))
        assert payload["context_window"] == WINDOW
        assert payload["context_window"] != WINDOW - RESERVE

    def test_the_reserve_travels_beside_the_pair(self):
        """Carried, not folded in: it is what makes a full bar explainable."""
        assert _payload(dict(SPEED))["context_reserve"] == RESERVE

    def test_the_round_number_travels_with_it(self):
        assert _payload(dict(SPEED), round_idx=11)["round"] == 11

    def test_the_speed_half_passes_through_untouched(self):
        payload = _payload(dict(SPEED))
        assert payload["prefill_tok_s"] == 845
        assert payload["decode_tok_s"] == 54
        assert payload["model"] == "qwen 3.8 27b"

    def test_the_providers_own_dict_is_not_mutated(self):
        provider_speed = dict(SPEED)
        _payload(provider_speed)
        assert "context_used" not in provider_speed
        assert "round" not in provider_speed


class TestOccupancyIsNotLlamaCppOnly:
    """The speed half exists only where a provider reports it. The window comes
    from the agent, so the occupancy half must not inherit that limit — seven of
    nine agents run on an API provider that reports no speed at all."""

    def test_a_provider_that_reports_no_speed_still_reports_occupancy(self):
        payload = _payload(None)
        assert payload["context_used"] == 107431
        assert payload["context_window"] == WINDOW
        assert "prefill_tok_s" not in payload

    def test_an_empty_speed_dict_is_treated_as_no_speed(self):
        assert _payload({})["context_window"] == WINDOW


class TestAbsentStaysAbsent:

    def test_no_window_leaves_the_speed_alone(self):
        payload = _payload(dict(SPEED), window=None)
        assert payload["prefill_tok_s"] == 845
        assert "context_used" not in payload
        assert "context_window" not in payload

    def test_a_round_that_reported_no_input_size_gets_no_pair(self):
        """The silent-drop case returns prompt_tokens 0. Showing "0 / 262 144"
        would say the context is empty, which is the opposite of what happened."""
        payload = _payload(dict(SPEED), prompt_tokens=0)
        assert "context_used" not in payload

    def test_no_reserve_means_no_reserve_key(self):
        payload = _payload(dict(SPEED), reserve=None)
        assert "context_reserve" not in payload
        assert payload["context_window"] == WINDOW

    def test_neither_half_emits_nothing_at_all(self):
        """No speed and no window is the old silent round; it must stay silent
        rather than emit an empty strip."""
        assert _payload(None, window=None) is None
        assert _payload({}, window=None) is None


class _Recorder:
    """The UI's end of the wire: what emit_progress was actually handed."""

    def __init__(self):
        self.calls = []

    def __call__(self, message, tool=None, round=None, tool_calls=None, speed=None):
        self.calls.append({"message": message, "round": round, "speed": speed})

    @property
    def strips(self):
        return [c["speed"] for c in self.calls if c["speed"]]


class _Llm:
    """One round, final answer, no tools — the shape that used to carry no
    counter at all."""

    def __init__(self, usage):
        self._usage = usage

    async def chat(self, messages, tools=None, on_stream_chunk=None,
                   conversation_id=None, reasoning_effort=None):
        return {"content": "done", "tool_calls": []}, self._usage


class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return []


def _run(usage, tmp_path, monkeypatch, **kwargs):
    # run_llm_loop reads an agent config keyed by the ROOT DIRECTORY NAME, and
    # the helper that resolves that path creates ~/.dpc/agents/<name>/ as a side
    # effect of the read. A test run must not leave a directory in the user's
    # real agent registry, so the read is stubbed out.
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
        **kwargs,
    ))
    assert response == "done"
    return recorder


class TestItReachesTheCallbackTheUiListensOn:
    """Computed and correct and never delivered is this repo's most frequent
    defect shape; the payload is only worth anything at the far end of the wire."""

    def test_the_loop_hands_the_occupancy_to_the_progress_callback(self, tmp_path, monkeypatch):
        usage = {"prompt_tokens": 107431, "completion_tokens": 900,
                 "total_tokens": 108331, "speed": dict(SPEED)}
        strips = _run(usage, tmp_path, monkeypatch, context_window=WINDOW, context_reserve=RESERVE).strips
        assert strips, "the round emitted no strip at all"
        assert strips[-1]["context_used"] == 107431
        assert strips[-1]["context_window"] == WINDOW
        assert strips[-1]["context_reserve"] == RESERVE
        assert strips[-1]["round"] == 1

    def test_an_api_provider_round_now_emits_a_strip_it_never_had(self, tmp_path, monkeypatch):
        usage = {"prompt_tokens": 3030, "completion_tokens": 120, "total_tokens": 3150}
        strips = _run(usage, tmp_path, monkeypatch, context_window=949386, context_reserve=16384).strips
        assert strips, "a provider without speed still has a window to report"
        assert strips[-1]["context_used"] == 3030
        assert strips[-1]["context_window"] == 949386

    def test_a_round_with_neither_stays_silent(self, tmp_path, monkeypatch):
        usage = {"prompt_tokens": 3030, "completion_tokens": 120, "total_tokens": 3150}
        assert _run(usage, tmp_path, monkeypatch).strips == []


class _StubMonitor:
    """Only the surface get_session_state reads."""

    def __init__(self, token_limit, tokens_used=51140):
        self._limit = token_limit
        self._used = tokens_used
        self._tokens_after_last_response = tokens_used
        self._tokens_after_last_response_at = "2026-08-20T13:18:51Z"
        self.message_history = []

    def get_token_usage(self):
        return {"token_limit": self._limit, "tokens_used": self._used}


def _manager(monitor, *, provider_alias="llama.cpp", window=262144, config=None):
    """A manager without its constructor: building one for real creates
    ~/.dpc/agents/<id>/ on the user's disk, and this test is about a lookup."""
    from types import SimpleNamespace
    from dpc_client_core.managers.agent_manager import DpcAgentManager

    mgr = object.__new__(DpcAgentManager)
    mgr.config = config if config is not None else {"provider_alias": provider_alias}
    mgr.agent_id = "agent_test"
    mgr._agent_monitors = {"group-x": monitor} if monitor is not None else {}
    mgr._last_used_agent = None
    llm_manager = SimpleNamespace(
        providers={provider_alias: SimpleNamespace(model="qwen 3.8 27b")},
        get_context_window=lambda model: window,
        get_active_model_name=lambda: "qwen 3.8 27b",
    )
    mgr.service = SimpleNamespace(
        llm_manager=llm_manager,
        peer_metadata={},
        get_group_agent_context=lambda *a, **k: None,
    )
    return mgr


class TestTheWindowBelongsToTheAgentNotToTheGroup:
    """Measured 2026-08-20: a local agent in a group ran against a denominator of
    1 000 000 while its model's window is 262 144. The group monitor's
    token_limit is set to the LARGEST window among the group's agents, and
    history.json persists that number, so every monitor that later loads the file
    inherits it — including the per-agent one this manager owns.

    The strip is the visible half. The same number calibrates the overflow guard
    and its reserve, so on that agent the guard would have refused a round only
    near 950K, roughly four times past the point where the model itself dies."""

    def test_the_agents_own_window_beats_the_shared_one(self):
        state = _manager(_StubMonitor(token_limit=1000000)).get_session_state("group-x")
        assert state["tokens_limit"] == 262144

    def test_a_config_override_still_wins_over_the_provider(self):
        """An explicitly configured window is the agent's own setting too."""
        mgr = _manager(
            _StubMonitor(token_limit=1000000),
            config={"provider_alias": "llama.cpp", "context_window": 131072},
        )
        assert mgr.get_session_state("group-x")["tokens_limit"] == 131072

    def test_an_api_agent_keeps_its_large_window(self):
        """The fix must not shrink an agent that genuinely has a 1M window."""
        mgr = _manager(_StubMonitor(token_limit=262144),
                       provider_alias="deepseek_pro", window=1000000)
        assert mgr.get_session_state("group-x")["tokens_limit"] == 1000000

    def test_with_no_monitor_the_answer_is_still_the_agents_window(self):
        assert _manager(None).get_session_state("group-x")["tokens_limit"] == 262144

    def test_an_unresolvable_window_falls_back_to_what_is_known(self):
        """Absent stays absent: with nothing to resolve from, the monitor's
        number is better than a hardcoded default."""
        mgr = _manager(_StubMonitor(token_limit=131072))
        mgr.service.llm_manager = None
        assert mgr.get_session_state("group-x")["tokens_limit"] == 131072


class TestTheContextGuardIsActuallyFedByTheLoop:
    """Registering a guard and feeding it are two different things.

    Neutralising `ctx.state.last_prompt_tokens` in the loop left every unit test
    green: the guard's own tests build their context by hand, so they cannot
    notice that production never fills it. That is this repo's most frequent
    defect shape — computed, correct, never delivered — and it was sitting inside
    the fix for it. This test drives the real `run_llm_loop`.
    """

    class _ToolsThatRun:
        _ctx = None

        def schemas(self, core_only=False, include_restricted=False):
            return [{"type": "function", "function": {"name": "noop", "parameters": {}}}]

        def get_timeout(self, name):
            return 5

        def execute(self, name, args, ctx=None):
            return "ok"

    class _LlmTwoRounds:
        """Round one asks for a tool, which is what buys a second round; the
        second call is the finalizer the guard-stop path makes without tools."""

        def __init__(self, prompt_tokens):
            self._prompt_tokens = prompt_tokens
            self.seen_final_messages = None
            self.calls = 0

        async def chat(self, messages, tools=None, on_stream_chunk=None,
                       conversation_id=None, reasoning_effort=None):
            self.calls += 1
            if tools is None:
                self.seen_final_messages = list(messages)
                return {"content": "wrapped up", "tool_calls": []}, {}
            return (
                {"content": "", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "noop", "arguments": "{}"}}]},
                {"prompt_tokens": self._prompt_tokens, "completion_tokens": 10,
                 "total_tokens": self._prompt_tokens + 10},
            )

    def _drive(self, prompt_tokens, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "dpc_client_core.dpc_agent.loop.load_agent_config", lambda _name: {},
        )
        llm = self._LlmTwoRounds(prompt_tokens)
        response, _usage, _trace = asyncio.run(run_llm_loop(
            messages=[{"role": "user", "content": "hi"}],
            tools=self._ToolsThatRun(),
            llm=llm,
            agent_root=tmp_path,
            emit_progress=_Recorder(),
        ))
        return response, llm

    def test_a_run_past_the_ceiling_is_stopped_by_the_loop_itself(self, tmp_path, monkeypatch):
        # 200 000 against CompactionState's 204 800 fallback window is 97.7 %.
        _response, llm = self._drive(200_000, tmp_path, monkeypatch)

        assert llm.seen_final_messages is not None, "the guard never stopped the loop"
        injected = "\n".join(
            m.get("content") or "" for m in llm.seen_final_messages
            if m.get("role") == "system"
        )
        assert "[CONTEXT_LIMIT]" in injected

    def test_a_run_with_room_is_not_stopped(self, tmp_path, monkeypatch):
        """The same wiring, below the ceiling: the loop must run on. Without this
        the test above would pass on a guard that stops everything."""
        _response, llm = self._drive(1_000, tmp_path, monkeypatch)

        injected = "\n".join(
            m.get("content") or "" for m in (llm.seen_final_messages or [])
            if m.get("role") == "system"
        )
        assert "[CONTEXT_LIMIT]" not in injected
