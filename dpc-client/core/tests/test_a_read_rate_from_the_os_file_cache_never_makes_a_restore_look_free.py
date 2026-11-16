"""A read rate measured from the OS file cache never makes a KV snapshot restore look free.

On 2026-11-16 at 21:08 a file written a minute earlier was read back from the Windows file
cache and 2.3 GB/s landed in the folder's index, on a disk that does about 90-130 MB/s.
Mike's call, 2026-11-16: the read rate the threshold uses is at most twice the folder's
write rate. It is capped where it is used, so an index already holding such a sample is
read right without editing it, and when a sample is recorded, with one INFO line saying so.
With no write rate the read rate stands as measured.
"""

import logging

import pytest

from dpc_client_core.managers import kv_snapshots as kv

WRITE = 105e6
CACHED = 2.3e9
STRATA = kv.SEED_SIZES["strata"]
COLD = 3_750.0


@pytest.fixture(autouse=True)
def _clean_state():
    kv.reset_state()
    yield
    kv.reset_state()


def _folder(tmp_path, rates):
    kv.write_index(tmp_path, {**kv.read_index(tmp_path), "rates": dict(rates)})
    return tmp_path


def test_the_cap_is_twice_the_write_rate():
    assert kv.READ_RATE_CAP_OF_WRITE == 2.0
    assert kv.capped_read_rate({"write_bytes_per_s": WRITE, "read_bytes_per_s": CACHED}) == pytest.approx(2 * WRITE)
    assert kv.capped_read_rate({"write_bytes_per_s": WRITE, "read_bytes_per_s": 90e6}) == pytest.approx(90e6)
    assert kv.capped_read_rate({"read_bytes_per_s": CACHED}) == pytest.approx(CACHED)
    assert kv.capped_read_rate({"write_bytes_per_s": WRITE}) is None
    assert kv.capped_read_rate({}) is None


def test_an_index_already_holding_a_cached_sample_is_read_at_the_cap(tmp_path):
    folder = _folder(tmp_path, {"write_bytes_per_s": WRITE, "read_bytes_per_s": CACHED})
    tokens = 50_000
    ok, words = kv.worth_saving(tokens, kv.folder_rates(folder), STRATA, COLD)
    nbytes = kv.estimate_bytes(tokens, STRATA["floor_bytes"], STRATA["bytes_per_token"])
    assert ok and words.startswith(f"restore ~{nbytes / (2 * WRITE):.1f} s ")
    # The index itself is left as it was.
    assert kv.read_index(folder)["rates"]["read_bytes_per_s"] == pytest.approx(CACHED)


def test_a_capped_read_never_calls_a_short_conversation_worth_a_restore(tmp_path):
    # At 2.3 GB/s 3K tokens would restore in ~0.12 s against a 0.8 s cold read; at the cap ~1.3 s.
    folder = _folder(tmp_path, {"write_bytes_per_s": WRITE, "read_bytes_per_s": CACHED})
    ok, words = kv.worth_saving(3_000, kv.folder_rates(folder), STRATA, COLD)
    assert not ok and words.startswith("below the size threshold: restore ~1.3 s")


def test_a_cached_sample_is_recorded_at_the_cap_with_one_line(tmp_path, caplog):
    folder = _folder(tmp_path, {"write_bytes_per_s": WRITE})
    with caplog.at_level(logging.INFO, logger=kv.logger.name):
        kv.note_restore_rate(folder, int(CACHED), 1_000)
    assert kv.folder_rates(folder)["read_bytes_per_s"] == pytest.approx(2 * WRITE)
    lines = [r.getMessage() for r in caplog.records if "read rate" in r.getMessage()]
    assert lines == [f"KV snapshots read rate of {folder} capped: measured 2300.0 MB/s, recorded 210.0 MB/s "
                     "(2 x the write rate 105.0 MB/s; a read from the OS file cache measures memory)"]
    assert all(r.levelno == logging.INFO for r in caplog.records if "read rate" in r.getMessage())


def test_a_sample_under_the_cap_is_recorded_as_measured_and_says_nothing(tmp_path, caplog):
    folder = _folder(tmp_path, {"write_bytes_per_s": WRITE})
    with caplog.at_level(logging.INFO, logger=kv.logger.name):
        kv.note_restore_rate(folder, 90_000_000, 1_000)
    assert kv.folder_rates(folder)["read_bytes_per_s"] == pytest.approx(90e6)
    assert not any("capped" in r.getMessage() for r in caplog.records)


def test_with_no_write_rate_a_sample_stands_as_measured(tmp_path, caplog):
    folder = _folder(tmp_path, {})
    with caplog.at_level(logging.INFO, logger=kv.logger.name):
        kv.note_restore_rate(folder, int(CACHED), 1_000)
    assert kv.folder_rates(folder)["read_bytes_per_s"] == pytest.approx(CACHED)
    assert not any("capped" in r.getMessage() for r in caplog.records)
