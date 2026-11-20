"""A synced record's `file_path` makes this node serve nothing outside the conversation's files.

Card A-SYNCED-ATTACHMENT-PATH-CAN-MAKE-THIS-NODE-SERVE-ANY-FILE-ON-ITS-DISK
(HIGH; confirmed independently by Zcode). A group member exported a record
whose attachment named `~/.dpc/node.key`; `_remap_attachment_paths` kept any
path that existed on this disk as "already ours" (and `add_message_with_id`
skipped even that for a record claiming this node's id), `file_is_here` checked
existence alone, and `_served_attachment` served `Path(att["file_path"])`. One
FILE_REQUEST later this node uploaded its private key.

Mike's call, 2026-11-20 (option A):

- arriving, a path is this node's only inside the conversation's own `files`
  folder, both sides resolved, on every import path, a record claiming this
  node's id included; any other path is looked up there by name;
- serving, the same check against the requesting conversation's folder, and a
  file whose SHA-256 is not the record's `hash` is `not_held`.

A file this node sent from outside its store (a one-to-one file sent from where
it lay) is therefore not served again; option B (a registry of the paths this
node wrote) is later.

Zcode's medium finding in the same review: a group FILE_OFFER checked only the
format of the group id, so an unknown group, or a sender outside its roster,
had a monitor and `conversations/<group>/files` created for it.

Ark's review, 2026-11-20: such an offer is answered (FILE_CANCEL `not_a_member`,
the word of group_access.py), not dropped, so its sender is not left waiting;
and a holder hashes an unchanged file once, not on every FILE_REQUEST.

Over the in-process network of test_a_group_file_crosses_a_full_mesh_once, with
the real FileTransferManager, FileRequests, offer handler and, on the holder,
the real ConversationMonitor.
"""

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from dpc_client_core import conversation_paths
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.managers.file_request import FILE_REQUEST_DECLINED, NOT_HELD_REASON
from dpc_client_core.service import CoreService
from tests.test_a_group_file_crosses_a_full_mesh_once import GROUP, _ids, _make_node
from tests.test_a_synced_record_without_its_file_can_be_requested_from_a_peer_online import (
    _outcome, _RequestNetwork,
)

KEY_BYTES = b"-----BEGIN PRIVATE KEY-----\nnot a real key, but this node's own\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    from dpc_client_core import service as service_mod
    home = tmp_path / "home"
    (home / ".dpc").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", home / ".dpc")
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))
    return home


@pytest.fixture
def node_key(home):
    key = home / ".dpc" / "node.key"
    key.write_bytes(KEY_BYTES)
    return key


def _holder_with_a_real_monitor(net, home, me, peers, members):
    """A node whose group monitor is the production ConversationMonitor under `home`."""
    node = _make_node(net, home.parent, me, peers, members)
    node.manager.storage_base_path = home / ".dpc" / "conversations"
    real = {}

    def monitor_for(key):
        if key not in real:
            participants = [{"node_id": m, "name": m[9:13],
                             "context": "local" if m == me else "peer"} for m in members]
            real[key] = ConversationMonitor(conversation_id=key, participants=participants,
                                            llm_manager=None)
        return real[key]

    node.service._get_or_create_conversation_monitor = monitor_for
    node.real = real
    return node


def _three(net, home):
    """A holds the file (real monitor), B is its author, C asks A for it; C reaches only A."""
    a_id, b_id, c_id = _ids(3)
    members = [a_id, b_id, c_id]
    a = _holder_with_a_real_monitor(net, home, a_id, [b_id, c_id], members)
    c = _make_node(net, home.parent, c_id, [a_id], members)
    return a, b_id, c


def _att(filename, data, path, **over):
    att = {"type": "file", "filename": filename, "size_bytes": len(data),
           "hash": hashlib.sha256(data).hexdigest(), "mime_type": "application/octet-stream",
           "status": "completed", "file_path": str(path)}
    att.update(over)
    return att


