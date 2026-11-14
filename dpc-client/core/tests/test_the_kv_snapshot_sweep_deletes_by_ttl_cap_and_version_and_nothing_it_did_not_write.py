"""The KV snapshot sweep deletes by TTL, cap and version, and nothing it did not write (ADR-044, 2026-11-13, points 5-6).

The index is the only record of what is DPC's (Ark, round 2): Strata's image job
may leave its own session files in the folder, so a file the index does not name
is never counted against the cap and never deleted. The restore's HTTP answer
decides whether a file goes (refused, missing) or stays (no room, transport).
"""

import json
import os
import time
from pathlib import Path

import pytest

from dpc_client_core.managers import kv_snapshots as kv

NOW = 2_000_000.0
REAL_NOW = time.time()
IDENTITY = {"engine_version": "0.1.57", "model": "flash-next", "drafter": "mtp"}


@pytest.fixture(autouse=True)
def _clean_state():
    kv.reset_state()
    yield
    kv.reset_state()


def _save(folder: Path, *, conversation="c1", alias="flash", nbytes=100, saved_at=NOW, ttl=3600,
          identity=None, cap=10 ** 9):
    _, final = kv.new_snapshot_names(folder)
    (folder / final).write_bytes(b"k" * 16)
    record = {"agent_id": "ag", "conversation_id": conversation, "caller_kind": "agent",
              "prefix_sha256": "ff", "prefix_messages": 3}
    entry = kv.new_entry(file=final, alias=alias, engine="strata", identity=identity or IDENTITY, record=record,
                         n_saved=100, nbytes=nbytes, save_ms=10, now=saved_at, ttl_s=ttl)
    assert kv.add_entry(folder, entry, cap_bytes=cap)
    return final


def _age(folder: Path, *names, by=3600.0):
    for name in names:
        os.utime(folder / name, (REAL_NOW - by, REAL_NOW - by))


def _names(folder: Path):
    return sorted(e["file"] for e in kv.read_index(folder)["entries"])


def test_an_expired_entry_is_gone_after_a_lookup(tmp_path):
    name = _save(tmp_path, ttl=60)
    assert kv.lookup(tmp_path, alias="flash", agent_id="ag", conversation_id="c1", now=NOW + 59)["file"] == name
    assert kv.lookup(tmp_path, alias="flash", agent_id="ag", conversation_id="c1", now=NOW + 61) is None
    assert not (tmp_path / name).exists()
    assert _names(tmp_path) == []


def test_a_lookup_by_another_key_finds_nothing_and_deletes_nothing(tmp_path):
    name = _save(tmp_path)
    for key in ({"agent_id": "ag", "conversation_id": "c2"}, {"agent_id": "other", "conversation_id": "c1"},
                {"agent_id": "ag", "conversation_id": None}):
        assert kv.lookup(tmp_path, alias="flash", now=NOW, **key) is None
    assert kv.lookup(tmp_path, alias="other-alias", agent_id="ag", conversation_id="c1", now=NOW) is None
    assert (tmp_path / name).exists() and _names(tmp_path) == [name]


def test_the_cap_removes_the_oldest_first_and_never_the_file_just_written(tmp_path):
    a = _save(tmp_path, conversation="a", nbytes=400, saved_at=NOW)
    b = _save(tmp_path, conversation="b", nbytes=400, saved_at=NOW + 1)
    c = _save(tmp_path, conversation="c", nbytes=400, saved_at=NOW + 2, cap=1_000)
    assert _names(tmp_path) == sorted([b, c])
    assert not (tmp_path / a).exists()
    # keep wins even when it is the oldest
    removed = kv.sweep(tmp_path, alias="flash", now=NOW, cap_bytes=300, keep=b)
    assert removed == [(c, kv.OVER_CAP)]
    assert _names(tmp_path) == [b]


def test_a_foreign_file_is_never_deleted_or_counted(tmp_path):
    foreign = tmp_path / "image-job-session.bin"
    foreign.write_bytes(b"strata image job" * 1000)
    lookalike = tmp_path / "dpc-kv-notes.bin"          # the prefix without hex is not DPC's name
    lookalike.write_bytes(b"not ours")
    name = _save(tmp_path, nbytes=900, cap=1_000)
    _age(tmp_path, foreign.name, lookalike.name)
    kv.sweep(tmp_path, alias="flash", now=REAL_NOW + 10 ** 9, cap_bytes=1)
    assert foreign.exists() and lookalike.exists()
    assert not (tmp_path / name).exists()
    kv.register_folder("flash", tmp_path, cap_bytes=1_000)
    assert kv.status_of("flash", now=NOW)["files"] == 0 and kv.status_of("flash", now=NOW)["bytes"] == 0


