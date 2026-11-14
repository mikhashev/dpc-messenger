"""Spec tests for the KV snapshot arithmetic: save bound, size threshold and the
status-code classes. Written from ADR-044 (amendment 2026-11-13, points 4, 5, 8)
and the slice-1 plan (section 2, P1) without sight of the implementation.
"""

import pytest

from dpc_client_core.managers import kv_snapshots as kv


def _kind(x):
    """An outcome kind as a plain lowercase string (str or Enum alike)."""
    return str(getattr(x, "value", x)).lower()


# ------------------------------------------------------------------ constants

def test_the_defaults_are_a_day_and_32_gib():
    assert kv.DEFAULT_TTL_S == 86_400
    assert kv.DEFAULT_MAX_GIB == 32


def test_the_write_ceiling_is_60_seconds():
    assert kv.KV_SNAPSHOT_WRITE_CEILING_S == 60.0


def test_the_index_file_name_is_the_decided_one():
    assert kv.INDEX_NAME == "dpc-kv-snapshots.json"


# ------------------------------------------------------------------ save bound

def test_60_s_of_stop_bound_left_and_50_s_expected_stop_leaves_10_s():
    now = 5_000.0
    assert kv.save_bound(now=now, stop_bound=now + 60.0, expected_stop_s=50.0) == pytest.approx(10.0)


def test_no_stop_bound_gives_the_60_s_ceiling():
    assert kv.save_bound(now=5_000.0, stop_bound=None, expected_stop_s=50.0) == pytest.approx(60.0)


def test_a_generous_stop_bound_is_still_capped_at_the_ceiling():
    now = 5_000.0
    assert kv.save_bound(now=now, stop_bound=now + 3_600.0, expected_stop_s=20.0) == pytest.approx(60.0)


def test_the_bound_is_never_negative_when_the_stop_eats_all_that_is_left():
    now = 5_000.0
    assert kv.save_bound(now=now, stop_bound=now + 10.0, expected_stop_s=50.0) == 0.0


def test_the_bound_is_never_negative_when_the_stop_bound_has_passed():
    now = 5_000.0
    assert kv.save_bound(now=now, stop_bound=now - 30.0, expected_stop_s=20.0) == 0.0


def test_an_explicit_ceiling_is_respected():
    now = 5_000.0
    assert kv.save_bound(now=now, stop_bound=now + 1_000.0, expected_stop_s=0.0,
                         ceiling=15.0) == pytest.approx(15.0)


# ------------------------------------------------------------------- threshold
# Every figure below is from the ADR-044 amendment of 2026-11-13.

STRATA_FLOOR_BYTES = 236e6                     # "What was measured": Strata file floor
STRATA_BYTES_PER_TOKEN = 15.3e6 / 1000         # same: ~15.3 MB per 1K tokens
STRATA_COLD_TOK_S = 3755.0                     # point 4: Strata alias cold median

HDD_WRITE_B_S = 0.107e9                        # point 4 / hint: HDD write, GB/s
HDD_READ_B_S = 0.094e9                         # same: HDD read, GB/s
SSD_WRITE_B_S = 4.05e9 / 2.1                   # "What was measured": 4.05 GB saved in 2.1 s
SSD_READ_B_S = 4.05e9 / 3.2                    # same: restored in 3.2 s


def _rates(write, read):
    # The plan does not spell out the shape of `folder_rates`; this is the one
    # place that names its keys.
    return {"write_bytes_per_s": write, "read_bytes_per_s": read}


def _sizes(floor=STRATA_FLOOR_BYTES, per_token=STRATA_BYTES_PER_TOKEN):
    # Likewise for `alias_sizes`.
    return {"floor_bytes": floor, "bytes_per_token": per_token}


@pytest.mark.parametrize("tokens", [10_000, 50_000, 250_000])
def test_strata_from_an_hdd_never_pays_at_a_stop(tokens):
    ok, reason = kv.worth_saving(tokens, _rates(HDD_WRITE_B_S, HDD_READ_B_S),
                                 _sizes(), STRATA_COLD_TOK_S)
    assert ok is False
    assert reason  # the skip carries its reason for the log line


