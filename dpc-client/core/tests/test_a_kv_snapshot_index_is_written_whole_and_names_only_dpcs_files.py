"""The KV snapshots index is written whole and names only DPC's files (ADR-044, amendment of 2026-11-13, point 6).

One file `dpc-kv-snapshots.json` beside the snapshots, replaced whole through a
temporary file in the same folder; opaque random file names; the folder rules
(validation without a probe, one folder per alias, the missing-disk case at
launch). No engine, no network: `tmp_path` and a clock passed in.
"""

import json
import os
import re
import threading
from pathlib import Path

import pytest

from dpc_client_core.managers import kv_snapshots as kv

NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def _clean_state():
    kv.reset_state()
    yield
    kv.reset_state()


def _entry(folder: Path, *, conversation="c1", agent="a1", alias="qwen", nbytes=100, saved_at=NOW,
           ttl=kv.DEFAULT_TTL_S, n_saved=10, save_ms=50, engine="strata", identity=None):
    _, final = kv.new_snapshot_names(folder)
    (folder / final).write_bytes(b"x" * 8)
    record = {"agent_id": agent, "conversation_id": conversation, "caller_kind": "agent",
              "prefix_sha256": "ab", "prefix_messages": 2}
    return kv.new_entry(file=final, alias=alias, engine=engine, identity=identity or {"engine_version": "1"},
                        record=record, n_saved=n_saved, nbytes=nbytes, save_ms=save_ms, now=saved_at, ttl_s=ttl)


def test_the_index_round_trips_and_leaves_no_temporary_file(tmp_path):
    index = {"entries": [{"file": "dpc-kv-" + "0" * 32 + ".bin", "alias": "a"}], "rates": {"write_bytes_per_s": 5.0},
             "sizes": {}}
    kv.write_index(tmp_path, index)
    assert sorted(p.name for p in tmp_path.iterdir()) == [kv.INDEX_NAME]
    back = kv.read_index(tmp_path)
    assert back["version"] == kv.INDEX_VERSION
    assert back["entries"] == index["entries"]
    assert back["rates"] == {"write_bytes_per_s": 5.0}


def test_a_replace_the_os_refuses_is_retried_then_lands(tmp_path, monkeypatch):
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("held by an indexer")
        return real(src, dst)

    monkeypatch.setattr(kv.os, "replace", flaky)
    monkeypatch.setattr(kv, "_REPLACE_PAUSE_S", 0.0)
    kv.write_index(tmp_path, {"entries": []})
    assert calls["n"] == 3
    assert (tmp_path / kv.INDEX_NAME).exists()


def test_a_replace_refused_every_time_raises_and_keeps_the_old_index(tmp_path, monkeypatch):
    kv.write_index(tmp_path, {"entries": [], "rates": {"write_bytes_per_s": 1.0}})

    def refuse(src, dst):
        raise PermissionError("held")

    monkeypatch.setattr(kv.os, "replace", refuse)
    monkeypatch.setattr(kv, "_REPLACE_PAUSE_S", 0.0)
    with pytest.raises(PermissionError):
        kv.write_index(tmp_path, {"entries": []})
    assert sorted(p.name for p in tmp_path.iterdir()) == [kv.INDEX_NAME]
    assert kv.read_index(tmp_path)["rates"] == {"write_bytes_per_s": 1.0}


def test_file_names_are_opaque_and_pass_stratas_filename_rule(tmp_path):
    staging, final = kv.new_snapshot_names(tmp_path)
    assert re.fullmatch(r"dpc-kv-[0-9a-f]{32}\.staging", staging)
    assert re.fullmatch(r"dpc-kv-[0-9a-f]{32}\.bin", final)
    assert staging[len("dpc-kv-"):-len(".staging")] == final[len("dpc-kv-"):-len(".bin")]
    # Strata v0.2.11 slot_filename_problem: no leading dot, no separator or drive, no trailing dot.
    for name in (staging, final):
        assert not name.startswith(".") and not name.endswith((".", " "))
        assert not any(c in name for c in '/\\:*?"<>|')
    assert kv.new_snapshot_names(tmp_path)[1] != final


def test_an_index_of_another_version_or_torn_reads_as_empty(tmp_path):
    (tmp_path / kv.INDEX_NAME).write_text(json.dumps({"version": 99, "entries": [{"file": "x.bin"}]}))
    assert kv.read_index(tmp_path)["entries"] == []
    (tmp_path / kv.INDEX_NAME).write_text('{"version": 1, "entries": [')
    assert kv.read_index(tmp_path)["entries"] == []


