"""Spec tests for the KV snapshot index, lookup and sweep.

Written from ADR-044 (amendment 2026-11-13, points 4, 6), the slice-1 plan
(section 2, P1) and the coordinator's contract changes, without sight of the
implementation. The index is read only through the module's own functions, or as
raw text (does it name a file), so the tests do not depend on its inner layout.

Times are anchored on the real clock: add_entry sweeps at real time after it adds.
"""

import json
import os
import time
from pathlib import Path

import pytest

from dpc_client_core.managers import kv_snapshots as kv

NOW = time.time()
ALIAS = "strata_spec_alias"
HOUR = 3600.0


def _entry(name, *, saved_at, size=400, expires_at=None, conversation_id="conv-1",
           agent_id="agent-1", alias=ALIAS):
    """One index entry, with the keys the plan names (section 5)."""
    return {
        "file": name,
        "alias": alias,
        "engine": "strata",
        "engine_version": "0.1.57",
        "model": "model-a",
        "agent_id": agent_id,
        "conversation_id": conversation_id,
        "caller_kind": "agent",
        "n_saved": 1000,
        "bytes": size,
        "save_ms": 12,
        "saved_at": saved_at,
        "expires_at": expires_at if expires_at is not None else saved_at + 24 * HOUR,
        "prefix_sha256": "0" * 64,
        "prefix_messages": 1,
    }


def _put(folder: Path, entry, *, cap_bytes=10**12):
    """Write the snapshot file with the entry's size and add the entry."""
    (folder / entry["file"]).write_bytes(b"x" * entry["bytes"])
    kv.add_entry(folder, entry, cap_bytes=cap_bytes)


def _index_path(folder: Path) -> Path:
    return folder / kv.INDEX_NAME


def _index_text(folder: Path) -> str:
    p = _index_path(folder)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _swept(result):
    return {f: reason for f, reason in result}


def _get(entry, key):
    if isinstance(entry, dict):
        return entry.get(key)
    return getattr(entry, key, None)


def _rename_in_index(folder: Path, old: str, new: str):
    """Make the index name `new` where it named `old`, whatever its layout."""
    p = _index_path(folder)
    text = p.read_text(encoding="utf-8")
    assert old in text
    p.write_text(text.replace(old, new), encoding="utf-8")


def _age(path: Path, seconds: float):
    t = time.time() - seconds
    os.utime(path, (t, t))


# ---------------------------------------------------------------- TTL / lookup

def test_an_expired_entry_is_gone_after_lookup(tmp_path):
    # Not yet expired when added (the add sweeps at real time), expired at the
    # lookup's `now`.
    e = _entry("dpc-kv-aaaa1111.bin", saved_at=NOW - 2 * HOUR, expires_at=NOW + 300.0)
    _put(tmp_path, e)
    assert (tmp_path / e["file"]).exists()

    found = kv.lookup(tmp_path, alias=ALIAS, agent_id="agent-1",
                      conversation_id="conv-1", now=NOW + HOUR)

    assert found is None
    assert not (tmp_path / e["file"]).exists()
    assert e["file"] not in _index_text(tmp_path)


def test_a_live_entry_is_found_by_lookup_and_its_file_stays(tmp_path):
    e = _entry("dpc-kv-bbbb2222.bin", saved_at=NOW - HOUR, expires_at=NOW + HOUR)
    _put(tmp_path, e)

    found = kv.lookup(tmp_path, alias=ALIAS, agent_id="agent-1",
                      conversation_id="conv-1", now=NOW)

    assert found is not None
    assert _get(found, "file") == e["file"]
    assert (tmp_path / e["file"]).exists()


def test_lookup_of_another_conversation_finds_nothing_and_deletes_nothing(tmp_path):
    e = _entry("dpc-kv-cccc3333.bin", saved_at=NOW - HOUR, expires_at=NOW + HOUR)
    _put(tmp_path, e)

    found = kv.lookup(tmp_path, alias=ALIAS, agent_id="agent-1",
                      conversation_id="some-other-conversation", now=NOW)

    assert found is None
    assert (tmp_path / e["file"]).exists()
    assert e["file"] in _index_text(tmp_path)


def test_an_expired_entry_is_swept_with_reason_expired(tmp_path):
    old = _entry("dpc-kv-dddd4444.bin", saved_at=NOW - 3 * HOUR, expires_at=NOW + 300.0,
                 conversation_id="old")
    fresh = _entry("dpc-kv-eeee5555.bin", saved_at=NOW - HOUR, expires_at=NOW + 3 * HOUR,
                   conversation_id="fresh")
    _put(tmp_path, old)
    _put(tmp_path, fresh)
    assert (tmp_path / old["file"]).exists()      # the adds did not expire it

    swept = _swept(kv.sweep(tmp_path, alias=ALIAS, now=NOW + HOUR, expected={},
                            cap_bytes=10**12))

    assert swept == {old["file"]: "expired"}
    assert not (tmp_path / old["file"]).exists()
    assert (tmp_path / fresh["file"]).exists()


