"""An id a peer chooses names no folder but the one it is.

Card PEER-CHOSEN-NODE-IDS-STILL-NAME-FOLDERS-OUTSIDE-THE-CERTIFICATE-STORE.
After `verify_signature` and the CERT_REQUEST read (2a930e66, 0f8a0d81), the
other places that build a path from an id that arrives on the wire:

- a group id in GROUP_CREATE (`apply_sync` on first sight): `dpc-node-<peer>`,
  `agent_001` or an upper-case copy of a held group's id named that
  conversation's folder, which GROUP_DELETE from the same peer then removed
  with `rmtree`; a relative or absolute id let the legacy clean-up unlink
  `<id>.json` outside `groups/`. Now only `group-` + 12 lowercase hex, the shape
  `create_group` writes (`is_valid_group_id`).
- a group id in FILE_OFFER: the file landed in the folder it named.
- the sender id of a Hub WebRTC signal: the one peer id no key proves, and it
  becomes the connection's id (`conversations/<node_id>/`).
- the 9998 file server's two path segments (local, but it answers any web page
  with `Access-Control-Allow-Origin: *`).

Nothing touches ~/.dpc: every store is under tmp_path.
"""

import io
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core import p2p_manager as p2p_mod
from dpc_client_core.file_server import FileServerHandler
from dpc_client_core.managers.file_transfer_manager import FileTransferManager
from dpc_client_core.managers.group_manager import GroupManager, GroupMetadata, is_valid_group_id
from dpc_client_core.message_handlers.file_offer_handler import FileOfferHandler
from dpc_client_core.message_handlers.group_handler import GroupCreateHandler
from dpc_client_core.p2p_manager import P2PManager

SELF = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
OTHER = "dpc-node-" + "c" * 32
GOOD_GROUP = "group-0123456789ab"


def _traversal_ids(base):
    return [
        "../providers",
        "group-../../providers",
        str(base / "outside" / "x"),          # absolute on the running OS
        "C:/Windows/x",
        "..\\providers",
    ]


# --- the validator ----------------------------------------------------------


def test_the_id_create_group_writes_is_a_group_id(tmp_path):
    manager = GroupManager(tmp_path / "dpc", SELF)
    assert is_valid_group_id(manager.create_group("g", "", []).group_id)
    assert is_valid_group_id(GOOD_GROUP)


@pytest.mark.parametrize("value", [
    "group-0123456789AB", "GROUP-0123456789ab",        # case folds on Windows and macOS
    "group-0123456789ab\n", "group-0123456789ab-slug",  # a slug folder's prefix
    "group-0123456789a", "group-0123456789abc",
    PEER, "agent_001", "local_ai", "telegram-1",
    "group-../../x", "../group-0123456789ab", "", None, 7, ["group-0123456789ab"],
])
def test_anything_else_is_not(value):
    assert not is_valid_group_id(value)


# --- GROUP_CREATE / apply_sync ----------------------------------------------


