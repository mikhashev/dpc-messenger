"""A screenshot that is not saved to disk still shows its thumbnail, and no record names a deleted file.

With `image_transfer.save_screenshots_to_disk` off (card
SCREENSHOT-PREVIEWS-NEED-SAVE-TO-DISK-AND-SAVE-TO-DISK-IS-OFF-BY-DEFAULT) the
chat showed no preview. Followed through send and receive between in-process
nodes, the sender lost the screenshot three ways:

- 1:1 on Windows: FILE_COMPLETE came while the chunk loop still had the file
  open, the delete raised PermissionError, and the error took the sender's own
  message and history record with it;
- group: the first member's FILE_COMPLETE deleted the file, so a member whose
  FILE_ACCEPT came later got nothing; and the sender's record kept file_path to
  the deleted file (card A-SENT-SCREENSHOT-KEEPS-A-HISTORY-PATH-TO-A-FILE-THE-
  SENDER-DELETED-AFTER-UPLOAD);
- group with no member online: the file was never deleted at all.

Now the file is deleted when the last upload of it lets go, the records carry
the thumbnail and no path, and the receiving side is unchanged: it writes
nothing and shows the thumbnail.
"""

import asyncio
import base64
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from dpc_client_core import service as service_mod
from dpc_client_core.service import CoreService
from dpc_client_core.managers.file_transfer_manager import FileTransferManager
from dpc_client_core.message_handlers.file_offer_handler import FileOfferHandler
from dpc_client_core.message_handlers.file_complete_handler import FileCompleteHandler

A, B, C = ("dpc-node-" + ch * 32 for ch in "abc")
GROUP = "group-000000000001"


def _png_data_url() -> str:
    buf = io.BytesIO()
    # Noise, so the PNG spans several 64 KB chunks
    Image.frombytes("RGB", (300, 300), bytes((i * 7919) % 256 for i in range(300 * 300 * 3))).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


class _Monitor:
    def __init__(self, directory: Path):
        self.directory = directory
        self.records = []

    def _get_conversation_dir(self):
        return self.directory

    def add_message(self, role, content, attachments=None, **kw):
        self.records.append({"role": role, "content": content, "attachments": attachments or []})

    def save_history(self):
        pass

    def get_last_msg_index(self):
        return len(self.records)


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))

    def attachments(self, *names):
        return [a for n, p in self.events if n in names for a in p.get("attachments", [])]


class _Net:
    """Delivers every message as its own task, as a socket would; a held node queues."""

    def __init__(self):
        self.nodes = {}
        self.held = {}
        self.held_command = {}
        self.errors = []

    def hold(self, node_id, command=None):
        """Queue what reaches `node_id` (only `command`, when one is named)."""
        self.held.setdefault(node_id, [])
        self.held_command[node_id] = command

    def release(self, node_id):
        self.held_command.pop(node_id, None)
        for frm, msg in self.held.pop(node_id, []):
            self._deliver(node_id, frm, msg)

    def send(self, to, frm, msg):
        command = self.held_command.get(to)
        if to in self.held and command in (None, msg["command"]):
            self.held[to].append((frm, msg))
        else:
            self._deliver(to, frm, msg)

    def _deliver(self, to, frm, msg):
        asyncio.get_running_loop().create_task(self._dispatch(to, frm, msg))

    async def _dispatch(self, to, frm, msg):
        node = self.nodes[to]
        cmd, payload = msg["command"], msg["payload"]
        try:
            if cmd == "FILE_OFFER":
                await FileOfferHandler(node.service).handle(frm, payload)
            elif cmd == "FILE_ACCEPT":
                await node.manager.handle_file_accept(frm, payload["transfer_id"])
            elif cmd == "FILE_CHUNK":
                await node.manager.handle_file_chunk(frm, payload)
            elif cmd == "FILE_COMPLETE":
                await FileCompleteHandler(node.service).handle(frm, payload)
            elif cmd == "FILE_CANCEL":
                await node.manager.handle_file_cancel(frm, payload)
        except Exception as e:  # noqa: BLE001 — the assertion names it
            self.errors.append(f"{cmd} at {to[:12]}: {type(e).__name__}: {e}")