def test_add_entry_itself_removes_an_entry_already_expired_at_that_moment(tmp_path):
    stale = _entry("dpc-kv-0a0a0a01.bin", saved_at=NOW - 3 * HOUR, expires_at=NOW - HOUR)

    (tmp_path / stale["file"]).write_bytes(b"x" * stale["bytes"])
    kv.add_entry(tmp_path, stale, cap_bytes=10**12, now=time.time())

    assert not (tmp_path / stale["file"]).exists()
    assert stale["file"] not in _index_text(tmp_path)


# ------------------------------------------------------------------------- cap

def test_over_the_cap_the_oldest_by_saved_time_goes_first(tmp_path):
    # Inserted out of order on purpose: age is saved_at, not insertion order.
    mid = _entry("dpc-kv-0a0b0001.bin", saved_at=NOW - 200, conversation_id="mid")
    old = _entry("dpc-kv-0a0b0002.bin", saved_at=NOW - 300, conversation_id="old")
    new = _entry("dpc-kv-0a0b0003.bin", saved_at=NOW - 100, conversation_id="new")
    for e in (mid, old, new):
        _put(tmp_path, e)

    # 3 x 400 = 1200 bytes held; a cap of 900 asks for one file to go.
    swept = _swept(kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={}, cap_bytes=900))

    assert swept == {old["file"]: "over_cap"}
    assert not (tmp_path / old["file"]).exists()
    assert (tmp_path / mid["file"]).exists()
    assert (tmp_path / new["file"]).exists()


def test_over_the_cap_several_oldest_go_until_it_fits(tmp_path):
    files = []
    for i in range(4):
        e = _entry(f"dpc-kv-0c0d000{i}.bin", saved_at=NOW - (400 - i * 100),
                   conversation_id=f"c{i}")
        _put(tmp_path, e)
        files.append(e["file"])

    # 4 x 400 held, cap 850: the two oldest go, two newest stay (800 <= 850).
    swept = _swept(kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={}, cap_bytes=850))

    assert set(swept) == {files[0], files[1]}
    assert (tmp_path / files[2]).exists() and (tmp_path / files[3]).exists()


def test_the_just_written_file_is_never_removed_for_the_cap(tmp_path):
    oldest = _entry("dpc-kv-0e0f0001.bin", saved_at=NOW - 300, conversation_id="oldest")
    mid = _entry("dpc-kv-0e0f0002.bin", saved_at=NOW - 200, conversation_id="mid")
    new = _entry("dpc-kv-0e0f0003.bin", saved_at=NOW - 100, conversation_id="new")
    for e in (oldest, mid, new):
        _put(tmp_path, e)

    # `keep` shields the oldest, so the next oldest goes instead.
    swept = _swept(kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={}, cap_bytes=800,
                            keep=oldest["file"]))

    assert oldest["file"] not in swept
    assert (tmp_path / oldest["file"]).exists()
    assert swept == {mid["file"]: "over_cap"}


def test_a_file_larger_than_the_cap_is_not_added(tmp_path):
    big = _entry("dpc-kv-0b1c0001.bin", saved_at=NOW, size=5000, conversation_id="big")
    (tmp_path / big["file"]).write_bytes(b"x" * big["bytes"])

    kv.add_entry(tmp_path, big, cap_bytes=1000)

    assert big["file"] not in _index_text(tmp_path)
    assert kv.lookup(tmp_path, alias=ALIAS, agent_id="agent-1",
                     conversation_id="big", now=NOW) is None


def test_a_file_that_fits_the_cap_is_added(tmp_path):
    ok = _entry("dpc-kv-0f170001.bin", saved_at=NOW, size=500, conversation_id="fits",
                expires_at=NOW + HOUR)
    (tmp_path / ok["file"]).write_bytes(b"x" * ok["bytes"])

    kv.add_entry(tmp_path, ok, cap_bytes=1000)

    assert ok["file"] in _index_text(tmp_path)
    assert kv.lookup(tmp_path, alias=ALIAS, agent_id="agent-1",
                     conversation_id="fits", now=NOW) is not None


# ------------------------------------------------------------- foreign files