def _record(message_id, sender, att):
    return {"id": message_id, "role": "user", "content": "", "sender_node_id": sender,
            "sender_name": sender[9:13], "timestamp": "2026-11-20T10:00:00+00:00",
            "attachments": [att]}


def _requesters_copy(c, record):
    """C's synced record of the same file: the author's path, no file on C."""
    att = dict(record["attachments"][0], file_path="/nonexistent-node/somebody/" +
               record["attachments"][0]["filename"])
    c.service._get_or_create_conversation_monitor(GROUP).message_history.append(
        dict(record, attachments=[att]))


async def _ask(net, c, record):
    answer = await CoreService.request_attachment(c.service, GROUP, record["id"])
    assert answer["status"] == "requesting", answer
    return await _outcome(net, c, answer["request_id"])


def _files_dir(a):
    folder = a.service._get_or_create_conversation_monitor(GROUP)._get_conversation_dir() / "files"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _assert_not_served(net, c, final):
    assert final["state"] == "not_held", final
    declined = net.named(FILE_REQUEST_DECLINED, to=c.me)
    assert declined and all(d[3]["reason"] == NOT_HELD_REASON for d in declined), declined
    assert net.offers_to(c.me) == [], "no byte of the file left the holder"


# --- the hole, end to end ------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_synced_record_naming_the_nodes_key_is_not_served(home, node_key):
    net = _RequestNetwork()
    a, b_id, c = _three(net, home)
    monitor = a.service._get_or_create_conversation_monitor(GROUP)
    planted = _record("planted", b_id, _att("node.key", KEY_BYTES, node_key))

    assert monitor.merge_history([planted]) == 1
    stored = monitor.message_history[-1]["attachments"][0]
    assert Path(stored["file_path"]) != node_key, "the record does not keep a path to the key"
    assert not conversation_paths.is_file_inside(stored["file_path"], [home / ".dpc"]), stored

    _requesters_copy(c, planted)
    final = await _ask(net, c, planted)

    _assert_not_served(net, c, final)
    assert not net.errors, net.errors


@pytest.mark.asyncio
async def test_a_record_claiming_this_nodes_own_id_is_checked_as_well(home, node_key):
    net = _RequestNetwork()
    a, b_id, c = _three(net, home)
    monitor = a.service._get_or_create_conversation_monitor(GROUP)
    assert monitor._local_node_id() == a.me
    planted = _record("as-if-mine", a.me, _att("node.key", KEY_BYTES, node_key))

    assert monitor.merge_history([planted]) == 1
    stored = monitor.message_history[-1]["attachments"][0]
    assert Path(stored["file_path"]) != node_key, "this node's id buys no path past the check"

    _requesters_copy(c, planted)
    final = await _ask(net, c, planted)

    _assert_not_served(net, c, final)


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["author", "self"])
async def test_a_record_already_in_the_history_is_refused_when_served(home, node_key, sender):
    """A history written before the fix still holds such records: the holder checks too."""
    net = _RequestNetwork()
    a, b_id, c = _three(net, home)
    monitor = a.service._get_or_create_conversation_monitor(GROUP)
    planted = _record("old", b_id if sender == "author" else a.me,
                      _att("node.key", KEY_BYTES, node_key))
    monitor.message_history.append(planted)

    _requesters_copy(c, planted)
    final = await _ask(net, c, planted)

    _assert_not_served(net, c, final)


@pytest.mark.asyncio
async def test_a_received_file_in_the_conversations_folder_is_still_served(home):
    net = _RequestNetwork()
    a, b_id, c = _three(net, home)
    monitor = a.service._get_or_create_conversation_monitor(GROUP)
    data = b"%PDF the minutes of the meeting"
    received = _files_dir(a) / "minutes.pdf"
    received.write_bytes(data)
    # B's record as a sync hands it over: B's path, which A rebases onto its copy.
    record = _record("minutes", b_id, _att("minutes.pdf", data,
                                           "/nonexistent-node/b/.dpc/files/minutes.pdf"))
    assert monitor.merge_history([record]) == 1
    assert monitor.message_history[-1]["attachments"][0]["file_path"] == str(received)

    _requesters_copy(c, record)
    final = await _ask(net, c, record)

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    (offer,) = net.offers_to(c.me)
    assert offer[3]["filename"] == "minutes.pdf"
    got = c.monitors[GROUP].message_history[-1]["attachments"][0]["file_path"]
    assert Path(got).read_bytes() == data