class _Wire:
    def __init__(self, net, me, peers):
        self.net, self.node_id = net, me
        self.peers = {p: object() for p in peers}

    def get_display_name(self):
        return self.node_id[:12]

    async def send_message_to_peer(self, to, msg):
        self.net.send(to, self.node_id, msg)


def _node(net, root: Path, me: str, peers, save: bool):
    api, wire = _Api(), _Wire(net, me, peers)
    firewall = SimpleNamespace(rules={
        "file_transfer": {"nodes": {p: {"file_transfer.allow": "allow"} for p in peers}},
        "image_transfer": {"save_screenshots_to_disk": save},
    })
    manager = FileTransferManager(
        p2p_manager=wire, firewall=firewall,
        settings=SimpleNamespace(get=lambda section, key, default=None: default),
        local_api=api, storage_base_path=root / me,
    )
    monitors = {}

    def monitor_for(key):
        if key not in monitors:
            (root / me / key).mkdir(parents=True, exist_ok=True)
            monitors[key] = _Monitor(root / me / key)
        return monitors[key]

    group = SimpleNamespace(members=[A, B, C])
    service = SimpleNamespace(
        file_transfer_manager=manager, p2p_manager=wire, peer_metadata={}, local_api=api,
        firewall=firewall, _get_or_create_conversation_monitor=monitor_for,
        _processed_message_ids=set(),
        group_manager=SimpleNamespace(get_group=lambda gid: group),
        p2p_coordinator=SimpleNamespace(get_connected_peers=lambda: list(wire.peers)),
        settings=SimpleNamespace(get=lambda *args: args[-1]),
    )
    manager.service = service
    node = SimpleNamespace(me=me, api=api, manager=manager, service=service, monitors=monitors)
    net.nodes[me] = node
    return node


@pytest.fixture
def mesh(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path / "home-a")

    def build(save: bool, online=(B, C)):
        net = _Net()
        nodes = {
            A: _node(net, tmp_path, A, list(online), save),
            B: _node(net, tmp_path, B, [A, C], save),
            C: _node(net, tmp_path, C, [A, B], save),
        }
        return net, nodes
    return build


