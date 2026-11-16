"""A KV snapshot's size estimate fits its floor and its part per token from several saves.

One save fits one unknown, so the refit kept the held floor (the llama-server seed 0.47 GB) and
followed the file with the slope alone: 17.96 KB/token after the three 27B files of 2026-11-17,
about 9 % under a 200K file. The alias now keeps its last `SIZE_SAMPLES_PER_ALIAS` saves in the
index (`size_samples`: tokens, file bytes, the checkpoint bytes the engine named or null) and fits
both by least squares once two of one kind lie `SIZE_FIT_MIN_SPREAD_TOKENS` apart; a fit with a
negative floor or a slope not above zero, too few samples, or too little spread: the one-file rule.
Samples with the engine's checkpoint bytes and whole-file samples are never fitted together.
"""

import time

import pytest

from dpc_client_core.managers import kv_snapshots as kv

MIB = 1024 ** 2
# The three real 27B files, in the order they were saved.
REAL = [
    (78_757, 2_249_078_208, 2, round(299.291 * MIB)),
    (93_828, 2_589_381_388, 2, round(299.291 * MIB)),
    (67_797, 4_041_495_004, 15, round(2244.686 * MIB)),
]
LLAMA_FLOOR = kv.SEED_SIZES["llamacpp_server"]["floor_bytes"]
FIELD = "size_samples"


def _save(folder, tokens, nbytes, ck_count=None, ck_bytes=None, *, engine="llamacpp_server", n=[0]):
    n[0] += 1
    _, final = kv.new_snapshot_names(folder)
    (folder / final).write_bytes(b"x" * 8)
    record = {"agent_id": "a1", "conversation_id": f"c{n[0]}", "caller_kind": "agent",
              "prefix_sha256": "ab", "prefix_messages": 2}
    entry = kv.new_entry(file=final, alias="qwen", engine=engine, identity={"engine_version": "1"},
                         record=record, n_saved=tokens, nbytes=nbytes, save_ms=30_000, now=time.time())
    assert kv.add_entry(folder, entry, cap_bytes=10 ** 13, checkpoint_count=ck_count, checkpoint_bytes=ck_bytes)
    return kv.read_index(folder)["sizes"]["qwen"]


def _estimate(folder, tokens, engine="llamacpp_server"):
    s = kv.alias_sizes(folder, "qwen", engine)
    return kv.estimate_bytes(tokens, s["floor_bytes"], s["bytes_per_token"], s.get("checkpoint_bytes", 0.0))


def _fit(held):
    return {k: v for k, v in held.items() if k != FIELD}


# ---- the real files

def test_the_three_real_27b_files_fit_the_measured_floor_and_slope(tmp_path):
    for tokens, nbytes, count, ck in REAL:
        held = _save(tmp_path, tokens, nbytes, count, ck)
    ck = REAL[-1][3]
    want = 0.157e9 + 22_580 * 200_000 + ck
    assert _estimate(tmp_path, 200_000) == pytest.approx(want, rel=0.02)
    assert held["checkpoint_bytes"] == ck and held["checkpoint_count"] == 15
    assert held["floor_bytes"] == pytest.approx(0.157e9, rel=0.01)
    assert held["bytes_per_token"] == pytest.approx(22_580, rel=0.005)
    assert held[FIELD] == [[t, b, c] for t, b, _, c in REAL]


def test_one_file_keeps_the_one_file_rule_and_the_second_fits_both(tmp_path):
    (t1, b1, n1, c1), (t2, b2, n2, c2) = REAL[:2]
    held = _save(tmp_path, t1, b1, n1, c1)
    assert _fit(held) == {"floor_bytes": LLAMA_FLOOR, "bytes_per_token": (b1 - c1 - LLAMA_FLOOR) / t1,
                          "checkpoint_bytes": float(c1), "checkpoint_count": n1}
    held = _save(tmp_path, t2, b2, n2, c2)
    slope = ((b2 - c2) - (b1 - c1)) / (t2 - t1)
    assert held["bytes_per_token"] == pytest.approx(slope)
    assert held["floor_bytes"] == pytest.approx((b1 - c1) - slope * t1)


# ---- the fallback

def test_two_saves_closer_than_the_minimum_spread_keep_the_one_file_rule(tmp_path):
    t1, b1, n1, c1 = REAL[0]
    _save(tmp_path, t1, b1, n1, c1)
    t2 = t1 + kv.SIZE_FIT_MIN_SPREAD_TOKENS - 1
    b2 = b1 + 22_580 * (t2 - t1)
    held = _save(tmp_path, t2, b2, n1, c1)
    assert _fit(held) == {"floor_bytes": LLAMA_FLOOR, "bytes_per_token": (b2 - c1 - LLAMA_FLOOR) / t2,
                          "checkpoint_bytes": float(c1), "checkpoint_count": n1}


def test_a_fit_with_a_negative_floor_keeps_the_one_file_rule(tmp_path):
    _save(tmp_path, 10_000, 300_000_000)
    held = _save(tmp_path, 30_000, 1_500_000_000)           # 60 KB/token, floor -300 MB
    assert _fit(held) == {"floor_bytes": LLAMA_FLOOR, "bytes_per_token": (1_500_000_000 - LLAMA_FLOOR) / 30_000}