@pytest.mark.asyncio
async def test_a_file_that_does_not_hash_to_its_record_is_not_served(home):
    net = _RequestNetwork()
    a, b_id, c = _three(net, home)
    monitor = a.service._get_or_create_conversation_monitor(GROUP)
    recorded = b"the bytes the author sent, 32 b."
    swapped = b"other bytes of the very same len"
    assert len(recorded) == len(swapped)
    held = _files_dir(a) / "report.bin"
    held.write_bytes(swapped)
    record = _record("report", b_id, _att("report.bin", recorded, held))
    monitor.message_history.append(record)

    _requesters_copy(c, record)
    final = await _ask(net, c, record)

    _assert_not_served(net, c, final)


def _symlink_or_skip(link: Path, target: Path):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:  # Windows without the privilege
        pytest.skip(f"symlinks are not available here: {e}")


@pytest.mark.asyncio
async def test_a_link_inside_the_folder_that_points_out_of_it_is_not_served(home, node_key):
    net = _RequestNetwork()
    a, b_id, c = _three(net, home)
    monitor = a.service._get_or_create_conversation_monitor(GROUP)
    link = _files_dir(a) / "holiday.jpg"
    _symlink_or_skip(link, node_key)
    record = _record("holiday", b_id, _att("holiday.jpg", KEY_BYTES, link))
    monitor.message_history.append(record)

    _requesters_copy(c, record)
    final = await _ask(net, c, record)

    _assert_not_served(net, c, final)
    # Arriving, the link is not taken for the file either.
    remapped = monitor._remap_attachment_paths([dict(record["attachments"][0])], record)
    assert remapped[0].get("file_path") != str(link)


def _folder_link_or_skip(link: Path, target: Path):
    """A link to a folder: a junction on Windows, which needs no privilege; a symlink elsewhere."""
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as e:
        pytest.skip(f"folder links are not available here: {e}")


@pytest.mark.asyncio
async def test_a_folder_link_inside_the_folder_that_points_out_of_it_is_not_served(home, node_key):
    """The same through a linked folder (a junction on Windows): `files/keys/node.key`."""
    net = _RequestNetwork()
    a, b_id, c = _three(net, home)
    monitor = a.service._get_or_create_conversation_monitor(GROUP)
    link = _files_dir(a) / "keys"
    _folder_link_or_skip(link, node_key.parent)
    through = link / "node.key"
    assert through.read_bytes() == KEY_BYTES, "the fixture must reach the key through the link"
    record = _record("keys", b_id, _att("node.key", KEY_BYTES, through))
    monitor.message_history.append(record)

    _requesters_copy(c, record)
    final = await _ask(net, c, record)

    _assert_not_served(net, c, final)
    assert monitor.merge_history([dict(record, id="keys-synced")]) == 1
    assert monitor.message_history[-1]["attachments"][0].get("file_path") != str(through)


# --- the import side on its own ------------------------------------------------------

def test_a_filename_that_climbs_out_of_the_folder_finds_nothing(home, node_key):
    """The name lookup is held to the folder as well: `../../../node.key` is no file of it."""
    monitor = ConversationMonitor(conversation_id=GROUP, participants=[], llm_manager=None)
    (monitor._get_conversation_dir() / "files").mkdir(parents=True)
    climb = os.path.relpath(node_key, monitor._get_conversation_dir() / "files")
    att = {"type": "file", "filename": climb, "size_bytes": len(KEY_BYTES),
           "file_path": "/nonexistent-node/x"}

    (remapped,) = monitor._remap_attachment_paths([att], {"id": "m", "sender_node_id": "x"})

    assert remapped["file_path"] == "/nonexistent-node/x", "left as it came: no file here"


