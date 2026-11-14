"""A KV snapshot is saved only when save and restore beat the cold read (ADR-044, 2026-11-13, points 4 and 8).

The rates are the spec's 2026-11-13 measurements of a 4.05 GB Strata file
(SSD: saved in 2.1 s, restored in 3.2 s; HDD: 37.8 s and 43.3 s) and the ledger's
cold median of 3,755 tok/s on the Strata alias. By CC's arithmetic in the spec,
Strata from SSD breaks even at about 1.2K tokens and from HDD never at a stop.
A save's HTTP answer decides whether saving goes on: only a 500 suspends it.
"""

import shutil
from collections import namedtuple

import pytest

from dpc_client_core.managers import kv_snapshots as kv

FILE = 4.05e9
SSD = {"write_bytes_per_s": FILE / 2.1, "read_bytes_per_s": FILE / 3.2}
HDD = {"write_bytes_per_s": FILE / 37.8, "read_bytes_per_s": FILE / 43.3}
STRATA = kv.SEED_SIZES["strata"]
COLD = 3_755.0


@pytest.fixture(autouse=True)
def _clean_state():
    kv.reset_state()
    yield
    kv.reset_state()


def test_strata_from_hdd_never_passes_at_a_stop():
    for tokens in (1_000, 10_000, 100_000, 250_000, 1_000_000):
        ok, reason = kv.worth_saving(tokens, HDD, STRATA, COLD)
        assert not ok, tokens
        assert reason.startswith("below the size threshold")


def test_strata_from_ssd_passes_from_about_1_2k_tokens():
    assert not kv.worth_saving(1_000, SSD, STRATA, COLD)[0]
    assert kv.worth_saving(1_400, SSD, STRATA, COLD)[0]
    assert kv.worth_saving(250_000, SSD, STRATA, COLD)[0]


def test_an_idle_save_counts_only_the_restore():
    # Strata HDD, restore only: break-even about 24K tokens (spec, point 4).
    assert not kv.worth_saving(15_000, HDD, STRATA, COLD, at_stop=False)[0]
    assert kv.worth_saving(40_000, HDD, STRATA, COLD, at_stop=False)[0]


def test_unknown_inputs_say_so():
    assert kv.worth_saving(None, SSD, STRATA, COLD) == (False, "size unknown")
    assert kv.worth_saving(0, SSD, STRATA, COLD) == (False, "size unknown")
    assert kv.worth_saving(50_000, {}, STRATA, COLD) == (True, "write rate unknown, tried once")
    assert kv.worth_saving(50_000, SSD, STRATA, None) == (True, "threshold unknown")
    # No read rate yet: the write rate stands in for it.
    write_only = {"write_bytes_per_s": SSD["write_bytes_per_s"]}
    assert kv.worth_saving(50_000, write_only, STRATA, COLD)[0]


def test_estimate_bytes_is_floor_plus_part_per_token():
    assert kv.estimate_bytes(250_000, STRATA["floor_bytes"], STRATA["bytes_per_token"]) == pytest.approx(4.06e9, rel=0.01)
    assert kv.estimate_bytes(None, 10, 5) == 10


def _row(prompt, ms, *, alias="flash", cached=0, source="engine"):
    return {"alias": alias, "prompt_tokens": prompt, "prompt_ms": ms, "cached_tokens": cached,
            "counts_source": source}


def test_the_cold_rate_comes_from_cold_engine_rows_of_the_nearest_length():
    rows = [
        _row(10_000, 2_500), _row(12_000, 3_000), _row(14_000, 3_500),           # 4,000 tok/s near 10K
        _row(200_000, 400_000),                                                    # 500 tok/s at depth
        _row(10_000, 100, cached=9_000), _row(10_000, 100, cached=None),          # not cold / not known cold
        _row(10_000, 100, source="ours"), _row(4_000, 1), _row(10_000, 100, alias="other"),
        {"alias": "flash", "prompt_tokens": "x"},
    ]
    assert kv.cold_rate_at(rows, "flash", 11_000) == pytest.approx(4_000)
    assert kv.cold_rate_at(rows, "flash", 230_000) == pytest.approx(500)
    assert kv.cold_rate_at(rows, "flash", 40_000) == pytest.approx(4_000)
    assert kv.cold_rate_at(rows, "nobody", 10_000) is None
    assert kv.cold_rate_at([], "flash", 10_000) is None


