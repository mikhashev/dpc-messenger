"""A conversation keeps one folder, whatever its display name does.

The defect these cover: the folder holding a group's history was derived from
the group's *name*, so the moment the name arrived the store moved and the 66
messages written before it stayed in a folder nothing wrote to again. A second
resolver on the service preferred the other shape, so with both folders on disk
two code paths read two different histories of the same conversation.

Cross-platform, because the naive repair breaks differently on each platform:
`Path.rename` onto an existing directory raises on Windows and can silently
succeed on POSIX when the target is empty, and Windows and macOS default to
case-insensitive filesystems while Linux does not.
"""

import json
from pathlib import Path

import pytest

from dpc_client_core import conversation_paths as cp
from dpc_client_core.conversation_monitor import (
    chain_hash_for,
    consolidate_conversation_stores,
    rechain,
)


GROUP = "group-970e5c7006a0"


def _store(base: Path, name: str, messages=None, extra_files=()):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    if messages is not None:
        (d / "history.json").write_text(
            json.dumps({"conversation_id": GROUP, "version": 1, "messages": messages}),
            encoding="utf-8",
        )
    for filename in extra_files:
        (d / filename).write_text(filename, encoding="utf-8")
    return d


def _msg(mid, ts, content, sender="Ark"):
    return {"id": mid, "role": "user", "sender_name": sender, "content": content, "timestamp": ts}


# --- the rule that removes the class ----------------------------------------

def test_an_existing_store_wins_over_the_preferred_name(tmp_path):
    """The load-bearing one: a display name must not move a live store."""
    _store(tmp_path, GROUP, [_msg("a", "2026-08-04T11:21:51+00:00", "old")])

    resolved = cp.resolve_store_dir(tmp_path, GROUP, "work")

    assert resolved.name == GROUP, (
        "the group's history is in the bare folder; naming it 'work' must not "
        "start a second store"
    )


def test_the_preferred_name_is_used_only_when_nothing_exists(tmp_path):
    assert cp.resolve_store_dir(tmp_path, GROUP, "work").name == f"{GROUP}-work"
    assert cp.resolve_store_dir(tmp_path, GROUP, None).name == GROUP
    assert not (tmp_path / GROUP).exists(), "resolving must not create anything"


def test_a_rename_of_the_group_does_not_move_the_store(tmp_path):
    """The store is chosen once; every later name resolves back to it."""
    _store(tmp_path, f"{GROUP}-work", [_msg("a", "2026-08-23T11:50:11+00:00", "hi")])

    for name in ("work", "Work Chat", "Работа", None, "something else"):
        assert cp.resolve_store_dir(tmp_path, GROUP, name).name == f"{GROUP}-work"


def test_both_resolvers_answer_with_the_same_folder(tmp_path):
    """The manager preferred the slug, the service preferred the bare id."""
    _store(tmp_path, GROUP, [_msg("a", "2026-08-04T11:21:51+00:00", "old")])
    _store(tmp_path, f"{GROUP}-work", [_msg("b", "2026-08-23T11:50:11+00:00", "new")])

    dirs = cp.existing_store_dirs(tmp_path, GROUP)
    service_answer = cp.canonical_store_dir(dirs)
    manager_answer = cp.resolve_store_dir(tmp_path, GROUP, "work")

    assert service_answer == manager_answer


# --- what counts as this conversation's folder ------------------------------

def test_duplicate_detection_ignores_case(tmp_path):
    """Windows and macOS fold case; Linux does not. Detection must not depend
    on which one is running, or a repair would fire on one platform only."""
    _store(tmp_path, f"{GROUP}-Work", [])

    found = cp.existing_store_dirs(tmp_path, GROUP)

    assert [d.name for d in found] == [f"{GROUP}-Work"]
    assert cp.is_store_dir_name(f"{GROUP}-WORK", GROUP)


def test_a_neighbouring_id_is_not_adopted(tmp_path):
    assert not cp.is_store_dir_name("group-970e5c7006a1", GROUP)
    assert not cp.is_store_dir_name(f"{GROUP}_history", GROUP)
    assert not cp.is_store_dir_name(f"{GROUP}-work.merged-20260824", GROUP)
    # Only a slug may follow the id, and a slug never holds an underscore.
    assert not cp.is_store_dir_name(f"{GROUP}-work_2", GROUP)


