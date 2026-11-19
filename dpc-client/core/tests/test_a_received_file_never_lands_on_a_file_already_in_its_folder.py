"""A received file never lands on a file already in its folder.

The receive side finished every download with `os.replace(temp, destination)`,
and the destination was the sender's name in the conversation's `files/`
folder. That folder is shared: a group sender keeps its own copy there since
the local-first send (3837b3a5), and every earlier download of the name lives
there too. So an incoming file of a taken name replaced the file already
there, silently, and the older record went on naming the new bytes with the
old size. One-to-one downloads used the same destination, and images and
voice messages the same final move.

The file now goes onto a free name (`<stem>_N<suffix>`), claimed by an
exclusive create, so the filesystem, not a comparison of strings, decides that
a name is taken: on NTFS and on APFS by default `Song.mp3` and `song.mp3` are
one file. The record keeps the sender's name in `filename` (the author's record
is matched on it) and the path on disk in `file_path`; a relay offers the name
it was offered, not its own `_N`.

Card: FILE-NAME-COLLISION-OVERWRITES-SILENTLY.
"""

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.managers.file_transfer_manager import (
    FileTransfer,
    FileTransferManager,
    TransferStatus,
    claim_free_name,
)
from dpc_client_core.service import CoreService, keep_copy_under_a_free_name
from tests.test_a_group_file_crosses_a_full_mesh_once import (
    GROUP, _ids, _make_node, _Network, records, star,
)

PEER = "dpc-node-" + "b" * 32


class _P2P:
    node_id = "dpc-node-" + "a" * 32

    async def send_message_to_peer(self, node_id, message):
        pass


class _LocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))

    def attachments(self, event):
        return [p["attachments"][0] for n, p in self.events if n == event]


def _manager(tmp_path):
    api = _LocalApi()
    manager = FileTransferManager(
        p2p_manager=_P2P(), firewall=SimpleNamespace(rules={
            "image_transfer": {"save_screenshots_to_disk": True}}),
        settings=SimpleNamespace(get=lambda section, key, default=None: default),
        local_api=api, storage_base_path=tmp_path,
    )

    async def no_transcription(**kw):
        return None

    # A received voice message is handed to transcription; nothing else of a service is read.
    manager.service = SimpleNamespace(_maybe_transcribe_voice_message=no_transcription)
    return manager, api


async def _receive(manager, transfer_id, filename, data, mime_type="text/plain", **extra):
    transfer = FileTransfer(
        transfer_id=transfer_id, filename=filename, size_bytes=len(data),
        hash=hashlib.sha256(data).hexdigest(), mime_type=mime_type, chunk_size=len(data),
        node_id=PEER, direction="download", status=TransferStatus.TRANSFERRING,
        chunks_received=set(), total_chunks=1, **extra,
    )
    manager.active_transfers[transfer_id] = transfer
    await manager.handle_file_chunk(PEER, {
        "transfer_id": transfer_id, "chunk_index": 0, "data": base64.b64encode(data).decode(),
    })
    assert transfer.status == TransferStatus.COMPLETED, transfer.status
    return transfer


# --- one-to-one: the second file of a name is a second file -------------------

@pytest.mark.asyncio
async def test_a_second_file_of_one_name_is_a_second_file_with_its_own_record(tmp_path):
    manager, api = _manager(tmp_path)
    first, second = b"the first notes\n", b"other notes, longer than the first\n"

    await _receive(manager, "t-1", "notes.txt", first)
    await _receive(manager, "t-2", "notes.txt", second)

    old, new = api.attachments("new_p2p_message")
    assert Path(old["file_path"]) != Path(new["file_path"])
    assert Path(old["file_path"]).read_bytes() == first
    assert Path(new["file_path"]).read_bytes() == second
    for att, data in ((old, first), (new, second)):
        assert att["size_bytes"] == len(data) == Path(att["file_path"]).stat().st_size
        assert att["filename"] == "notes.txt", "the sender's name, which the author's record has"
    folder = Path(old["file_path"]).parent
    assert sorted(p.name for p in folder.iterdir()) == ["notes.txt", "notes_1.txt"]