@pytest.mark.parametrize("tokens", [5_000, 10_000, 50_000, 250_000])
def test_strata_from_an_ssd_pays_from_5k_tokens_up(tokens):
    ok, _reason = kv.worth_saving(tokens, _rates(SSD_WRITE_B_S, SSD_READ_B_S),
                                  _sizes(), STRATA_COLD_TOK_S)
    assert ok is True


def test_the_derived_ssd_figures_match_the_spec_5_3_s_against_67_s():
    # A wrong transcription of the spec numbers fails here, not as a puzzling
    # threshold result above.
    size = STRATA_FLOOR_BYTES + 250 * STRATA_BYTES_PER_TOKEN * 1000
    assert size / 1e9 == pytest.approx(4.06, abs=0.05)
    assert size / SSD_WRITE_B_S + size / SSD_READ_B_S == pytest.approx(5.3, abs=0.3)
    assert 250_000 / STRATA_COLD_TOK_S == pytest.approx(66.6, abs=0.5)


def test_the_derived_hdd_figures_match_the_measured_38_s_and_43_s():
    size = STRATA_FLOOR_BYTES + 250 * STRATA_BYTES_PER_TOKEN * 1000
    assert size / HDD_WRITE_B_S == pytest.approx(37.8, abs=1.5)
    assert size / HDD_READ_B_S == pytest.approx(43.3, abs=1.5)


def test_estimate_bytes_is_floor_plus_per_token():
    assert kv.estimate_bytes(1000, 236e6, 15_300) == pytest.approx(236e6 + 15.3e6)
    assert kv.estimate_bytes(0, 236e6, 15_300) == pytest.approx(236e6)


# ----------------------------------------------------------- status classes

@pytest.mark.parametrize("code", [415, 403, 501, 400])
def test_a_transport_or_configuration_status_never_classifies_as_the_engine_refusing(code):
    assert _kind(kv.classify_save(code, "")) != "engine_refuses"
    assert _kind(kv.classify_save(code, "some body text")) != "engine_refuses"


def test_a_500_is_the_engine_refusing_and_suspends_saving():
    assert _kind(kv.classify_save(500, "invalid K/V extent")) == "engine_refuses"


def test_the_four_transport_statuses_are_neither_suspending_nor_saved():
    for code in (415, 403, 501, 400):
        assert _kind(kv.classify_save(code, "")) not in ("engine_refuses", "saved")


def test_classify_save_takes_no_body_and_defaults_to_strata():
    assert _kind(kv.classify_save(500)) == "engine_refuses"
    assert _kind(kv.classify_save(500, engine="strata")) == "engine_refuses"


def test_a_500_from_llama_server_is_not_the_suspending_outcome():
    assert _kind(kv.classify_save(500, "slot save failed", engine="llamacpp_server")) != "engine_refuses"
    assert _kind(kv.classify_save(500, engine="llamacpp_server")) != "engine_refuses"


@pytest.mark.parametrize("engine", ["strata", "llamacpp_server"])
def test_no_answer_is_not_engine_death(engine):
    assert _kind(kv.classify_save(None, engine=engine)) != "engine_died"
    assert _kind(kv.classify_save(None, "", engine=engine)) != "engine_died"


@pytest.mark.parametrize("code", [415, 403, 501, 400])
def test_the_transport_statuses_stay_off_the_suspending_outcome_on_llama_server_too(code):
    assert _kind(kv.classify_save(code, "", engine="llamacpp_server")) != "engine_refuses"


def test_a_200_is_a_save():
    assert _kind(kv.classify_save(200, "")) == "saved"


def test_a_strata_restore_400_saved_with_another_model_is_refused_and_deleted():
    kind = kv.classify_restore(400, "snapshot was saved with another model")
    assert _kind(kind) == "refused"


def test_a_llama_restore_400_no_available_space_keeps_the_file():
    kind = kv.classify_restore(400, "No available space in KV cache")
    assert _kind(kind) == "no_room"
    assert _kind(kind) != "refused"


@pytest.mark.parametrize("code", [415, 403, 501])
def test_a_restore_transport_status_keeps_the_file(code):
    kind = _kind(kv.classify_restore(code, ""))
    assert kind not in ("refused", "restored")


def test_a_restore_200_is_restored():
    assert _kind(kv.classify_restore(200, "")) == "restored"