# --- consolidation ----------------------------------------------------------

def test_consolidation_recovers_the_orphaned_half(tmp_path):
    old = [_msg("a", "2026-08-04T11:21:51+00:00", "first"),
           _msg("b", "2026-08-11T09:30:34+00:00", "last of the old half")]
    new = [_msg("c", "2026-08-23T11:50:11+00:00", "first of the new half"),
           _msg("d", "2026-08-24T11:40:56+00:00", "newest")]
    _store(tmp_path, GROUP, old)
    _store(tmp_path, f"{GROUP}-work", new)

    summary = consolidate_conversation_stores(tmp_path, GROUP, "work")

    assert summary["messages_added"] == 2
    canonical = cp.resolve_store_dir(tmp_path, GROUP, "work")
    merged = json.loads((canonical / "history.json").read_text(encoding="utf-8"))["messages"]
    assert [m["id"] for m in merged] == ["a", "b", "c", "d"], "ordered by timestamp"


def test_consolidation_re_chains_so_the_loader_sees_no_break(tmp_path):
    _store(tmp_path, GROUP, [_msg("a", "2026-08-04T11:21:51+00:00", "old")])
    _store(tmp_path, f"{GROUP}-work", [_msg("c", "2026-08-23T11:50:11+00:00", "new")])

    consolidate_conversation_stores(tmp_path, GROUP, "work")

    canonical = cp.resolve_store_dir(tmp_path, GROUP, "work")
    merged = json.loads((canonical / "history.json").read_text(encoding="utf-8"))["messages"]
    prev = "genesis"
    for i, m in enumerate(merged):
        assert m["msg_index"] == i + 1
        assert m["chain_hash"] == chain_hash_for(m, prev)
        prev = m["chain_hash"]

    anchor = json.loads((canonical / ".chain_meta.json").read_text(encoding="utf-8"))
    assert anchor == {"msg_count": len(merged), "last_chain_hash": merged[-1]["chain_hash"]}


def test_consolidation_never_deletes_the_folder_it_emptied(tmp_path):
    _store(tmp_path, GROUP, [_msg("a", "2026-08-04T11:21:51+00:00", "old")])
    _store(tmp_path, f"{GROUP}-work", [_msg("c", "2026-08-23T11:50:11+00:00", "new")])

    consolidate_conversation_stores(tmp_path, GROUP, "work")

    retired = [d for d in tmp_path.iterdir() if cp.RETIRED_MARKER in d.name]
    assert len(retired) == 1, "the emptied folder is renamed, not removed"
    kept = json.loads((retired[0] / "history.json").read_text(encoding="utf-8"))["messages"]
    assert kept, "the folded-in folder keeps its own copy of the history"
    canonical = cp.resolve_store_dir(tmp_path, GROUP, "work")
    surviving = {m["id"] for m in json.loads(
        (canonical / "history.json").read_text(encoding="utf-8"))["messages"]}
    assert {"a", "c"} == surviving, "and both halves are in the store that remains"


def test_consolidation_moves_payload_but_never_overwrites(tmp_path):
    # The bare folder holds more history, so it is the one that survives.
    canonical = _store(tmp_path, GROUP, [_msg("a", "2026-08-04T11:21:51+00:00", "old")],
                       extra_files=("settings.json",))
    (canonical / "settings.json").write_text("canonical wins", encoding="utf-8")
    _store(tmp_path, f"{GROUP}-work", [], extra_files=("orphan-only.bin", "settings.json"))

    consolidate_conversation_stores(tmp_path, GROUP, "work")

    assert canonical == cp.resolve_store_dir(tmp_path, GROUP, "work")
    assert (canonical / "orphan-only.bin").exists()
    assert (canonical / "settings.json").read_text(encoding="utf-8") == "canonical wins"
    retired = [d for d in tmp_path.iterdir() if cp.RETIRED_MARKER in d.name][0]
    assert (retired / "settings.json").exists(), "the colliding file stays with the orphan"


