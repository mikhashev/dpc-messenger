"""The save bound is what the waiting request has left, less the stop (ADR-044, 2026-11-13, point 4).

The smaller of the per-file write ceiling (60 s) and what is left of the waiting
request's stop bound when the save starts, less the stop still to come; never
below zero. With no waiting request (Stop engine, a retire) the ceiling alone.
An estimate over the bound is skipped without trying.
"""

import pytest

from dpc_client_core.managers import kv_snapshots as kv


def test_the_ceiling_is_sixty_seconds():
    assert kv.KV_SNAPSHOT_WRITE_CEILING_S == 60.0


def test_sixty_seconds_left_and_a_fifty_second_stop_leave_ten():
    assert kv.save_bound(1_000.0, 1_060.0, 50.0) == pytest.approx(10.0)


def test_no_waiting_request_gives_the_ceiling():
    assert kv.save_bound(1_000.0, None, 50.0) == kv.KV_SNAPSHOT_WRITE_CEILING_S
    assert kv.save_bound(1_000.0, None, 50.0, ceiling=5.0) == 5.0


def test_the_ceiling_caps_a_generous_stop_bound():
    assert kv.save_bound(0.0, 600.0, 20.0) == kv.KV_SNAPSHOT_WRITE_CEILING_S


def test_the_bound_is_read_when_the_save_starts_not_at_the_mark():
    # The same request: later start, less left.
    assert kv.save_bound(0.0, 70.0, 20.0) == pytest.approx(50.0)
    assert kv.save_bound(30.0, 70.0, 20.0) == pytest.approx(20.0)


def test_nothing_left_is_zero_never_negative():
    assert kv.save_bound(1_000.0, 1_030.0, 50.0) == 0.0
    assert kv.save_bound(1_100.0, 1_060.0, 0.0) == 0.0


def test_a_peers_swap_on_strata_leaves_about_ten_seconds_at_best():
    # 60 s replaces a peer's stop bound; Strata's stop is 20 s plus up to 30 s of VRAM release.
    assert kv.save_bound(0.0, 60.0, 20.0 + 30.0) == pytest.approx(10.0)


def test_the_estimated_write_is_compared_with_the_bound():
    rates = {"write_bytes_per_s": 100e6}
    assert kv.estimate_save_s(2_000_000_000, rates) == pytest.approx(20.0)
    assert kv.estimate_save_s(2_000_000_000, rates) > kv.save_bound(0.0, 60.0, 50.0)
    assert kv.estimate_save_s(2_000_000_000, {}) is None