@pytest.mark.asyncio
async def test_a_file_already_in_the_folder_keeps_its_bytes(tmp_path):
    manager, api = _manager(tmp_path)
    folder = manager._get_peer_storage_path(PEER)
    (folder / "report.pdf").write_bytes(b"%PDF mine")

    await _receive(manager, "t-1", "report.pdf", b"%PDF theirs, other bytes", "application/pdf")

    (att,) = api.attachments("new_p2p_message")
    assert (folder / "report.pdf").read_bytes() == b"%PDF mine"
    assert Path(att["file_path"]) == folder / "report_1.pdf"
    assert Path(att["file_path"]).read_bytes() == b"%PDF theirs, other bytes"


@pytest.mark.asyncio
async def test_two_names_that_differ_only_by_case_are_two_files_on_every_platform(tmp_path):
    # On NTFS and APFS (case-insensitive by default) `Song.mp3` takes `song.mp3`;
    # on ext4 they are two names anyway. Either way: two files, each its own bytes.
    manager, api = _manager(tmp_path)

    await _receive(manager, "t-1", "Song.mp3", b"ID3 upper", "audio/mpeg")
    await _receive(manager, "t-2", "song.mp3", b"ID3 lower, another take", "audio/mpeg")

    upper, lower = api.attachments("new_p2p_message")
    assert Path(upper["file_path"]).read_bytes() == b"ID3 upper"
    assert Path(lower["file_path"]).read_bytes() == b"ID3 lower, another take"
    assert len(list(Path(upper["file_path"]).parent.iterdir())) == 2


@pytest.mark.asyncio
async def test_a_directory_of_the_name_is_a_taken_name(tmp_path):
    # Windows answers PermissionError for a directory, POSIX FileExistsError.
    manager, api = _manager(tmp_path)
    folder = manager._get_peer_storage_path(PEER)
    (folder / "screenshots").mkdir(exist_ok=True)

    await _receive(manager, "t-1", "screenshots", b"a file with no extension")

    (att,) = api.attachments("new_p2p_message")
    assert Path(att["file_path"]) == folder / "screenshots_1"
    assert (folder / "screenshots").is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize("name, saved", [
    ('what?.txt', "what_.txt"),
    ('a<b>c|d*e"f.txt', "a_b_c_d_e_f.txt"),
    ("tab\there.txt", "tab_here.txt"),
    ("trailing dot.", "trailing dot"),
    ("trailing space.txt  ", "trailing space.txt"),
])
async def test_a_name_windows_refuses_is_saved_under_one_it_accepts_on_every_platform(
        tmp_path, name, saved):
    manager, api = _manager(tmp_path)

    await _receive(manager, "t-1", name, b"from a linux peer")

    (att,) = api.attachments("new_p2p_message")
    assert Path(att["file_path"]).name == saved
    assert Path(att["file_path"]).read_bytes() == b"from a linux peer"
    assert att["filename"] == name, "the record keeps the name as it was sent"


@pytest.mark.asyncio
async def test_an_image_and_a_voice_message_of_a_taken_name_are_second_files(tmp_path):
    manager, api = _manager(tmp_path)
    image = {"dimensions": {"width": 1, "height": 1}, "thumbnail_base64": ""}
    voice = {"duration_seconds": 1.0}

    for tid, data in (("i-1", b"\x89PNG one"), ("i-2", b"\x89PNG two!")):
        await _receive(manager, tid, "shot.png", data, "image/png", image_metadata=image)
    for tid, data in (("v-1", b"webm one"), ("v-2", b"webm two!")):
        await _receive(manager, tid, "voice.webm", data, "audio/webm", voice_metadata=voice)

    atts = api.attachments("new_p2p_message")
    paths = [Path(a["file_path"]) for a in atts]
    assert len(set(paths)) == 4
    assert [p.read_bytes() for p in paths] == [
        b"\x89PNG one", b"\x89PNG two!", b"webm one", b"webm two!"]