def test_consolidation_is_idempotent(tmp_path):
    _store(tmp_path, GROUP, [_msg("a", "2026-08-04T11:21:51+00:00", "old")])
    _store(tmp_path, f"{GROUP}-work", [_msg("c", "2026-08-23T11:50:11+00:00", "new")])

    first = consolidate_conversation_stores(tmp_path, GROUP, "work")
    second = consolidate_conversation_stores(tmp_path, GROUP, "work")

    assert first["merged"] == 1
    assert second == {"merged": 0, "orphans": [], "messages_added": 0, "files_moved": 0}


def test_consolidation_does_nothing_when_there_is_one_store(tmp_path):
    _store(tmp_path, f"{GROUP}-work", [_msg("c", "2026-08-23T11:50:11+00:00", "new")])

    assert consolidate_conversation_stores(tmp_path, GROUP, "work")["merged"] == 0
    assert not [d for d in tmp_path.iterdir() if cp.RETIRED_MARKER in d.name]


def test_a_message_arriving_twice_is_not_stored_twice(tmp_path):
    shared = _msg("dup", "2026-08-10T10:00:00+00:00", "sent once, stored in both")
    _store(tmp_path, GROUP, [shared])
    _store(tmp_path, f"{GROUP}-work", [dict(shared)])

    consolidate_conversation_stores(tmp_path, GROUP, "work")

    canonical = cp.resolve_store_dir(tmp_path, GROUP, "work")
    merged = json.loads((canonical / "history.json").read_text(encoding="utf-8"))["messages"]
    assert [m["id"] for m in merged] == ["dup"]


# --- the cross-platform trap ------------------------------------------------

def test_retiring_never_renames_onto_an_existing_path(tmp_path):
    """`Path.rename` raises on Windows when the target exists and can silently
    succeed on POSIX when it is an empty directory — so a free name is found
    first, on every platform."""
    orphan = _store(tmp_path, GROUP, [])
    taken = tmp_path / f"{GROUP}{cp.RETIRED_MARKER}20260824"
    taken.mkdir()

    retired = cp.retire_orphan(orphan, "20260824")

    assert retired is not None and retired != taken
    assert taken.exists(), "the folder already retired under that name is untouched"
    assert not orphan.exists()


# --- one chain formula ------------------------------------------------------

def test_a_null_sender_name_does_not_read_as_a_broken_chain():
    """The add path rendered a missing sender as "" and the loader's check
    rendered a stored null as "None", so such a message verified as tampered on
    every load. One formula, one answer."""
    message = {"id": "x", "role": "user", "sender_name": None,
               "content": "hi", "timestamp": "2026-08-24T10:00:00+00:00"}
    absent = {"id": "x", "role": "user", "content": "hi",
              "timestamp": "2026-08-24T10:00:00+00:00"}
    rechain([message])
    rechain([absent])

    assert message["chain_hash"] == absent["chain_hash"]


def test_rechain_is_deterministic_and_starts_at_genesis():
    messages = [_msg("a", "2026-08-01T00:00:00+00:00", "one"),
                _msg("b", "2026-08-02T00:00:00+00:00", "two")]
    rechain(messages)
    first = [m["chain_hash"] for m in messages]
    rechain(messages)

    assert [m["chain_hash"] for m in messages] == first
    assert messages[0]["chain_hash"] == chain_hash_for(messages[0], "genesis")


# --- deleting a group means deleting all of it ------------------------------

def test_deleting_a_group_removes_every_store_it_has(tmp_path):
    """A split group must not leave half of itself behind for the next
    GROUP_CREATE to adopt — and a retired backup must survive the sweep,
    because that is the whole point of retiring rather than deleting."""
    from dpc_client_core.managers.group_manager import GroupManager, GroupMetadata

    home = tmp_path
    conversations = home / "conversations"
    _store(conversations, GROUP, [_msg("a", "2026-08-04T11:21:51+00:00", "old")])
    _store(conversations, f"{GROUP}-work", [_msg("c", "2026-08-23T11:50:11+00:00", "new")])
    backup = _store(conversations, f"{GROUP}-work{cp.RETIRED_MARKER}20260801", [])

    manager = GroupManager(home, "dpc-node-me")
    manager._groups[GROUP] = GroupMetadata(group_id=GROUP, name="work")
    manager._delete_group_file(GROUP)

    assert not (conversations / GROUP).exists()
    assert not (conversations / f"{GROUP}-work").exists()
    assert backup.exists(), "a retired backup is not a store and is kept"