def test_containment_is_by_folder_not_by_string_prefix(tmp_path):
    files = tmp_path / "group-x" / "files"
    files.mkdir(parents=True)
    sibling = tmp_path / "group-x" / "files-old"
    sibling.mkdir()
    (sibling / "a.txt").write_bytes(b"a")
    (files / "b.txt").write_bytes(b"b")
    (files / "sub").mkdir()
    (files / "sub" / "c.txt").write_bytes(b"c")

    assert conversation_paths.is_file_inside(files / "b.txt", [files])
    assert conversation_paths.is_file_inside(files / "sub" / "c.txt", [files])
    assert not conversation_paths.is_file_inside(sibling / "a.txt", [files])
    assert not conversation_paths.is_file_inside(files / ".." / "files-old" / "a.txt", [files])
    assert not conversation_paths.is_file_inside(files / "sub", [files]), "a folder is no file"
    assert not conversation_paths.is_file_inside(files / "missing.txt", [files])
    assert not conversation_paths.is_file_inside("", [files])
    assert not conversation_paths.is_file_inside(None, [files])
    assert not conversation_paths.is_file_inside("a\0b", [files])


# --- a group offer for a group this node does not hold ---------------------------------

FOREIGN_GROUP = "group-0000000000bb"


def _offer(group_id, sender):
    data = b"x" * 100
    return {"transfer_id": "t-1", "filename": "notes.txt", "size_bytes": len(data),
            "hash": hashlib.sha256(data).hexdigest(), "mime_type": "text/plain",
            "chunk_size": 64 * 1024, "group_id": group_id, "message_id": "m-1",
            "sender_node_id": sender}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown_group", "sender_not_a_member"])
async def test_a_group_offer_from_outside_the_roster_is_refused_and_creates_no_folder(tmp_path, case):
    net = _RequestNetwork()
    x_id, s_id, m_id = _ids(3)
    x = _make_node(net, tmp_path, x_id, [s_id], [x_id, m_id])
    _make_node(net, tmp_path, s_id, [x_id], [s_id])
    if case == "unknown_group":
        x.service.group_manager.get_group = lambda g: None
    # In the other case the group is held and its roster is [x, m]: s is not in it.

    await net.wire(s_id).send_message_to_peer(
        x_id, {"command": "FILE_OFFER", "payload": _offer(FOREIGN_GROUP, s_id)})
    await net.settle()

    assert not net.errors, net.errors
    assert x.monitors == {}, "no monitor was made for it"
    assert not any(p.name.startswith(FOREIGN_GROUP) for p in tmp_path.rglob("*")), \
        "no folder was made for it"
    answers = [f for f in net.frames if f[2] in ("FILE_ACCEPT", "FILE_CANCEL")]
    assert len(answers) == 1, answers
    (to, frm, command, payload) = answers[0]
    assert (to, frm, command) == (s_id, x_id, "FILE_CANCEL"), answers
    assert payload == {"transfer_id": "t-1", "reason": "not_a_member"},         "one word for both cases, as every group door answers"
    assert x.manager.active_transfers == {}
    assert x.api.events == []