@pytest.fixture
def groups(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    dpc = tmp_path / "dpc"
    manager = GroupManager(dpc, SELF)
    service = SimpleNamespace(
        firewall=SimpleNamespace(is_blocked=lambda node_id: False),
        group_manager=manager,
        p2p_manager=SimpleNamespace(node_id=SELF, peers={}, send_message_to_peer=AsyncMock()),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        peer_metadata={},
        conversation_monitors={},
        clear_group_access_denied=lambda peer, gid: None,
    )
    return SimpleNamespace(dpc=dpc, manager=manager, service=service)


def _record(group_id):
    return GroupMetadata(group_id=group_id, name="g", created_by=PEER,
                         members=[PEER, SELF], version=1).to_dict()


def test_a_record_with_a_traversal_id_is_refused(groups, tmp_path):
    for group_id in _traversal_ids(tmp_path):
        assert groups.manager.apply_sync(_record(group_id), PEER) is None, group_id
        assert groups.manager.get_group(group_id) is None


def test_a_record_naming_another_conversations_folder_is_refused(groups):
    held = groups.manager.create_group("mine", "", [PEER])
    peer_chat = groups.dpc / "conversations" / OTHER
    peer_chat.mkdir(parents=True)
    (peer_chat / "history.json").write_text("{}", encoding="utf-8")

    for group_id in (OTHER, "agent_001", held.group_id.upper()):
        assert groups.manager.apply_sync(_record(group_id), PEER) is None, group_id

    assert (peer_chat / "history.json").is_file()
    assert groups.manager.get_group(held.group_id).created_by == SELF


def test_a_record_with_a_group_id_is_still_taken(groups):
    taken = groups.manager.apply_sync(_record(GOOD_GROUP), PEER)
    assert taken is not None and groups.manager.get_group(GOOD_GROUP) is not None


@pytest.mark.asyncio
async def test_group_create_refuses_a_traversal_or_foreign_id_and_answers_nothing(groups, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    for group_id in [*_traversal_ids(tmp_path), OTHER, GOOD_GROUP.upper()]:
        await GroupCreateHandler(groups.service).handle(PEER, _record(group_id))
        assert groups.manager.get_group(group_id) is None, group_id

    groups.service.p2p_manager.send_message_to_peer.assert_not_awaited()
    groups.service.local_api.broadcast_event.assert_not_awaited()
    assert any("is not a group id" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_group_create_with_a_group_id_is_still_an_invitation(groups):
    await GroupCreateHandler(groups.service).handle(PEER, _record(GOOD_GROUP))
    assert groups.manager.get_group(GOOD_GROUP) is not None


def test_a_held_group_with_an_unsafe_id_deletes_no_file_outside_groups(groups):
    # Held before this fix (an older node took it): the legacy clean-up names
    # `groups/<id>.json` and must not follow the id out of `groups/`.
    # `groups/` exists, as on any node that ever deleted a group: POSIX only
    # walks `..` through a directory that is there; Windows collapses it.
    (groups.dpc / "groups").mkdir(parents=True, exist_ok=True)
    victim = groups.dpc / "providers.json"
    victim.write_text("{}", encoding="utf-8")
    group_id = "../providers"
    groups.manager._groups[group_id] = GroupMetadata.from_dict(_record(group_id))

    groups.manager.handle_group_deleted(group_id)

    assert victim.is_file()


# --- FILE_OFFER --------------------------------------------------------------


class _Wire:
    def __init__(self):
        self.node_id = SELF
        self.sent = []
        self.peers = {}

    async def send_message_to_peer(self, node_id, message):
        self.sent.append(message)


def _offer_node(tmp_path):
    wire = _Wire()
    api = SimpleNamespace(broadcast_event=AsyncMock())
    manager = FileTransferManager(
        p2p_manager=wire,
        firewall=SimpleNamespace(rules={"file_transfer": {"nodes": {PEER: {"file_transfer.allow": "allow"}}}}),
        settings=SimpleNamespace(get=lambda section, key, default=None: "0" if key == "chunk_delay" else default),
        local_api=api,
        storage_base_path=tmp_path,
    )
    service = SimpleNamespace(file_transfer_manager=manager, p2p_manager=wire,
                              peer_metadata={}, local_api=api)
    return manager, wire, FileOfferHandler(service)


def _offer(transfer_id, group_id):
    return {"transfer_id": transfer_id, "filename": "f.bin", "size_bytes": 10, "hash": "sha256:x",
            "chunk_size": 1024, "mime_type": "application/octet-stream",
            "group_id": group_id, "message_id": "m-1", "sender_node_id": PEER}


@pytest.mark.asyncio
async def test_a_group_offer_with_a_traversal_or_foreign_id_is_ignored(tmp_path):
    manager, wire, handler = _offer_node(tmp_path)
    ids = [*_traversal_ids(tmp_path), OTHER, "agent_001", GOOD_GROUP.upper()]
    for n, group_id in enumerate(ids):
        await handler.handle(PEER, _offer(f"t{n}", group_id))

    assert manager.active_transfers == {}
    assert wire.sent == []
    assert not (tmp_path / "conversations" / OTHER).exists()


@pytest.mark.asyncio
async def test_a_group_offer_with_a_group_id_is_still_taken(tmp_path):
    manager, wire, handler = _offer_node(tmp_path)
    await handler.handle(PEER, _offer("ok", GOOD_GROUP))
    assert manager.active_transfers["ok"].group_id == GOOD_GROUP


# --- Hub WebRTC signal -------------------------------------------------------


@pytest.fixture
def p2p(monkeypatch):
    m = P2PManager.__new__(P2PManager)
    m.node_id = SELF
    m._handle_webrtc_offer = AsyncMock()
    m._handle_webrtc_answer = AsyncMock()
    m._handle_ice_candidate = AsyncMock()
    return m


def _signal(sender, kind="offer"):
    return {"type": "signal", "sender_node_id": sender, "payload": {"type": kind, "sdp": "v=0"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", [
    "../../x", "dpc-node-" + "b" * 32 + "/../" + "dpc-node-" + "c" * 32,
    "/etc/x", "C:/x", ("dpc-node-" + "b" * 32).upper(), "dpc-node-alice",
])
@pytest.mark.parametrize("kind", ["offer", "answer", "ice-candidate"])
async def test_a_hub_signal_from_an_id_that_is_not_a_key_hash_is_ignored(p2p, sender, kind):
    await p2p.handle_incoming_signal(_signal(sender, kind), SimpleNamespace())

    p2p._handle_webrtc_offer.assert_not_awaited()
    p2p._handle_webrtc_answer.assert_not_awaited()
    p2p._handle_ice_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_hub_signal_from_a_node_id_is_still_handled(p2p):
    hub = SimpleNamespace()
    await p2p.handle_incoming_signal(_signal(PEER), hub)
    p2p._handle_webrtc_offer.assert_awaited_once()
    assert p2p._handle_webrtc_offer.await_args.args[0] == PEER


# --- the 9998 file server ----------------------------------------------------


def _get(home, path):
    handler = FileServerHandler.__new__(FileServerHandler)
    handler.dpc_home_dir = home
    handler.path = path
    handler.wfile = io.BytesIO()
    answer = {}
    handler.send_error = lambda code, msg=None: answer.setdefault("code", code)
    handler.send_response = lambda code: answer.setdefault("code", code)
    handler.send_header = lambda *a: None
    handler.end_headers = lambda: None
    handler.do_GET()
    return answer["code"], handler.wfile.getvalue()


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "dpc"
    files = home / "conversations" / PEER / "files"
    files.mkdir(parents=True)
    (files / "voice.webm").write_bytes(b"voice")
    (home / "conversations" / PEER / "history.json").write_text("secret", encoding="utf-8")
    (home / "conversations_old" / "files").mkdir(parents=True)
    (home / "conversations_old" / "files" / "x").write_text("old", encoding="utf-8")
    (home / "node.key").write_text("key", encoding="utf-8")
    return home


@pytest.mark.parametrize("path", [
    f"/files/{PEER}/..%5Chistory.json",          # ..\history.json
    f"/files/{PEER}/..",
    "/files/..%5Cconversations_old/x",            # ..\conversations_old
    "/files/../x",
    f"/files/{PEER}/C:%5Cnode.key",
    f"/files/{PEER}/voice.webm%00",
    f"/files/{PEER}/voice.webm.",
])
def test_the_file_server_refuses_a_segment_that_is_a_path(home, path):
    code, body = _get(home, path)
    assert code in (403, 404) and body == b""


def test_the_file_server_still_serves_a_received_file(home):
    code, body = _get(home, f"/files/{PEER}/voice.webm")
    assert code == 200 and body == b"voice"