def test_an_index_entry_naming_a_path_outside_the_folder_is_ignored(tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep")
    folder = tmp_path / "kv"
    folder.mkdir()
    (folder / kv.INDEX_NAME).write_text(json.dumps({"version": 1, "entries": [
        {"file": "../outside.bin", "expires_at": 0}, {"file": kv.INDEX_NAME, "expires_at": 0}]}))
    assert kv.read_index(folder)["entries"] == []
    kv.sweep(folder, alias="qwen", now=NOW)
    assert outside.exists() and (folder / kv.INDEX_NAME).exists()


def test_an_unreadable_index_is_never_overwritten_by_an_add(tmp_path, monkeypatch):
    first = _entry(tmp_path, conversation="c1")
    assert kv.add_entry(tmp_path, first, cap_bytes=10_000)
    second = _entry(tmp_path, conversation="c2")
    real = Path.read_bytes

    def refuse(self, *a, **k):
        if self.name == kv.INDEX_NAME:
            raise PermissionError("locked")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", refuse)
    monkeypatch.setattr(kv, "_REPLACE_PAUSE_S", 0.0)
    with pytest.raises(PermissionError):
        kv.add_entry(tmp_path, second, cap_bytes=10_000)
    assert kv.sweep(tmp_path, alias="qwen", now=NOW) == []          # logged, never raised
    monkeypatch.setattr(Path, "read_bytes", real)
    assert [e["conversation_id"] for e in kv.read_index(tmp_path)["entries"]] == ["c1"]


# ---- a hand-edited, corrupt or foreign index (review items 3-5)


@pytest.mark.parametrize("content", [
    b"\xff\xfe{garbage",                                              # not UTF-8
    b'{"version": 1, "entries": [',                                   # torn
    b"[1, 2]",                                                        # not an object
    b'{"entries": [{"file": "dpc-kv-aa.bin"}]}',                      # no version
    b'{"version": "1", "entries": []}',                               # version not an int
    b'{"version": 1, "entries": {"file": "dpc-kv-aa.bin"}}',          # entries not a list
])
def test_a_corrupt_or_foreign_index_is_moved_aside_never_overwritten(tmp_path, content):
    (tmp_path / kv.INDEX_NAME).write_bytes(content)
    (tmp_path / "dpc-kv-aa.bin").write_bytes(b"named only by the bad index")
    kv.register_folder("qwen", tmp_path)
    assert kv.sweep(tmp_path, alias="qwen", now=NOW) == []
    assert kv.lookup(tmp_path, alias="qwen", agent_id=None, conversation_id="c", now=NOW) is None
    assert kv.status_of("qwen", now=NOW)["files"] == 0
    aside = [p for p in tmp_path.iterdir() if p.name.startswith(kv.INDEX_NAME + ".corrupt-")]
    assert len(aside) == 1 and aside[0].read_bytes() == content
    entry = _entry(tmp_path, conversation="fresh")
    assert kv.add_entry(tmp_path, entry, cap_bytes=10_000)
    assert [e["conversation_id"] for e in kv.read_index(tmp_path)["entries"]] == ["fresh"]
    assert aside[0].read_bytes() == content


def test_ill_typed_entries_are_skipped_and_never_raise(tmp_path):
    good = _entry(tmp_path, conversation="good")
    for name in ("dpc-kv-aa.bin", "dpc-kv-bb.bin", "dpc-kv-cc.bin"):
        (tmp_path / name).write_bytes(b"x")
    bad = [
        {"file": "dpc-kv-aa.bin", "expires_at": 9e18, "bytes": "big"},
        {"file": "dpc-kv-bb.bin", "expires_at": "tomorrow"},
        {"file": "dpc-kv-cc.bin", "prefix_messages": "2", "expires_at": 9e18},
        "not a dict",
        {"file": 7},
    ]
    (tmp_path / kv.INDEX_NAME).write_text(json.dumps({"version": 1, "entries": bad + [good]}))
    kv.register_folder("qwen", tmp_path)
    assert kv.sweep(tmp_path, alias="qwen", now=NOW, cap_bytes=1_000) == []
    assert [e["file"] for e in kv.read_index(tmp_path)["entries"]] == [good["file"]]
    status = kv.status_of("qwen", now=NOW)
    assert status["files"] == 1 and status["bytes"] == 100
    assert kv.lookup(tmp_path, alias="qwen", agent_id="a1", conversation_id="good", now=NOW)["file"] == good["file"]
    # The skipped entries' files are not touched: unindexed, and fresh by mtime.
    assert all((tmp_path / n).exists() for n in ("dpc-kv-aa.bin", "dpc-kv-bb.bin", "dpc-kv-cc.bin"))


def test_only_dpc_names_are_ever_deleted_whatever_the_index_says(tmp_path):
    thesis = tmp_path / "thesis.docx"
    thesis.write_text("precious")
    session = tmp_path / "image-job.session"
    session.write_text("strata image job")
    # A current-version index whose entries name foreign files, expired and pending.
    (tmp_path / kv.INDEX_NAME).write_text(json.dumps({"version": 1, "entries": [
        {"file": "thesis.docx", "expires_at": 0},
        {"file": "image-job.session", "delete_pending": "expired"}]}))
    kv.sweep(tmp_path, alias="qwen", now=NOW, cap_bytes=0)
    assert thesis.exists() and session.exists()
    # Another integer version: only its DPC names go.
    old = tmp_path / "dpc-kv-0ld.bin"
    old_dpc = tmp_path / "dpc-kv-0ab1.bin"
    old_dpc.write_bytes(b"x")
    (tmp_path / kv.INDEX_NAME).write_text(json.dumps({"version": 0, "entries": [
        {"file": "thesis.docx"}, {"file": old.name}, {"file": old_dpc.name}]}))
    assert kv.sweep(tmp_path, alias="qwen", now=NOW) == [(old_dpc.name, kv.VERSION)]
    assert thesis.exists() and session.exists()


def test_a_file_that_alone_exceeds_the_cap_is_deleted_and_not_indexed(tmp_path):
    entry = _entry(tmp_path, nbytes=5_000)
    assert kv.add_entry(tmp_path, entry, cap_bytes=4_000) is False
    assert not (tmp_path / entry["file"]).exists()
    assert kv.read_index(tmp_path)["entries"] == []


def test_a_newer_save_of_the_same_key_replaces_the_older_file(tmp_path):
    old = _entry(tmp_path, saved_at=NOW)
    assert kv.add_entry(tmp_path, old, cap_bytes=10_000)
    new = _entry(tmp_path, saved_at=NOW + 10)
    assert kv.add_entry(tmp_path, new, cap_bytes=10_000)
    assert not (tmp_path / old["file"]).exists()
    assert [e["file"] for e in kv.read_index(tmp_path)["entries"]] == [new["file"]]
    other = _entry(tmp_path, conversation="c2", saved_at=NOW + 20)
    kv.add_entry(tmp_path, other, cap_bytes=10_000)
    assert len(kv.read_index(tmp_path)["entries"]) == 2


def test_an_add_records_the_write_rate_and_the_aliass_bytes_per_token(tmp_path):
    floor = kv.SEED_SIZES["strata"]["floor_bytes"]
    entry = _entry(tmp_path, nbytes=int(floor + 1_000 * 15_000), n_saved=1_000, save_ms=2_000)
    kv.add_entry(tmp_path, entry, cap_bytes=kv.cap_bytes_of({}))
    rates = kv.folder_rates(tmp_path)
    assert rates["write_bytes_per_s"] == pytest.approx(entry["bytes"] / 2.0)
    sizes = kv.alias_sizes(tmp_path, "qwen", "strata")
    assert sizes["floor_bytes"] == pytest.approx(floor)
    assert sizes["bytes_per_token"] == pytest.approx(15_000, rel=1e-6)
    assert kv.alias_sizes(tmp_path, "other", "llamacpp_server") == kv.SEED_SIZES["llamacpp_server"]
    kv.note_restore_rate(tmp_path, 40_000_000, 1_000)
    assert kv.folder_rates(tmp_path)["read_bytes_per_s"] == pytest.approx(40_000_000)


def test_a_tiny_or_instant_sample_leaves_the_measured_rates_unchanged(tmp_path):
    floor = kv.SEED_SIZES["strata"]["floor_bytes"]
    real = _entry(tmp_path, conversation="real", nbytes=int(floor + 1_000 * 15_000), n_saved=1_000,
                  save_ms=2_000)
    kv.add_entry(tmp_path, real, cap_bytes=kv.cap_bytes_of({}))
    kv.note_restore_rate(tmp_path, 400_000_000, 4_000)
    before_rates = kv.folder_rates(tmp_path)
    before_sizes = kv.alias_sizes(tmp_path, "qwen", "strata")
    # A test engine's 4 KiB file in 15 ms, and a big file reported in 0 or 10 ms.
    for nbytes, ms in ((4096, 15), (kv.RATE_SAMPLE_MIN_BYTES - 1, 5_000), (500_000_000, 0),
                       (500_000_000, kv.RATE_SAMPLE_MIN_MS - 1)):
        noise = _entry(tmp_path, conversation=f"noise-{nbytes}-{ms}", nbytes=nbytes, n_saved=30_000, save_ms=ms)
        assert kv.add_entry(tmp_path, noise, cap_bytes=kv.cap_bytes_of({}))
        kv.note_restore_rate(tmp_path, nbytes, ms)
    assert kv.folder_rates(tmp_path) == before_rates
    assert kv.alias_sizes(tmp_path, "qwen", "strata") == before_sizes
    # On an empty folder a tiny sample measures nothing: the rate stays unknown.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    kv.add_entry(fresh, _entry(fresh, nbytes=4096, n_saved=30_000, save_ms=15), cap_bytes=10 ** 9)
    assert kv.folder_rates(fresh) == {}
    assert kv.alias_sizes(fresh, "qwen", "strata") == kv.SEED_SIZES["strata"]


def test_adds_from_many_threads_all_land(tmp_path):
    entries = [_entry(tmp_path, conversation=f"c{i}") for i in range(12)]
    threads = [threading.Thread(target=kv.add_entry, args=(tmp_path, e), kwargs={"cap_bytes": 10 ** 9})
               for e in entries]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(e["file"] for e in kv.read_index(tmp_path)["entries"]) == sorted(e["file"] for e in entries)


# ---- the folder rules


def test_folder_validation_refuses_what_point_6_names(tmp_path):
    assert kv.folder_error(None, other_folders=[], install_dir=None) is None
    assert kv.folder_error("", other_folders=[], install_dir=None) is None
    assert "absolute" in kv.folder_error("relative/kv", other_folders=[], install_dir=None)
    assert "control character" in kv.folder_error(str(tmp_path / "a\x01b"), other_folders=[], install_dir=None)
    assert "root" in kv.folder_error(os.path.abspath(os.sep), other_folders=[], install_dir=None)
    system = kv._system_folders()[0] if os.name != "nt" else os.environ.get("SystemRoot", r"C:\Windows")
    assert "system folder" in kv.folder_error(os.path.join(system, "kv"), other_folders=[], install_dir=None)
    install = tmp_path / "strata"
    assert "Strata install" in kv.folder_error(str(install / "kv"), other_folders=[], install_dir=str(install))
    mine = str(tmp_path / "kv")
    assert "another alias" in kv.folder_error(mine, other_folders=[mine], install_dir=None)
    assert kv.folder_error(mine, other_folders=[str(tmp_path / "other")], install_dir=None) is None


def test_folder_validation_does_not_probe_a_missing_disk(tmp_path):
    missing = tmp_path / "unplugged" / "kv"
    assert kv.folder_error(str(missing), other_folders=[], install_dir=None) is None
    assert not (tmp_path / "unplugged").exists()


def test_alias_fields_are_whole_seconds_and_positive_gib():
    assert kv.alias_field_errors({}) == []
    assert kv.alias_field_errors({"kv_snapshot_ttl_s": 3600, "kv_snapshot_max_gib": 1.5}) == []
    assert kv.alias_field_errors({"kv_snapshot_ttl_s": 3600.0}) == []
    for bad in (0, -1, 1.5, True, "3600"):
        assert kv.alias_field_errors({"kv_snapshot_ttl_s": bad}), bad
    for bad in (0, -2, True, "32", float("inf")):
        assert kv.alias_field_errors({"kv_snapshot_max_gib": bad}), bad
    assert kv.ttl_s_of({}) == kv.DEFAULT_TTL_S
    assert kv.cap_bytes_of({}) == kv.DEFAULT_MAX_GIB * 1024 ** 3
    assert kv.cap_bytes_of({"kv_snapshot_max_gib": 2}) == 2 * 1024 ** 3


def test_two_aliases_naming_one_folder_are_refused_on_the_second(tmp_path):
    folder = str(tmp_path / "kv")
    errors = kv.provider_list_errors([
        {"alias": "a", "type": "strata", "slot_save_path": folder},
        {"alias": "b", "type": "llamacpp_server", "slot_save_path": folder},
        {"alias": "c", "type": "llamacpp_server", "kv_snapshot_ttl_s": 0},
        {"alias": "d", "type": "ollama"},
    ])
    assert len(errors) == 2
    assert errors[0].startswith("b: ") and "'a'" in errors[0]
    assert errors[1].startswith("c: kv_snapshot_ttl_s")


def test_a_missing_parent_turns_saving_off_and_creates_nothing(tmp_path):
    folder = tmp_path / "unplugged" / "kv"
    assert kv.prepare_folder(folder) == kv.FOLDER_MISSING == "folder missing"
    assert not (tmp_path / "unplugged").exists()


def test_an_existing_parent_gets_the_folder_and_a_file_in_the_way_raises(tmp_path):
    assert kv.prepare_folder(tmp_path / "kv") is None
    assert (tmp_path / "kv").is_dir()
    assert kv.prepare_folder(tmp_path / "kv") is None
    assert list((tmp_path / "kv").iterdir()) == []
    (tmp_path / "blocked").write_text("a file")
    with pytest.raises(OSError):
        kv.prepare_folder(tmp_path / "blocked")


def test_a_file_where_the_parent_should_be_is_refused_not_read_as_a_missing_disk(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a folder")
    with pytest.raises(OSError):
        kv.prepare_folder(blocker / "inside")
    assert blocker.is_file() and blocker.read_text() == "a file, not a folder"
    # A parent that does not exist at all is still the missing disk.
    assert kv.prepare_folder(tmp_path / "absent" / "inside") == kv.FOLDER_MISSING


# ---- one folder, compared one way (review item 10) and the unmounted disk (item 11)


def test_folders_differing_only_in_case_are_one_folder_on_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(kv.sys, "platform", "darwin")
    upper, lower = str(tmp_path / "Users" / "KV"), str(tmp_path / "users" / "kv")
    assert kv._path_key(upper) == kv._path_key(lower)
    errors = kv.provider_list_errors([
        {"alias": "a", "slot_save_path": upper},
        {"alias": "b", "slot_save_path": lower},
    ])
    assert len(errors) == 1 and errors[0].startswith("b: ")
    assert "another alias" in kv.folder_error(lower, other_folders=[upper], install_dir=None)


@pytest.mark.skipif(os.name == "nt", reason="Windows folds case through normcase on every platform value")
def test_folders_differing_only_in_case_are_two_folders_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(kv.sys, "platform", "linux")
    assert kv._path_key(str(tmp_path / "KV")) != kv._path_key(str(tmp_path / "kv"))


def test_a_link_and_its_target_are_one_folder(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink rights here")
    assert kv._key(link) == kv._key(target)
    assert kv._folder_lock(link) is kv._folder_lock(target)
    assert len(kv.provider_list_errors([{"alias": "a", "slot_save_path": str(target)},
                                        {"alias": "b", "slot_save_path": str(link)}])) == 1


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_an_empty_unmounted_mount_point_is_a_missing_folder(monkeypatch, tmp_path, platform, caplog):
    removable = tmp_path / "media"
    mount_point = removable / "usb"
    mount_point.mkdir(parents=True)
    monkeypatch.setattr(kv.sys, "platform", platform)
    monkeypatch.setattr(kv, "_REMOVABLE_ROOTS", {"linux": (str(removable),), "darwin": (str(removable),)})
    monkeypatch.setattr(kv.os.path, "ismount", lambda p: False)
    with caplog.at_level("WARNING", logger=kv.logger.name):
        assert kv.prepare_folder(mount_point / "kv") == kv.FOLDER_MISSING
    assert "heuristic" in caplog.text
    assert not (mount_point / "kv").exists()
    # Mounted, or holding files: an ordinary parent.
    monkeypatch.setattr(kv.os.path, "ismount", lambda p: Path(p) == mount_point)
    assert kv.prepare_folder(mount_point / "kv") is None


def test_the_unmounted_heuristic_is_off_on_windows_and_outside_removable_roots(monkeypatch, tmp_path):
    empty = tmp_path / "media" / "usb"
    empty.mkdir(parents=True)
    monkeypatch.setattr(kv.os.path, "ismount", lambda p: False)
    monkeypatch.setattr(kv, "_REMOVABLE_ROOTS", {"linux": (str(tmp_path / "media"),)})
    monkeypatch.setattr(kv.sys, "platform", "win32")
    assert kv.prepare_folder(empty / "kv") is None
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(kv.sys, "platform", "linux")
    assert kv.prepare_folder(other / "kv") is None
