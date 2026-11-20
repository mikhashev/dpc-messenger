"""A synced attachment names a file on the sender's disk; the copy is here.

Group history sync carries the sender's record of a pasted screenshot with the
sender's `file_path` (`/home/mike/.dpc/...`). The receiver holds the file
after the transfer, but the record kept the foreign path and the UI — whose
asset scope is the local `$HOME/.dpc/**` — could not render it.

`_remap_attachment_paths` existed for this and did nothing on that path: it
was called only from `import_history` (the 1:1 route), never from the merge
that group sync uses, and it looked in `conversations/{id}/files/` — the bare
id, where a named group's store is `{id}-{slug}`, and one level above
`files/screenshots/` where images actually land.

Observed 2026-09-03 on the Windows node of group-0a52389f2bb6.
"""

from pathlib import Path

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor

GROUP = "group-0a52389f2bb6"
ME = "dpc-node-" + "b" * 32
PEER = "dpc-node-" + "a" * 32
PARTICIPANTS = [
    {"node_id": PEER, "name": "Mike (linux)", "context": "peer"},
    {"node_id": ME, "name": "User", "context": "local"},
]
# The sender's disk under a root no machine has: the remapper keeps a `file_path`
# that exists, and the real `/home/mike/...` does exist on the Linux node.
FOREIGN = f"/nonexistent-node/home/mike/.dpc/conversations/{GROUP}-1234/files/screenshots/paste_1788443154187.png"


@pytest.fixture(autouse=True)
def _home_is_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))
    assert not Path(FOREIGN).exists(), "the foreign path must be foreign on this machine too"


def _monitor(display_name=None):
    return ConversationMonitor(
        conversation_id=GROUP, participants=PARTICIPANTS, llm_manager=None,
        display_name=display_name,
    )


def _attachment(**over):
    att = {"type": "image", "filename": "paste_1788443154187.png", "file_path": FOREIGN,
           "mime_type": "image/png", "size_bytes": 98889, "status": "completed"}
    att.update(over)
    return att


def _local_copy(monitor, subdir="files/screenshots"):
    folder = monitor._get_conversation_dir() / subdir
    folder.mkdir(parents=True, exist_ok=True)
    local = folder / "paste_1788443154187.png"
    local.write_bytes(b"png")
    return local


def test_a_foreign_path_is_rebased_onto_the_screenshot_folder_of_the_named_store():
    """The store in use is `{id}-{slug}`, and images live under screenshots/."""
    monitor = _monitor(display_name="work")
    local = _local_copy(monitor)
    assert local.parent.parent.parent.name == f"{GROUP}-work", "the fixture must build the slugged store"

    remapped = monitor._remap_attachment_paths([_attachment()])

    assert remapped[0]["file_path"] == str(local)
    assert remapped[0]["size_bytes"] == 98889, "everything but the path stays"


def test_a_plain_file_is_found_one_level_up():
    monitor = _monitor()
    local = _local_copy(monitor, subdir="files")

    remapped = monitor._remap_attachment_paths([_attachment(type="file")])

    assert remapped[0]["file_path"] == str(local)


def test_a_path_with_no_local_copy_is_left_as_it_came():
    """Not deleted: the UI falls back to the thumbnail, and the transfer that
    brings the file may still be on its way."""
    monitor = _monitor()

    remapped = monitor._remap_attachment_paths([_attachment()])

    assert remapped[0]["file_path"] == FOREIGN


def test_the_merge_path_itself_rebases_the_path():
    """Not the helper in isolation — group sync goes through merge_history."""
    monitor = _monitor()
    local = _local_copy(monitor)

    added = monitor.merge_history([{
        "id": "m-1", "role": "user", "content": "", "sender_node_id": PEER,
        "sender_name": "Mike (linux)", "timestamp": "2026-09-03T10:00:00+00:00",
        "attachments": [_attachment()],
    }])

    assert added == 1
    assert monitor.message_history[-1]["attachments"][0]["file_path"] == str(local)


def test_a_record_claiming_our_id_is_held_to_the_store_like_any_other():
    """It used to skip the remap and keep any path as it came: the id is
    whatever the exporter wrote, so a member could plant a path to any file
    on this disk and have it served (card A-SYNCED-ATTACHMENT-PATH-CAN-MAKE-
    THIS-NODE-SERVE-ANY-FILE-ON-ITS-DISK). Our own records never arrive by a
    merge: `add_message` writes them, and a merge skips an id already held."""
    monitor = _monitor()
    elsewhere = Path.home() / "somewhere" / "paste_1788443154187.png"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(b"a file on this disk, outside the store")
    local = _local_copy(monitor)

    monitor.add_message_with_id({
        "id": "mine", "role": "user", "content": "", "sender_node_id": ME,
        "attachments": [_attachment(file_path=str(elsewhere))],
    })

    assert monitor.message_history[-1]["attachments"][0]["file_path"] == str(local)


def test_a_path_inside_the_store_is_kept_as_it_came():
    monitor = _monitor()
    local = _local_copy(monitor)

    remapped = monitor._remap_attachment_paths([_attachment(file_path=str(local))])

    assert remapped[0]["file_path"] == str(local)


def test_a_file_on_this_disk_outside_the_store_is_not_kept_and_stays_requestable():
    """No copy in the store: the record points where the file would land here,
    which is no file, so the UI offers to request it and nothing serves it."""
    monitor = _monitor()
    outside = Path.home() / ".dpc" / "node.key"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"key")

    remapped = monitor._remap_attachment_paths([_attachment(type="file", filename="node.key",
                                                            file_path=str(outside))])

    landing = monitor._get_conversation_dir() / "files" / "node.key"
    assert remapped[0]["file_path"] == str(landing)
    assert not landing.exists()
