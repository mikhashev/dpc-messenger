"""The sleep synthesis reserved room for its answer and never asked for it.

2026-08-20, the run that produced no morning brief: prompt 178 731 tokens of a
262 144 window, completion 8 192 of 8 192, `finish=length`, of which 4 921 were
the think block and 3 271 the answer — a real, well-formed brief cut mid-array
in `key_decisions`. Two numbers were in play and neither was the task's:
`SYNTHESIS_OUTPUT_RESERVE_TOKENS = 4000` was subtracted from the input budget
and never sent, while the request went out at the provider's default ceiling of
8 192. About 83 K of the window sat unused.

So the reserve and the ceiling become one number, and a thinking cap keeps the
think block from spending the room before the brief starts. The ceiling is the
deterministic half — it gives the brief space whatever the model does; the cap
is the other half, because the run above spent 60 % of its ceiling thinking.
"""

import pytest

from dpc_client_core.dpc_agent.sleep_pipeline import (
    SYNTHESIS_BUDGET_FACTOR,
    SYNTHESIS_OUTPUT_RESERVE_MAX,
    SYNTHESIS_OUTPUT_RESERVE_MIN,
    _compute_synthesis_budget,
    _synthesis_output_reserve,
    _synthesis_request_limits,
)

WINDOW = 262144  # the window the failure was measured on


class TestTheReserveAndTheCeilingAreOneNumber:

    def test_the_call_asks_for_exactly_what_the_budget_reserved(self):
        """Two numbers is the defect; one number is the fix."""
        assert _synthesis_request_limits(WINDOW)["max_tokens"] == _synthesis_output_reserve(WINDOW)

    def test_the_input_budget_subtracts_that_same_number(self):
        overhead = 3310
        assert (
            _compute_synthesis_budget(WINDOW, overhead)
            == int(WINDOW * SYNTHESIS_BUDGET_FACTOR) - _synthesis_output_reserve(WINDOW) - overhead
        )

    def test_the_room_reserved_is_more_than_the_brief_that_was_cut(self):
        """The observed answer had 3 271 tokens and was still mid-array; a
        ceiling at or below that would repeat the failure with a new number."""
        assert _synthesis_output_reserve(WINDOW) > 8192


class TestTheThinkBlockCannotSpendTheAnswersRoom:

    def test_a_thinking_cap_travels_with_the_call(self):
        limits = _synthesis_request_limits(WINDOW)
        assert 0 < limits["reasoning_budget_tokens"] < limits["max_tokens"]

    def test_the_cap_leaves_the_brief_more_than_it_had(self):
        """Thinking capped, the answer keeps the rest — and the rest must beat
        the 3 271 tokens that were not enough."""
        limits = _synthesis_request_limits(WINDOW)
        assert limits["max_tokens"] - limits["reasoning_budget_tokens"] > 3271

    def test_the_cap_is_not_the_whole_ceiling(self):
        for window in (16000, 131072, WINDOW, 1000000):
            limits = _synthesis_request_limits(window)
            assert limits["reasoning_budget_tokens"] < limits["max_tokens"], window


class TestBothHalvesStillFitTheWindowTheyRunIn:

    @pytest.mark.parametrize("window", [16000, 32768, 131072, 262144, 1000000])
    def test_input_budget_plus_output_ceiling_stays_inside_the_window(self, window):
        """A ceiling is only honest if the prompt it sits beside can still fit."""
        assert _compute_synthesis_budget(window, 3310) + _synthesis_output_reserve(window) < window

    @pytest.mark.parametrize("window", [16000, 32768, 131072, 262144, 1000000])
    def test_a_small_window_still_gets_a_budget_to_work_with(self, window):
        """The reserve is a fraction for this reason: a flat 16 384 would leave
        the fleet's smallest alias with a negative input budget, which is not a
        reserve but a refusal."""
        assert _compute_synthesis_budget(window, 3310) > 0

    def test_the_reserve_stays_between_its_floor_and_its_cap(self):
        assert _synthesis_output_reserve(1000) == SYNTHESIS_OUTPUT_RESERVE_MIN
        assert _synthesis_output_reserve(10_000_000) == SYNTHESIS_OUTPUT_RESERVE_MAX
        assert _synthesis_output_reserve(WINDOW) == SYNTHESIS_OUTPUT_RESERVE_MAX