async def _until(predicate, timeout_s=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _records(node):
    return [a for m in node.monitors.values() for r in m.records for a in r["attachments"]]


def _images_on_disk(root: Path):
    return [p for p in root.rglob("*.png")]


def _uploads(node):
    return [t for t in node.manager.active_transfers.values() if t.direction == "upload"]


@pytest.mark.asyncio
async def test_the_sender_of_a_one_to_one_screenshot_sees_its_thumbnail_and_keeps_no_file(mesh, tmp_path):
    net, n = mesh(save=False)
    # The last chunk's pause outlasts the receiver's answer, so FILE_COMPLETE
    # arrives while the chunk loop still holds the file open.
    n[A].manager.chunk_delay = 0.3

    result = await CoreService.send_p2p_image(n[A].service, B, _png_data_url(), "shot.png", "")
    sent_file = Path(result["file_path"])
    assert await _until(lambda: not _uploads(n[A]) and not sent_file.exists())

    assert net.errors == []
    shown = n[A].api.attachments("new_p2p_message")
    assert len(shown) == 1 and shown[0]["thumbnail"].startswith("data:image/jpeg;base64,")
    assert "file_path" not in shown[0]
    assert [("file_path" in a, bool(a["thumbnail"])) for a in _records(n[A])] == [(False, True)]


@pytest.mark.asyncio
async def test_the_receiver_of_a_screenshot_that_is_not_saved_shows_its_thumbnail_and_writes_nothing(mesh, tmp_path):
    net, n = mesh(save=False)

    await CoreService.send_p2p_image(n[A].service, B, _png_data_url(), "shot.png", "")
    assert await _until(lambda: n[B].api.attachments("new_p2p_message"))

    shown = n[B].api.attachments("new_p2p_message")[0]
    assert shown["thumbnail"].startswith("data:image/jpeg;base64,") and "file_path" not in shown
    assert [("file_path" in a, bool(a["thumbnail"])) for a in _records(n[B])] == [(False, True)]
    assert _images_on_disk(tmp_path / B) == []
    assert [e for e in net.errors if f"at {B[:12]}" in e] == []


@pytest.mark.asyncio
async def test_every_member_gets_a_group_screenshot_the_sender_does_not_keep(mesh, tmp_path):
    net, n = mesh(save=False)
    net.hold(C)  # C answers only after B's transfer has completed

    result = await CoreService.send_group_image(n[A].service, GROUP, _png_data_url(), "g.png", "")
    assert result["status"] == "success"
    assert await _until(lambda: n[B].api.attachments("group_file_received")
                        and len(_uploads(n[A])) == 1)
    net.release(C)
    assert await _until(lambda: n[C].api.attachments("group_file_received"))
    assert await _until(lambda: not _uploads(n[A]))

    assert net.errors == []
    for member in (B, C):
        shown = n[member].api.attachments("group_file_received")
        assert len(shown) == 1 and shown[0]["thumbnail"] and "file_path" not in shown[0]
    assert _images_on_disk(tmp_path / A) == []


@pytest.mark.asyncio
async def test_the_senders_group_record_names_no_file_it_will_delete(mesh, tmp_path):
    net, n = mesh(save=False)

    await CoreService.send_group_image(n[A].service, GROUP, _png_data_url(), "g.png", "")
    assert await _until(lambda: not _uploads(n[A]))

    for attachment in _records(n[A]) + n[A].api.attachments("group_file_received"):
        assert "file_path" not in attachment
        assert attachment["thumbnail"].startswith("data:image/jpeg;base64,")
    assert _images_on_disk(tmp_path / A) == []


@pytest.mark.asyncio
async def test_a_group_screenshot_offered_to_nobody_is_not_left_on_disk(mesh, tmp_path):
    net, n = mesh(save=False, online=())

    result = await CoreService.send_group_image(n[A].service, GROUP, _png_data_url(), "g.png", "")

    assert result["status"] == "success" and result["transfer_ids"] == []
    assert _images_on_disk(tmp_path / A) == []
    assert [bool(a["thumbnail"]) for a in _records(n[A])] == [True]


@pytest.mark.asyncio
async def test_a_completion_that_comes_after_the_upload_closed_the_file_deletes_it(mesh, tmp_path):
    net, n = mesh(save=False)
    net.hold(A, "FILE_COMPLETE")  # B's answer waits until the chunk loop has let go

    result = await CoreService.send_p2p_image(n[A].service, B, _png_data_url(), "shot.png", "")
    sent_file = Path(result["file_path"])
    assert await _until(lambda: n[B].api.attachments("new_p2p_message"))
    assert await _until(lambda: not n[A].manager._files_in_use)
    assert sent_file.exists()  # still offered: the upload has not ended
    net.release(A)

    assert await _until(lambda: not _uploads(n[A]))
    assert not sent_file.exists()
    assert net.errors == []


@pytest.mark.asyncio
async def test_a_declined_screenshot_is_not_left_on_the_senders_disk(mesh, tmp_path):
    net, n = mesh(save=False)
    n[B].service.firewall.rules["file_transfer"]["nodes"][A] = {"file_transfer.allow": "deny"}

    result = await CoreService.send_p2p_image(n[A].service, B, _png_data_url(), "shot.png", "")

    assert await _until(lambda: not _uploads(n[A]))
    assert not Path(result["file_path"]).exists()
    assert net.errors == []


@pytest.mark.asyncio
async def test_with_the_setting_on_both_sides_keep_the_file_and_name_it(mesh, tmp_path):
    net, n = mesh(save=True)

    result = await CoreService.send_p2p_image(n[A].service, B, _png_data_url(), "shot.png", "")
    assert await _until(lambda: not _uploads(n[A]) and n[B].api.attachments("new_p2p_message"))

    assert net.errors == []
    assert Path(result["file_path"]).exists()
    sent = n[A].api.attachments("new_p2p_message")[0]
    received = n[B].api.attachments("new_p2p_message")[0]
    assert Path(sent["file_path"]) == Path(result["file_path"])
    assert Path(received["file_path"]).exists()