@pytest.mark.asyncio
async def test_an_offer_answering_a_request_from_a_member_since_removed_ends_that_offer(tmp_path):
    """C asked S while S was a member; S left the roster before its offer came."""
    from dpc_client_core.managers.file_request import OpenRequest
    net = _RequestNetwork()
    x_id, s_id, m_id = _ids(3)
    x = _make_node(net, tmp_path, x_id, [s_id], [x_id, m_id])
    _make_node(net, tmp_path, s_id, [x_id], [s_id])
    offer = _offer(GROUP, s_id)
    request = OpenRequest(request_id="r-1", conversation_id=GROUP, group_id=GROUP,
                          message_id="m-1", filename=offer["filename"],
                          size_bytes=offer["size_bytes"], file_hash=offer["hash"],
                          candidates=[s_id])
    x.manager.file_requests._open[request.request_id] = request

    await net.wire(s_id).send_message_to_peer(
        x_id, {"command": "FILE_OFFER", "payload": dict(offer, request_id="r-1")})
    await net.settle()

    assert not net.errors, net.errors
    assert [(f[2], f[3]) for f in net.frames if f[0] == s_id] == [
        ("FILE_CANCEL", {"transfer_id": "t-1", "reason": "not_a_member"})]
    assert [(e["state"], e.get("reason")) for e in x.api.named("file_request_status")] == [
        ("failed", "not_a_member")]
    assert x.manager.active_transfers == {} and x.monitors == {}


@pytest.mark.asyncio
async def test_a_members_group_offer_is_still_taken(tmp_path):
    net = _RequestNetwork()
    x_id, s_id = _ids(2)
    x = _make_node(net, tmp_path, x_id, [s_id], [x_id, s_id])
    _make_node(net, tmp_path, s_id, [x_id], [x_id, s_id])

    await net.wire(s_id).send_message_to_peer(
        x_id, {"command": "FILE_OFFER", "payload": _offer(GROUP, s_id)})
    await net.settle()

    assert not net.errors, net.errors
    assert [f[2] for f in net.frames if f[0] == s_id] == ["FILE_ACCEPT"]
    assert "t-1" in x.manager.active_transfers
    # Ended, so its stall watch does not outlive the test.
    await net.wire(s_id).send_message_to_peer(x_id, {
        "command": "FILE_CANCEL", "payload": {"transfer_id": "t-1", "reason": "user_cancelled"}})
    await net.settle()
    assert x.manager.active_transfers == {}


# --- a holder hashes an unchanged file once --------------------------------------------

@pytest.mark.asyncio
async def test_a_holder_hashes_an_unchanged_file_once_and_a_changed_one_again(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from dpc_client_core.managers import file_transfer_manager as ftm
    from dpc_client_core.managers.file_request import FileRequests

    real = ftm.hash_file_in_one_pass
    hashed = []

    def spy(path, *args, **kw):
        hashed.append(Path(path).name)
        return real(path, *args, **kw)

    monkeypatch.setattr(ftm, "hash_file_in_one_pass", spy)
    requests = FileRequests(SimpleNamespace())
    data = b"the bytes the author sent, 32 b."
    held = tmp_path / "report.bin"
    held.write_bytes(data)
    att = _att("report.bin", data, held)

    assert await requests._bytes_are_the_records(att, held)
    assert await requests._bytes_are_the_records(att, held)
    assert hashed == ["report.bin"], "two requests for an unchanged file hash it once"

    # The same size, another mtime: read again, and found not to be the record's.
    held.write_bytes(b"other bytes of the very same len")
    st = held.stat()
    os.utime(held, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert not await requests._bytes_are_the_records(att, held)
    assert hashed == ["report.bin", "report.bin"], "a changed file is hashed again"

    # Another size: read again too.
    held.write_bytes(data + b"!")
    assert not await requests._bytes_are_the_records(att, held)
    assert len(hashed) == 3


@pytest.mark.asyncio
async def test_the_hash_cache_is_bounded(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from dpc_client_core.managers import file_request
    from dpc_client_core.managers.file_request import FileRequests

    monkeypatch.setattr(file_request, "HASH_CACHE_ENTRIES", 3)
    requests = FileRequests(SimpleNamespace())
    for i in range(5):
        data = f"file {i}".encode()
        path = tmp_path / f"f{i}.bin"
        path.write_bytes(data)
        assert await requests._bytes_are_the_records(_att(path.name, data, path), path)
    assert len(requests._hashes) == 3
    assert [Path(k[0]).name for k in requests._hashes] == ["f2.bin", "f3.bin", "f4.bin"]