def test_the_cold_rate_says_how_many_rows_it_rests_on():
    rows = [_row(10_000, 2_500), _row(12_000, 3_000), _row(14_000, 3_500), _row(200_000, 400_000),
            _row(10_000, 100, cached=9_000)]
    assert kv.cold_rate_with_rows(rows, "flash", 11_000) == (pytest.approx(4_000), 3)
    assert kv.cold_rate_with_rows(rows, "flash", 230_000) == (pytest.approx(500), 1)
    assert kv.cold_rate_with_rows(rows, "nobody", 10_000) == (None, 0)
    assert kv.cold_rate_with_rows([], "flash", 10_000) == (None, 0)
    for n in (11_000, 230_000):
        assert kv.cold_rate_at(rows, "flash", n) == kv.cold_rate_with_rows(rows, "flash", n)[0]


def test_the_cold_rate_detail_names_its_bucket_and_says_when_it_is_extrapolated():
    rows = [_row(10_000, 2_500), _row(200_000, 400_000)]
    own = kv.cold_rate_detail(rows, "flash", 11_000)
    assert own == {"rate": pytest.approx(4_000), "rows": 1, "bucket": "5K-16K", "extrapolated": False}
    far = kv.cold_rate_detail(rows, "flash", 300_000)
    assert far["bucket"] == "128K-256K" and far["extrapolated"] is True
    assert kv.cold_rate_detail(rows, "nobody", 10_000) == {
        "rate": None, "rows": 0, "bucket": None, "extrapolated": False}


def test_a_disk_short_of_the_estimate_plus_the_floor_is_short(tmp_path, monkeypatch):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(10 ** 12, 0, 6 * 1024 ** 3))
    assert not kv.disk_short(tmp_path, 1 * 1024 ** 3)
    assert kv.disk_short(tmp_path, 3 * 1024 ** 3)

    def gone(p):
        raise FileNotFoundError(p)

    monkeypatch.setattr(shutil, "disk_usage", gone)
    assert kv.disk_short(tmp_path / "unplugged", 1)


# ---- a save's answer


@pytest.mark.parametrize("code, body, kind", [
    (200, '{"n_saved": 30000, "n_written": 470000000}', kv.SAVED),
    (500, '{"error": {"code": 500, "message": "invalid K/V extent", "kind": "io"}}', kv.ENGINE_REFUSES),
    (500, '{"error": {"code": 500, "message": "engine ended; the next request starts the engine again"}}',
     kv.ENGINE_DIED),
    (None, "EngineSilent: no line for 300 s", kv.FAILED),
    (None, "ConnectError: connection dropped", kv.FAILED),
    (500, '{"error": {"message": "EngineDied"}}', kv.ENGINE_REFUSES),
    (None, "ReadTimeout: timed out", kv.FAILED),
    (415, "", kv.TRANSPORT),
    (403, "", kv.TRANSPORT),
    (501, '{"error": {"message": "slot save/restore is disabled"}}', kv.TRANSPORT),
    (400, '{"error": {"message": "this server has one slot: 0"}}', kv.TRANSPORT),
    (503, '{"error": {"code": 503, "message": "the model is not loaded"}}', kv.NOT_LOADED),
    (503, '{"error": {"code": 503, "message": "no memory", "kind": "memory"}}', kv.FAILED),
    (507, '{"error": {"code": 507, "message": "disk full", "kind": "storage"}}', kv.FAILED),
])
def test_a_save_answer_is_classified_by_status_and_body(code, body, kind):
    assert kv.classify_save(code, body) == kind