@pytest.mark.asyncio
@pytest.mark.parametrize("leftover", ["file", "directory"])
async def test_a_temporary_name_that_is_taken_is_left_alone_and_the_file_still_arrives(tmp_path, leftover):
    # The temporary name is built from the peer's filename and transfer id; a
    # file already there under it (left by a crash during the same transfer,
    # or a squatter) is not this transfer's to overwrite or remove. It used to
    # fail the receive with receive_error (Zcode in review, 2026-11-19); the
    # transfer now writes under a fresh temporary name beside it.
    manager, api = _manager(tmp_path)
    destination = manager._get_peer_storage_path(PEER) / "x.txt"
    squatter = manager._receive_path_for(destination, "t-1")
    if leftover == "file":
        squatter.write_bytes(b"not yours")
    else:
        squatter.mkdir()
    transfer = FileTransfer(
        transfer_id="t-1", filename="x.txt", size_bytes=3, hash="none", mime_type="text/plain",
        chunk_size=3, node_id=PEER, direction="download", status=TransferStatus.TRANSFERRING,
        chunks_received=set(), total_chunks=1,
    )
    manager.active_transfers["t-1"] = transfer

    await manager.handle_file_chunk(PEER, {
        "transfer_id": "t-1", "chunk_index": 0, "data": base64.b64encode(b"abc").decode()})

    if leftover == "file":
        assert squatter.read_bytes() == b"not yours"
    else:
        assert squatter.is_dir()
    assert transfer.status == TransferStatus.COMPLETED
    assert "t-1" not in manager.active_transfers
    assert destination.read_bytes() == b"abc"
    others = sorted(p.name for p in destination.parent.iterdir() if p not in (destination, squatter))
    assert others == [], f"the fresh temporary file was left behind: {others}"


@pytest.mark.asyncio
@pytest.mark.parametrize("name, saved", [
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("COM1.mp3", "_COM1.mp3"),
    ("LPT9", "_LPT9"),
    ("aux .log", "_aux .log"),
    ("COM¹.txt", "_COM¹.txt"),
    ("CONOUT$", "_CONOUT$"),
    ("console.txt", "console.txt"),
    ("COM10.txt", "COM10.txt"),
])
async def test_a_name_windows_reserves_for_a_device_is_received_as_a_file(tmp_path, name, saved):
    # Zcode in review, 2026-11-19, reported that such a name fails the receive;
    # it is prefixed with "_" on every platform, and the record keeps the name sent.
    manager, api = _manager(tmp_path)

    await _receive(manager, "t-1", name, b"a device name")

    (att,) = api.attachments("new_p2p_message")
    assert Path(att["file_path"]).name == saved
    assert Path(att["file_path"]).read_bytes() == b"a device name"
    assert att["filename"] == name


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["a" * 250 + ".mp3", "я" * 200 + ".txt", "b" * 300])
async def test_a_name_too_long_for_one_path_component_is_cut_and_the_file_still_arrives(tmp_path, name):
    # 255 bytes (ext4, APFS) or units (NTFS) per component, and the temporary
    # file adds its own suffix: such a name failed the receive on every platform.
    manager, api = _manager(tmp_path)

    await _receive(manager, "t-1", name, b"a long name")
    await _receive(manager, "t-2", name, b"the same long name again")

    first, second = api.attachments("new_p2p_message")
    for att, data in ((first, b"a long name"), (second, b"the same long name again")):
        saved = Path(att["file_path"]).name
        assert len(saved.encode("utf-8")) <= FileTransferManager.MAX_INCOMING_NAME_BYTES + 3  # `_1`
        assert Path(att["file_path"]).read_bytes() == data
        assert att["filename"] == name, "the record keeps the name as it was sent"
    assert Path(first["file_path"]).suffix == Path(name).suffix
    assert first["file_path"] != second["file_path"]


def test_a_claimed_name_is_an_empty_file_and_the_next_claim_moves_on(tmp_path):
    claimed = claim_free_name(tmp_path, "x.txt")
    assert claimed == tmp_path / "x.txt" and claimed.stat().st_size == 0
    assert claim_free_name(tmp_path, "x.txt") == tmp_path / "x_1.txt"


@pytest.mark.asyncio
async def test_a_failed_move_gives_back_the_name_it_claimed(tmp_path, monkeypatch):
    from dpc_client_core.managers import file_transfer_manager as ftm
    manager, api = _manager(tmp_path)
    folder = manager._get_peer_storage_path(PEER)
    (folder / "x.txt").write_bytes(b"mine")

    def refuse(src, dst):
        raise PermissionError(13, "held open elsewhere", str(dst))

    monkeypatch.setattr(ftm.os, "replace", refuse)
    transfer = FileTransfer(
        transfer_id="t-1", filename="x.txt", size_bytes=3, hash="none", mime_type="text/plain",
        chunk_size=3, node_id=PEER, direction="download", status=TransferStatus.TRANSFERRING,
        chunks_received=set(), total_chunks=1,
    )
    manager.active_transfers["t-1"] = transfer
    await manager.handle_file_chunk(PEER, {
        "transfer_id": "t-1", "chunk_index": 0, "data": base64.b64encode(b"abc").decode()})

    assert transfer.status == TransferStatus.FAILED
    assert sorted(p.name for p in folder.iterdir()) == ["x.txt"], "the claim or the temporary file was left"
    assert (folder / "x.txt").read_bytes() == b"mine"