def test_an_unindexed_dpc_file_older_than_any_write_is_deleted_as_an_orphan(tmp_path):
    old = tmp_path / ("dpc-kv-" + "1" * 32 + ".bin")
    old.write_bytes(b"left by a crash between rename and index")
    fresh = tmp_path / ("dpc-kv-" + "2" * 32 + ".bin")
    fresh.write_bytes(b"renamed a moment ago")
    _, writing = kv.new_snapshot_names(tmp_path)
    (tmp_path / writing).write_bytes(b"renamed, not indexed yet")
    foreign = tmp_path / "image-job-session.bin"
    foreign.write_bytes(b"x")
    _age(tmp_path, old.name, writing, foreign.name)
    removed = kv.sweep(tmp_path, alias="flash", now=REAL_NOW)
    assert removed == [(old.name, kv.ORPHAN)]
    assert fresh.exists() and (tmp_path / writing).exists() and foreign.exists()
    # Exactly the ceiling is not older than it.
    os.utime(fresh, (REAL_NOW - kv.KV_SNAPSHOT_WRITE_CEILING_S,) * 2)
    assert kv.sweep(tmp_path, alias="flash", now=REAL_NOW) == []


def test_the_sweep_after_an_add_runs_at_the_saves_own_time(tmp_path):
    early = _save(tmp_path, conversation="early", saved_at=NOW, ttl=60)
    late = _save(tmp_path, conversation="late", saved_at=NOW + 120, ttl=60)
    assert _names(tmp_path) == [late]
    assert not (tmp_path / early).exists()


def test_a_staging_leftover_is_deleted_and_a_staging_file_being_written_is_not(tmp_path):
    leftover = tmp_path / ("dpc-kv-" + "a" * 32 + ".staging")
    leftover.write_bytes(b"truncated")
    index_tmp = tmp_path / ".dpc-kv-snapshots-abc123.tmp"
    index_tmp.write_text("{")
    writing, _ = kv.new_snapshot_names(tmp_path)
    (tmp_path / writing).write_bytes(b"in progress")
    other = tmp_path / "notes.staging"
    other.write_text("not ours")
    removed = kv.sweep(tmp_path, alias="flash", now=NOW)
    assert sorted(removed) == sorted([(leftover.name, kv.STAGING_LEFT), (index_tmp.name, kv.STAGING_LEFT)])
    assert (tmp_path / writing).exists() and other.exists()
    kv.release_staging(tmp_path, writing)
    assert kv.sweep(tmp_path, alias="flash", now=NOW) == [(writing, kv.STAGING_LEFT)]


def test_an_index_of_another_version_sweeps_the_dpc_files_it_names_and_only_those(tmp_path):
    named = tmp_path / ("dpc-kv-" + "e" * 32 + ".bin")
    named.write_bytes(b"old")
    foreign = tmp_path / "foreign.bin"
    foreign.write_bytes(b"x")
    (tmp_path / kv.INDEX_NAME).write_text(json.dumps(
        {"version": 0, "entries": [{"file": named.name}, {"file": foreign.name}]}))
    assert kv.sweep(tmp_path, alias="flash", now=NOW) == [(named.name, kv.VERSION)]
    assert not named.exists() and foreign.exists()
    assert kv.read_index(tmp_path)["version"] == kv.INDEX_VERSION


def test_the_pre_launch_sweep_compares_the_expected_identity(tmp_path):
    same = _save(tmp_path, conversation="same")
    old_engine = _save(tmp_path, conversation="v", identity={**IDENTITY, "engine_version": "0.1.50"})
    other_model = _save(tmp_path, conversation="m", identity={**IDENTITY, "model": "other"})
    other_drafter = _save(tmp_path, conversation="d", identity={**IDENTITY, "drafter": "dflash2"})
    removed = dict(kv.sweep(tmp_path, alias="flash", now=NOW, expected=IDENTITY))
    assert removed == {old_engine: kv.VERSION, other_model: kv.MODEL, other_drafter: kv.DRAFTER}
    assert _names(tmp_path) == [same]


def test_a_drafter_absent_from_expected_is_not_compared(tmp_path):
    name = _save(tmp_path, identity={**IDENTITY, "drafter": "dflash2"})
    expected = {k: v for k, v in IDENTITY.items() if k != "drafter"}
    assert kv.sweep(tmp_path, alias="flash", now=NOW, expected=expected) == []
    assert _names(tmp_path) == [name]


def test_an_entry_whose_file_is_gone_is_dropped_as_missing(tmp_path):
    name = _save(tmp_path)
    (tmp_path / name).unlink()
    assert kv.sweep(tmp_path, alias="flash", now=NOW) == [(name, kv.MISSING)]
    assert _names(tmp_path) == []