def test_a_foreign_file_is_never_deleted_by_the_sweep(tmp_path):
    foreign = [tmp_path / "image-job-session.bin", tmp_path / "notes.txt",
               tmp_path / "slot_0.bin"]
    for p in foreign:
        p.write_bytes(b"y" * 10_000)
    old = _entry("dpc-kv-0f0e0001.bin", saved_at=NOW - 3 * HOUR, expires_at=NOW + 300.0,
                 conversation_id="old")
    _put(tmp_path, old)

    kv.sweep(tmp_path, alias=ALIAS, now=NOW + HOUR, expected={}, cap_bytes=1)

    for p in foreign:
        assert p.exists() and p.stat().st_size == 10_000
    assert not (tmp_path / old["file"]).exists()


def test_a_foreign_file_does_not_count_toward_the_cap(tmp_path):
    (tmp_path / "image-job-session.bin").write_bytes(b"y" * 50_000)
    a = _entry("dpc-kv-0a0c0001.bin", saved_at=NOW - 200, conversation_id="a")
    b = _entry("dpc-kv-0a0c0002.bin", saved_at=NOW - 100, conversation_id="b")
    _put(tmp_path, a)
    _put(tmp_path, b)

    # DPC's own files hold 800 bytes; the cap is exactly that. The 50 kB foreign
    # file would blow any cap if it were counted.
    swept = kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={}, cap_bytes=800)

    assert swept == []
    assert (tmp_path / a["file"]).exists() and (tmp_path / b["file"]).exists()
    assert (tmp_path / "image-job-session.bin").exists()


def test_a_foreign_file_is_not_added_to_the_index_by_the_sweep(tmp_path):
    foreign = tmp_path / "image-job-session.bin"
    foreign.write_bytes(b"y" * 100)
    kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={}, cap_bytes=10**9)
    assert "image-job-session.bin" not in _index_text(tmp_path)
    assert foreign.exists()


# --------------------------------------------------------- DPC names only

@pytest.mark.parametrize("bad", ["thesis.docx", "dpc-kv-xyz.bin", "dpc-kv-.bin",
                                 "dpc-kv-0a01.staging", "../dpc-kv-0a01.bin",
                                 "sub/dpc-kv-0a01.bin", "DPC-KV-0A01.BIN"])
def test_add_entry_refuses_a_name_that_is_not_a_dpc_snapshot_name(tmp_path, bad):
    e = _entry(bad, saved_at=NOW)
    with pytest.raises(ValueError):
        kv.add_entry(tmp_path, e, cap_bytes=10**9)


def test_add_entry_accepts_a_hex_dpc_name(tmp_path):
    e = _entry("dpc-kv-0123456789abcdef.bin", saved_at=NOW, expires_at=NOW + HOUR)
    _put(tmp_path, e)
    assert e["file"] in _index_text(tmp_path)


def test_an_index_entry_naming_a_non_dpc_file_never_deletes_it(tmp_path):
    victim = _entry("dpc-kv-0d0d0d01.bin", saved_at=NOW - 3 * HOUR,
                    expires_at=NOW + 300.0)           # expired at the sweep below
    _put(tmp_path, victim)
    _rename_in_index(tmp_path, victim["file"], "thesis.docx")
    thesis = tmp_path / "thesis.docx"
    thesis.write_bytes(b"my thesis")
    (tmp_path / victim["file"]).unlink()

    # Expired, over the cap, whatever: a name outside DPC's pattern is never unlinked.
    kv.sweep(tmp_path, alias=ALIAS, now=NOW + HOUR, expected={}, cap_bytes=1)

    assert thesis.exists() and thesis.read_bytes() == b"my thesis"


# ---------------------------------------------------------------- orphans

def test_an_unindexed_dpc_file_older_than_60_s_is_deleted_as_an_orphan(tmp_path):
    orphan = tmp_path / "dpc-kv-0ab0ba01.bin"
    orphan.write_bytes(b"x" * 10)
    _age(orphan, 120)

    swept = _swept(kv.sweep(tmp_path, alias=ALIAS, now=time.time(), expected={},
                            cap_bytes=10**9))

    assert not orphan.exists()
    assert swept.get(orphan.name) == "orphan"


def test_a_fresh_unindexed_dpc_file_is_not_an_orphan_yet(tmp_path):
    fresh = tmp_path / "dpc-kv-0ab0ba02.bin"
    fresh.write_bytes(b"x" * 10)
    _age(fresh, 5)       # a save may be between its rename and its index write

    swept = _swept(kv.sweep(tmp_path, alias=ALIAS, now=time.time(), expected={},
                            cap_bytes=10**9))

    assert fresh.exists()
    assert fresh.name not in swept