def test_a_llama_server_500_is_a_failure_not_a_refusal_of_saves():
    body = {"error": {"code": 500, "message": "Unable to save slot", "type": "server_error"}}
    assert kv.classify_save(500, body, engine="llamacpp_server") == kv.FAILED
    assert kv.classify_save(500, body) == kv.ENGINE_REFUSES                      # Strata is the default
    assert kv.classify_save(500, body, engine="strata") == kv.ENGINE_REFUSES
    assert kv.classify_save(415, "", engine="llamacpp_server") == kv.TRANSPORT


def test_no_answer_is_a_failure_with_no_answer_in_its_words():
    for text in (None, "", "Cannot connect to host 127.0.0.1:8080", "ReadTimeout"):
        assert kv.classify_save(None, text) == kv.FAILED
        assert kv.classify_restore(None, text) == kv.FAILED
    assert kv.describe_outcome(kv.FAILED) == "failed (no answer)"


def test_outcomes_read_as_words():
    assert kv.describe_outcome(kv.ENGINE_REFUSES, 500) == "the engine refused the save (500)"
    invalid = {"error": {"code": 400, "message": "m", "kind": "invalid"}}
    assert kv.classify_save(400, invalid) == kv.TRANSPORT
    assert kv.describe_outcome(kv.TRANSPORT, 400, invalid) == "the engine refused the request's content (400)"
    assert "slot or file" in kv.describe_outcome(kv.TRANSPORT, 400, {"error": {"message": "one slot: 0"}})
    for kind in (kv.SAVED, kv.RESTORED, kv.ENGINE_DIED, kv.NOT_LOADED, kv.REFUSED, kv.NO_ROOM, kv.MISSING):
        assert "_" not in kv.describe_outcome(kind, 500)


def test_the_engines_own_disk_refusal_reads_apart_from_dpcs_disk_check():
    body = {"error": {"code": 507, "message": "not enough free space", "kind": "storage"}}
    assert kv.classify_save(507, body) == kv.FAILED
    words = kv.describe_outcome(kv.FAILED, 507, body)
    assert words == "the engine refused: not enough disk for the file plus its reserve (507)"
    assert words != kv.describe_outcome(kv.FAILED, 500)


def test_a_skipped_save_reads_as_not_saved_with_the_callers_reason():
    assert kv.SKIPPED == "skipped"
    assert kv.describe_outcome(kv.SKIPPED, reason="below the size threshold") == \
        "not saved: below the size threshold"
    assert kv.describe_outcome(kv.SKIPPED) == "not saved"


def test_the_free_space_floor_is_stratas_own_session_reserve():
    assert kv.FREE_SPACE_FLOOR_BYTES == 4096 * 1024 ** 2


def test_no_transport_status_ever_reads_as_the_engine_refusing():
    for code in (415, 403, 501, 400):
        assert kv.classify_save(code, "anything") != kv.ENGINE_REFUSES
        assert "refuses" not in kv.describe_outcome(kv.classify_save(code, ""), code)


def test_a_suspension_lasts_the_launch_and_warnings_come_once_per_launch():
    assert kv.off_reason("flash") is None
    kv.suspend("flash", kv.ENGINE_REFUSES_SAVES)
    assert kv.off_reason("flash") == "the engine refuses saves"
    assert kv.off_reason("other") is None
    assert kv.first_this_launch("flash", "415") is True
    assert kv.first_this_launch("flash", "415") is False
    assert kv.first_this_launch("flash", "403") is True
    kv.note_save("flash", {"saved": False, "reason": "x"})
    kv.launch_began("flash")
    assert kv.off_reason("flash") is None
    assert kv.first_this_launch("flash", "415") is True
    assert kv.status_of("flash")["last_save"] == {"saved": False, "reason": "x"}