def test_a_file_the_os_refuses_to_unlink_stays_marked_for_the_next_sweep(tmp_path, monkeypatch):
    name = _save(tmp_path, ttl=10)
    real = Path.unlink

    def held(self, *a, **k):
        if self.name == name:
            raise PermissionError("being restored")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", held)
    assert kv.sweep(tmp_path, alias="flash", now=NOW + 20) == []
    [entry] = kv.read_index(tmp_path)["entries"]
    assert entry["delete_pending"] == kv.EXPIRED
    assert kv.lookup(tmp_path, alias="flash", agent_id="ag", conversation_id="c1", now=NOW) is None
    monkeypatch.setattr(Path, "unlink", real)
    assert kv.sweep(tmp_path, alias="flash", now=NOW) == [(name, kv.EXPIRED)]
    assert not (tmp_path / name).exists() and _names(tmp_path) == []


def test_a_sweep_of_a_missing_folder_writes_nothing_and_raises_nothing(tmp_path):
    missing = tmp_path / "unplugged"
    assert kv.sweep(missing, alias="flash", now=NOW) == []
    assert not missing.exists()
    assert kv.lookup(missing, alias="flash", agent_id="ag", conversation_id="c1", now=NOW) is None


def test_a_sweep_with_nothing_to_do_creates_no_index(tmp_path):
    assert kv.sweep(tmp_path, alias="flash", now=NOW, expected=IDENTITY, cap_bytes=10) == []
    assert list(tmp_path.iterdir()) == []


def test_remove_entry_deletes_one_file(tmp_path):
    keep = _save(tmp_path, conversation="k")
    gone = _save(tmp_path, conversation="g")
    assert kv.remove_entry(tmp_path, gone, reason="prefix mismatch")
    assert not (tmp_path / gone).exists() and _names(tmp_path) == [keep]


# ---- what a restore's answer does to the file


@pytest.mark.parametrize("code, body, kind", [
    (200, '{"n_restored": 10}', kv.RESTORED),
    (404, '{"error": {"code": 404, "message": "no saved session named x"}}', kv.MISSING),
    (400, '{"error": {"code": 400, "message": "session file: saved with another model (model fingerprint '
          'differs)", "kind": "invalid"}}', kv.REFUSED),
    (400, '{"error": {"message": "Unable to restore slot: No available space in KV cache or invalid slot save '
          'file"}}', kv.NO_ROOM),
    (503, '{"error": {"code": 503, "message": "out of host memory", "kind": "memory"}}', kv.NO_ROOM),
    (503, '{"error": {"code": 503, "message": "the model is not loaded"}}', kv.NOT_LOADED),
    (415, "", kv.TRANSPORT),
    (403, "", kv.TRANSPORT),
    (501, "", kv.TRANSPORT),
    (400, '{"error": {"message": "this server has one slot: 0"}}', kv.TRANSPORT),
    (500, '{"error": {"message": "engine exited; the next request starts the engine again"}}', kv.ENGINE_DIED),
    (500, '{"error": {"message": "Permission denied", "kind": "io"}}', kv.FAILED),
    (None, "ReadTimeout: timed out", kv.FAILED),
    (None, "RemoteProtocolError: connection dropped", kv.FAILED),
    (500, '{"error": {"message": "EngineSilent"}}', kv.FAILED),
])
def test_a_restore_answer_is_classified_by_status_and_body(code, body, kind):
    assert kv.classify_restore(code, body) == kind


def test_a_restore_body_may_arrive_as_a_dict():
    assert kv.classify_restore(400, {"error": {"kind": "invalid", "message": "x"}}) == kv.REFUSED


def test_keep_shields_from_the_cap_only_so_an_add_already_expired_is_refused(tmp_path):
    other = _save(tmp_path, conversation="other", saved_at=NOW, ttl=3600)
    _, final = kv.new_snapshot_names(tmp_path)
    (tmp_path / final).write_bytes(b"k")
    record = {"agent_id": "ag", "conversation_id": "late", "caller_kind": "agent"}
    entry = kv.new_entry(file=final, alias="flash", engine="strata", identity=IDENTITY, record=record,
                         n_saved=1, nbytes=1, save_ms=1, now=NOW, ttl_s=10)
    # Expired at the passed moment: deleted, not indexed, False.
    assert kv.add_entry(tmp_path, entry, cap_bytes=10 ** 9, now=NOW + 10) is False
    assert not (tmp_path / final).exists()
    assert _names(tmp_path) == [other]
    # A sweep naming a file as `keep` still expires it.
    kept = _save(tmp_path, conversation="kept", saved_at=NOW, ttl=5)
    assert kv.sweep(tmp_path, alias="flash", now=NOW + 6, keep=kept) == [(kept, kv.EXPIRED)]