def test_an_orphan_sweep_leaves_foreign_files_of_any_age(tmp_path):
    foreign = tmp_path / "image-job-session.bin"
    foreign.write_bytes(b"y" * 10)
    _age(foreign, 10_000)

    kv.sweep(tmp_path, alias=ALIAS, now=time.time(), expected={}, cap_bytes=10**9)

    assert foreign.exists()


# ------------------------------------------------------------ staging, version

def test_a_staging_leftover_is_deleted_by_the_sweep(tmp_path):
    staging = tmp_path / "dpc-kv-0123abcd.staging"
    staging.write_bytes(b"half a write")
    foreign = tmp_path / "image-job-session.bin"
    foreign.write_bytes(b"y" * 10)

    swept = _swept(kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={},
                            cap_bytes=10**9))

    assert not staging.exists()
    assert swept.get(staging.name) == "staging_left"
    assert foreign.exists()


def test_a_staging_leftover_is_deleted_when_there_is_no_index_at_all(tmp_path):
    assert not _index_path(tmp_path).exists()
    staging = tmp_path / "dpc-kv-feedbeef.staging"
    staging.write_bytes(b"x")

    kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={}, cap_bytes=10**9)

    assert not staging.exists()


def test_a_non_dpc_staging_looking_name_is_left_alone(tmp_path):
    other = tmp_path / "render-job.staging"
    other.write_bytes(b"x")
    kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={}, cap_bytes=10**9)
    assert other.exists()


def test_an_index_of_another_integer_version_sweeps_only_its_dpc_names(tmp_path):
    a = _entry("dpc-kv-0e0a0001.bin", saved_at=NOW - 100, expires_at=NOW + HOUR,
               conversation_id="a")
    b = _entry("dpc-kv-0e0a0002.bin", saved_at=NOW - 50, expires_at=NOW + HOUR,
               conversation_id="b")
    _put(tmp_path, a)
    _put(tmp_path, b)
    foreign = tmp_path / "image-job-session.bin"
    foreign.write_bytes(b"y" * 10)

    # b's name in the index becomes one outside DPC's pattern.
    _rename_in_index(tmp_path, b["file"], "thesis.docx")
    (tmp_path / b["file"]).unlink()
    thesis = tmp_path / "thesis.docx"
    thesis.write_bytes(b"my thesis")

    # Turn the index we wrote into one of an integer version this code does not know.
    p = _index_path(tmp_path)
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["version"] == kv.INDEX_VERSION
    doc["version"] = kv.INDEX_VERSION + 1000
    p.write_text(json.dumps(doc), encoding="utf-8")

    swept = _swept(kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={},
                            cap_bytes=10**12))

    assert a["file"] in swept
    assert not (tmp_path / a["file"]).exists()
    assert thesis.exists()
    assert foreign.exists()


@pytest.mark.parametrize("breaker", ["drop", "string", "float", "null"])
def test_a_json_object_without_an_integer_version_is_not_dpcs_index(tmp_path, breaker):
    e = _entry("dpc-kv-0f0f0f01.bin", saved_at=NOW - 100, expires_at=NOW + HOUR)
    _put(tmp_path, e)
    p = _index_path(tmp_path)
    doc = json.loads(p.read_text(encoding="utf-8"))
    if breaker == "drop":
        del doc["version"]
    else:
        doc["version"] = {"string": "1", "float": 1.5, "null": None}[breaker]
    p.write_text(json.dumps(doc), encoding="utf-8")
    snapshot = tmp_path / e["file"]
    snapshot.write_bytes(b"x" * 400)      # fresh mtime: no orphan reading either

    kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={}, cap_bytes=10**12)

    assert snapshot.exists()


def test_an_index_that_is_not_a_json_object_names_nothing_to_sweep(tmp_path):
    victim = tmp_path / "dpc-kv-0f0f0f02.bin"
    victim.write_bytes(b"x" * 10)
    _index_path(tmp_path).write_text("[\"dpc-kv-0f0f0f02.bin\"]", encoding="utf-8")

    kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={}, cap_bytes=10**12)

    assert victim.exists()


def test_an_index_of_the_current_version_is_written_with_that_version(tmp_path):
    e = _entry("dpc-kv-0c0c0001.bin", saved_at=NOW, expires_at=NOW + HOUR)
    _put(tmp_path, e)
    doc = json.loads(_index_path(tmp_path).read_text(encoding="utf-8"))
    assert doc["version"] == kv.INDEX_VERSION
    assert type(doc["version"]) is int