def test_a_fit_with_a_falling_slope_keeps_the_one_file_rule(tmp_path):
    _save(tmp_path, 50_000, 2_000_000_000)
    held = _save(tmp_path, 70_000, 1_900_000_000)
    assert _fit(held) == {"floor_bytes": LLAMA_FLOOR, "bytes_per_token": (1_900_000_000 - LLAMA_FLOOR) / 70_000}


def test_the_fallback_holds_a_fitted_floor(tmp_path):
    for tokens, nbytes, count, ck in REAL[:2]:
        fitted = _save(tmp_path, tokens, nbytes, count, ck)["floor_bytes"]
    # A whole-file save: no checkpoint-free sample enters, one whole-file sample is too few.
    held = _save(tmp_path, 60_000, 1_600_000_000)
    assert _fit(held) == {"floor_bytes": fitted, "bytes_per_token": (1_600_000_000 - fitted) / 60_000}


# ---- no mixing

def test_whole_file_samples_never_enter_a_fit_with_checkpoint_bytes(tmp_path):
    _save(tmp_path, 50_000, 3_000_000_000)                  # no engine line: the whole file
    (t1, b1, n1, c1), (t2, b2, n2, c2) = REAL[:2]
    _save(tmp_path, t1, b1, n1, c1)
    held = _save(tmp_path, t2, b2, n2, c2)
    slope = ((b2 - c2) - (b1 - c1)) / (t2 - t1)
    assert held["bytes_per_token"] == pytest.approx(slope)
    assert held["floor_bytes"] == pytest.approx((b1 - c1) - slope * t1)
    assert held[FIELD][0] == [50_000, 3_000_000_000, None]


def test_whole_file_samples_fit_together_when_no_engine_names_checkpoint_bytes(tmp_path):
    # Strata names the count, never the bytes: its files fit whole, as its seed was.
    _save(tmp_path, 40_000, 848_000_000, 1, None, engine="strata")
    held = _save(tmp_path, 82_754, 1_499_976_756, 1, None, engine="strata")
    slope = (1_499_976_756 - 848_000_000) / (82_754 - 40_000)
    assert held["bytes_per_token"] == pytest.approx(slope)
    assert held["floor_bytes"] == pytest.approx(848_000_000 - slope * 40_000)
    assert "checkpoint_bytes" not in held
    assert held[FIELD] == [[40_000, 848_000_000, None], [82_754, 1_499_976_756, None]]


# ---- the field

def test_an_index_without_the_field_refits_exactly_as_before(tmp_path):
    kv.write_index(tmp_path, {"entries": [], "rates": {}, "sizes": {
        "qwen": {"floor_bytes": 0.3e9, "bytes_per_token": 20e3}}})
    assert kv.alias_sizes(tmp_path, "qwen", "llamacpp_server") == {"floor_bytes": 0.3e9, "bytes_per_token": 20e3}
    t, b, n, c = REAL[2]
    held = _save(tmp_path, t, b, n, c)
    assert _fit(held) == {"floor_bytes": 0.3e9, "bytes_per_token": (b - c - 0.3e9) / t,
                          "checkpoint_bytes": float(c), "checkpoint_count": n}
    assert held[FIELD] == [[t, b, c]]
    assert kv.read_index(tmp_path)["version"] == kv.INDEX_VERSION == 1


@pytest.mark.parametrize("junk", ["x", 5, [[1, 2]], [[True, 3e9, None]], [[50_000, 3e9, 4e9]],
                                  [[50_000, -1, None]], [["50000", 3e9, None]], [[30_000, 1.2e9, "1"]]])
def test_ill_formed_samples_in_the_index_are_ignored(tmp_path, junk):
    kv.write_index(tmp_path, {"entries": [], "rates": {}, "sizes": {
        "qwen": {"floor_bytes": 0.3e9, "bytes_per_token": 20e3, FIELD: junk}}})
    held = _save(tmp_path, 60_000, 1_600_000_000)
    assert _fit(held) == {"floor_bytes": 0.3e9, "bytes_per_token": (1_600_000_000 - 0.3e9) / 60_000}
    assert held[FIELD] == [[60_000, 1_600_000_000, None]]


def test_the_alias_keeps_only_its_last_samples(tmp_path):
    for i in range(kv.SIZE_SAMPLES_PER_ALIAS + 3):
        held = _save(tmp_path, 20_000 + 10_000 * i, int(0.2e9 + 22_000 * (20_000 + 10_000 * i)))
    assert len(held[FIELD]) == kv.SIZE_SAMPLES_PER_ALIAS
    assert held[FIELD][0][0] == 20_000 + 10_000 * 3
    assert held["floor_bytes"] == pytest.approx(0.2e9, rel=1e-6)
    assert held["bytes_per_token"] == pytest.approx(22_000, rel=1e-6)


def test_a_save_too_small_for_its_floor_still_records_its_sample(tmp_path):
    held = _save(tmp_path, 1_000, 100_000_000)              # under the 0.47 GB seed floor: no fit
    assert held == {FIELD: [[1_000, 100_000_000, None]]}
    assert kv.alias_sizes(tmp_path, "qwen", "llamacpp_server") == kv.seed_sizes("llamacpp_server")