# --- groups: the sender's kept copy and a member's file of the same name ------

@pytest.fixture
def root(tmp_path, monkeypatch):
    from dpc_client_core import service as service_mod
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path / "home")
    return tmp_path


def _picked(root, folder, name, data):
    f = root / folder / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return f


@pytest.mark.asyncio
async def test_a_group_sender_that_kept_a_name_and_receives_another_file_of_it_keeps_both(root):
    # A sends while B is offline to it, so its copy is kept and goes nowhere;
    # then B, which never got A's file, sends its own file of the same name.
    net = _Network()
    a_id, b_id = _ids(2)
    a = _make_node(net, root, a_id, [], [a_id, b_id])
    b = _make_node(net, root, b_id, [a_id], [a_id, b_id])
    a.service.firewall.rules["file_transfer"]["nodes"][b_id] = {"file_transfer.allow": "allow"}
    mine = _picked(root, "a-out", "notes.txt", b"A's notes")
    theirs = _picked(root, "b-out", "notes.txt", b"B's notes, which are longer")

    sent = await CoreService.send_group_file(a.service, GROUP, str(mine))
    await net.settle()
    await CoreService.send_group_file(b.service, GROUP, str(theirs))
    await net.settle()

    assert not net.errors, net.errors
    kept = Path(sent["file_path"])
    assert kept.read_bytes() == b"A's notes", "the sender's own copy was replaced"
    own, received = records(a)
    (own_att,) = own["attachments"]
    (got_att,) = received["attachments"]
    assert Path(own_att["file_path"]) == kept
    assert own_att["size_bytes"] == kept.stat().st_size
    assert Path(got_att["file_path"]) != kept
    assert Path(got_att["file_path"]).read_bytes() == theirs.read_bytes()
    assert got_att["size_bytes"] == theirs.stat().st_size
    assert received["sender_node_id"] == b.me
    shown = a.api.named("group_file_received")[-1]["attachments"][0]
    assert shown["file_path"] == got_att["file_path"]


@pytest.mark.asyncio
async def test_a_hub_that_holds_the_name_relays_the_file_under_the_name_it_was_offered(root):
    net = _Network()
    hub, author, leaf = star(net, root, leaves=2)
    hub_dir = root / hub.me / GROUP / "files"
    hub_dir.mkdir(parents=True, exist_ok=True)
    (hub_dir / "notes.txt").write_bytes(b"the hub's own notes")
    picked = _picked(root, "outbox", "notes.txt", b"the author's notes")

    await CoreService.send_group_file(author.service, GROUP, str(picked))
    await net.settle()

    assert not net.errors, net.errors
    assert (hub_dir / "notes.txt").read_bytes() == b"the hub's own notes"
    assert (hub_dir / "notes_1.txt").read_bytes() == b"the author's notes"
    (relay,) = net.offers_to(leaf.me)
    assert relay[1] == hub.me
    assert relay[3]["filename"] == "notes.txt", "the relay offered its own `_N` name"
    (rec,) = records(leaf)
    assert Path(rec["attachments"][0]["file_path"]).read_bytes() == b"the author's notes"


# --- the sender side shares the claim ------------------------------------------

def test_the_sender_keeps_a_copy_beside_a_directory_of_the_same_name(tmp_path):
    folder = tmp_path / "files"
    (folder / "screenshots").mkdir(parents=True)
    source = tmp_path / "out" / "screenshots"
    source.parent.mkdir()
    source.write_bytes(b"a file called screenshots")

    kept = keep_copy_under_a_free_name(source, folder, source.name)

    assert kept == folder / "screenshots_1"
    assert kept.read_bytes() == source.read_bytes()


def test_the_sender_keeps_a_case_variant_as_a_second_file(tmp_path):
    folder = tmp_path / "files"
    folder.mkdir()
    upper = tmp_path / "a" / "Song.mp3"
    lower = tmp_path / "b" / "song.mp3"
    for f, data in ((upper, b"upper"), (lower, b"lower take")):
        f.parent.mkdir()
        f.write_bytes(data)

    one = keep_copy_under_a_free_name(upper, folder, upper.name)
    two = keep_copy_under_a_free_name(lower, folder, lower.name)

    assert one.read_bytes() == b"upper"
    assert two.read_bytes() == b"lower take"
    assert len(list(folder.iterdir())) == 2