def test_a_sweep_of_an_empty_folder_is_a_no_op(tmp_path):
    assert kv.sweep(tmp_path, alias=ALIAS, now=NOW, expected={}, cap_bytes=10**9) == []


# ------------------------------------- same conversation id, different owners

def test_lookup_tells_an_agents_entry_from_the_owners_chat_with_the_same_conversation_id(tmp_path):
    agent = _entry("dpc-kv-0a1a0001.bin", saved_at=NOW - 200, expires_at=NOW + HOUR,
                   conversation_id="X", agent_id="agent-A")
    owner = _entry("dpc-kv-0a1a0002.bin", saved_at=NOW - 100, expires_at=NOW + HOUR,
                   conversation_id="X", agent_id=None)
    owner["caller_kind"] = kv.OWNER_CHAT_KIND
    _put(tmp_path, agent)
    _put(tmp_path, owner)

    for_agent = kv.lookup(tmp_path, alias=ALIAS, agent_id="agent-A",
                          conversation_id="X", now=NOW)
    for_owner = kv.lookup(tmp_path, alias=ALIAS, agent_id=None,
                          conversation_id="X", now=NOW)

    assert _get(for_agent, "file") == agent["file"]
    assert _get(for_owner, "file") == owner["file"]
    assert (tmp_path / agent["file"]).exists() and (tmp_path / owner["file"]).exists()


def test_lookup_for_another_agent_with_the_same_conversation_id_finds_neither(tmp_path):
    agent = _entry("dpc-kv-0a1b0001.bin", saved_at=NOW - 200, expires_at=NOW + HOUR,
                   conversation_id="X", agent_id="agent-A")
    owner = _entry("dpc-kv-0a1b0002.bin", saved_at=NOW - 100, expires_at=NOW + HOUR,
                   conversation_id="X", agent_id=None)
    _put(tmp_path, agent)
    _put(tmp_path, owner)

    assert kv.lookup(tmp_path, alias=ALIAS, agent_id="agent-B",
                     conversation_id="X", now=NOW) is None
    assert (tmp_path / agent["file"]).exists() and (tmp_path / owner["file"]).exists()


def test_a_new_entry_for_the_agent_replaces_only_the_agents_older_entry(tmp_path):
    agent_old = _entry("dpc-kv-0a1c0001.bin", saved_at=NOW - 300, expires_at=NOW + HOUR,
                       conversation_id="X", agent_id="agent-A")
    owner = _entry("dpc-kv-0a1c0002.bin", saved_at=NOW - 200, expires_at=NOW + HOUR,
                   conversation_id="X", agent_id=None)
    _put(tmp_path, agent_old)
    _put(tmp_path, owner)

    agent_new = _entry("dpc-kv-0a1c0003.bin", saved_at=NOW - 10, expires_at=NOW + HOUR,
                       conversation_id="X", agent_id="agent-A")
    _put(tmp_path, agent_new)

    for_agent = kv.lookup(tmp_path, alias=ALIAS, agent_id="agent-A",
                          conversation_id="X", now=NOW)
    for_owner = kv.lookup(tmp_path, alias=ALIAS, agent_id=None,
                          conversation_id="X", now=NOW)
    assert _get(for_agent, "file") == agent_new["file"]
    assert _get(for_owner, "file") == owner["file"]
    assert agent_old["file"] not in _index_text(tmp_path)
    assert (tmp_path / owner["file"]).exists()
    assert (tmp_path / agent_new["file"]).exists()


def test_a_new_entry_for_the_owners_chat_replaces_only_the_owners_older_entry(tmp_path):
    agent = _entry("dpc-kv-0a1d0001.bin", saved_at=NOW - 300, expires_at=NOW + HOUR,
                   conversation_id="X", agent_id="agent-A")
    owner_old = _entry("dpc-kv-0a1d0002.bin", saved_at=NOW - 200, expires_at=NOW + HOUR,
                       conversation_id="X", agent_id=None)
    _put(tmp_path, agent)
    _put(tmp_path, owner_old)

    owner_new = _entry("dpc-kv-0a1d0003.bin", saved_at=NOW - 10, expires_at=NOW + HOUR,
                       conversation_id="X", agent_id=None)
    _put(tmp_path, owner_new)

    for_agent = kv.lookup(tmp_path, alias=ALIAS, agent_id="agent-A",
                          conversation_id="X", now=NOW)
    for_owner = kv.lookup(tmp_path, alias=ALIAS, agent_id=None,
                          conversation_id="X", now=NOW)
    assert _get(for_agent, "file") == agent["file"]
    assert _get(for_owner, "file") == owner_new["file"]
    assert owner_old["file"] not in _index_text(tmp_path)
