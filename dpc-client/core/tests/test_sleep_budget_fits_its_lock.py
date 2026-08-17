"""One step of sleep may not spend the whole allowance sleep is given.

`run_sleep` writes a lock and a later trigger calls that lock stuck once it is
older than the window. The synthesis is one step inside it. Set the two equal —
which the first version of the synthesis constant did, at 1800 seconds against a
thirty-minute window — and a synthesis that spends its full budget crosses the
stuck threshold at the same instant, so the next trigger resets a run that is
still working. The relation is asserted rather than remembered because both
numbers are edited for their own reasons.
"""

from __future__ import annotations

from dpc_client_core.dpc_agent.sleep_pipeline import (
    SLEEP_TIMEOUT_MINUTES,
    SYNTHESIS_TIMEOUT_SECONDS,
)


def test_the_synthesis_budget_fits_inside_the_stuck_sleep_window():
    assert SYNTHESIS_TIMEOUT_SECONDS < SLEEP_TIMEOUT_MINUTES * 60


def test_the_synthesis_budget_leaves_room_for_the_rest_of_the_pipeline():
    """Not merely under the window: the synthesis is one step of several, and a
    budget that fits only by a second buys nothing for the steps around it."""
    assert SYNTHESIS_TIMEOUT_SECONDS <= SLEEP_TIMEOUT_MINUTES * 60 / 2


def test_the_budget_is_still_far_above_the_provider_default_it_exists_to_beat():
    """The failure this replaced was a 300s provider default. A budget that drifts
    back down near it would restore the defect while looking configured."""
    assert SYNTHESIS_TIMEOUT_SECONDS >= 600.0
