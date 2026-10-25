"""One group file is one copy and one record on every member, however they are connected.

Every member that finishes receiving a group file relays it, because in a star
(the live stand: every link goes through one node) the other members cannot
reach its author. Until 2026-10-25 the only brake was the relayed transfer's
own id, and every relay is a new transfer: in a group of three or more members
all connected to each other one file went round for as long as everyone stayed
online — about 485 relays per node in 10 s on the harness, a record per relayer
in every history, a growing unread badge.

The brake now is the file's identity, the words GROUP_TEXT already uses: the
author's `sender_node_id` and `message_id`, carried by the offer and copied by
every relay. A member offered a file it already has, is receiving, or wrote
answers FILE_CANCEL `already_received`; a relay never goes back to the author;
an offer from a peer that predates the fields is judged by group and hash.

Real FileTransferManager, FileOfferHandler, FileCompleteHandler,
FileCancelHandler and CoreService group send paths, over an in-process network.
"""

import asyncio
import base64
import io
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from dpc_client_core import service as service_mod
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.service import CoreService
from dpc_client_core.managers.file_transfer_manager import (
    ALREADY_RECEIVED_REASON,
    FileTransferManager,
)
from dpc_client_core.message_handlers.file_cancel_handler import FileCancelHandler
from dpc_client_core.message_handlers.file_complete_handler import FileCompleteHandler
from dpc_client_core.message_handlers.file_offer_handler import FileOfferHandler

GROUP = "group-0000000000aa"


class _Monitor:
    """The group monitor's file-record surface, borrowed from the real class."""

    find_file_record = ConversationMonitor.find_file_record
    attach_received_file = ConversationMonitor.attach_received_file

    def __init__(self, directory: Path):
        self.directory = directory
        self.message_history = []
        self._history_dirty = False

    def _get_conversation_dir(self):
        return self.directory

    def add_message(self, role, content, attachments=None, **kw):
        self.message_history.append({
            "id": kw.get("message_id"), "role": role, "content": content,
            "sender_node_id": kw.get("sender_node_id"), "attachments": attachments,
        })

    def note_local_file_record(self, message_id):
        pass

    def save_history(self):
        pass

    def get_last_msg_index(self):
        return len(self.message_history)


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))

    def named(self, name):
        return [p for n, p in self.events if n == name]


class _Network:
    """Delivers each frame as its own task, like a socket; counts what crossed."""

    def __init__(self):
        self.nodes = {}
        self.in_flight = 0
        self.frames = []  # (to, from, command, payload)
        self.strip_identity_from = set()  # nodes that speak like a peer from before the fields
        self.errors = []

    def wire(self, me):
        net = self

        class _Wire:
            node_id = me
            peers = {}

            def get_display_name(self):
                return me[9:13]

            async def send_message_to_peer(self, to, message):
                if me in net.strip_identity_from and message["command"] == "FILE_OFFER":
                    message = dict(message, payload={
                        k: v for k, v in message["payload"].items()
                        if k not in ("message_id", "sender_node_id")
                    })
                net.in_flight += 1
                asyncio.get_running_loop().create_task(net._deliver(to, me, message))

        return _Wire()

    async def _deliver(self, to, frm, message):
        try:
            await asyncio.sleep(0)
            node = self.nodes[to]
            command, payload = message["command"], message["payload"]
            self.frames.append((to, frm, command, payload))
            if command == "FILE_OFFER":
                await FileOfferHandler(node.service).handle(frm, payload)
            elif command == "FILE_ACCEPT":
                await node.manager.handle_file_accept(frm, payload["transfer_id"])
            elif command == "FILE_CHUNK":
                await node.manager.handle_file_chunk(frm, payload)
            elif command == "FILE_COMPLETE":
                await FileCompleteHandler(node.service).handle(frm, payload)
            elif command == "FILE_CANCEL":
                await FileCancelHandler(node.service).handle(frm, payload)
        except Exception as e:  # noqa: BLE001 - reported by the test
            self.errors.append(f"{message['command']} to {to[9:10]}: {type(e).__name__}: {e}")
        finally:
            self.in_flight -= 1

    async def settle(self, quiet_s: float = 0.2, limit_s: float = 10.0):
        """Wait until nothing has crossed the network for `quiet_s`."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit_s
        seen, quiet_since = -1, loop.time()
        while loop.time() < deadline:
            await asyncio.sleep(0.02)
            if self.in_flight or len(self.frames) != seen:
                seen, quiet_since = len(self.frames), loop.time()
            elif loop.time() - quiet_since >= quiet_s:
                return
        raise AssertionError(f"the network never went quiet: {len(self.frames)} frames")

    def offers_to(self, node_id):
        return [f for f in self.frames if f[0] == node_id and f[2] == "FILE_OFFER"]

    def cancels(self, reason=None):
        return [f for f in self.frames if f[2] == "FILE_CANCEL"
                and (reason is None or f[3].get("reason") == reason)]


def _make_node(net, root, me, peers, members):
    api, wire = _Api(), net.wire(me)
    wire.peers = {p: object() for p in peers}
    firewall = SimpleNamespace(rules={
        "file_transfer": {"nodes": {p: {"file_transfer.allow": "allow"} for p in peers}},
        "image_transfer": {"save_screenshots_to_disk": True},
    }, _get_groups_for_node=lambda node_id: [])  # in no node group: a sender without an entry is asked
    manager = FileTransferManager(
        p2p_manager=wire, firewall=firewall,
        settings=SimpleNamespace(get=lambda s, k, d=None: d),
        local_api=api, storage_base_path=root / me,
    )
    monitors = {}

    def monitor_for(key):
        if key not in monitors:
            d = root / me / key
            d.mkdir(parents=True, exist_ok=True)
            monitors[key] = _Monitor(d)
        return monitors[key]

    async def nothing(**kw):
        return None

    group = SimpleNamespace(members=list(members))
    service = SimpleNamespace(
        file_transfer_manager=manager, p2p_manager=wire, peer_metadata={}, local_api=api,
        firewall=firewall, _get_or_create_conversation_monitor=monitor_for,
        _processed_message_ids=set(), _max_processed_ids=10000,
        group_manager=SimpleNamespace(get_group=lambda g: group),
        p2p_coordinator=SimpleNamespace(get_connected_peers=lambda: list(peers)),
        settings=SimpleNamespace(get=lambda *a: a[-1]),
        _maybe_transcribe_voice_message=nothing,
        _handle_group_agent_mentions=nothing,
    )
    manager.service = service
    node = SimpleNamespace(me=me, api=api, manager=manager, service=service, monitors=monitors,
                           root=root / me)
    net.nodes[me] = node
    return node


def _ids(n):
    return ["dpc-node-" + ch * 32 for ch in "abcdefgh"[:n]]


def full_mesh(net, root, n):
    ids = _ids(n)
    return [_make_node(net, root, me, [p for p in ids if p != me], ids) for me in ids]


def star(net, root, leaves):
    """The first node is the hub; the leaves reach only the hub."""
    ids = _ids(leaves + 1)
    hub = _make_node(net, root, ids[0], ids[1:], ids)
    return [hub] + [_make_node(net, root, me, [ids[0]], ids) for me in ids[1:]]


def _png_data_url():
    buf = io.BytesIO()
    Image.new("RGB", (16, 8), (10, 120, 200)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


async def send(node, kind, root):
    if kind == "file":
        f = root / "outbox" / "notes.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * 1000)
        result = await CoreService.send_group_file(node.service, GROUP, str(f))
    elif kind == "image":
        result = await CoreService.send_group_image(node.service, GROUP, _png_data_url(), "shot.png", "")
    else:
        result = await CoreService.send_group_voice_message(
            node.service, GROUP, base64.b64encode(b"\x1a\x45\xdf\xa3" + b"v" * 2000).decode(), 1.5)
    assert result["status"] == "success", result
    return result


def records(node):
    monitor = node.monitors.get(GROUP)
    return monitor.message_history if monitor else []


def received_files(node):
    """Files the receive side placed in the group folder (the sender's own copy lives elsewhere)."""
    folder = node.root / GROUP
    return sorted(p.name for p in folder.rglob("*") if p.is_file()) if folder.exists() else []


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path / "home")
    return tmp_path


def _the_member_holds_one_copy_and_one_record(node, author):
    assert len(received_files(node)) == 1, received_files(node)
    recs = records(node)
    assert len(recs) == 1, recs
    assert recs[0]["sender_node_id"] == author.me
    shown = node.api.named("group_file_received")
    assert len(shown) == 1, shown
    assert not node.manager.active_transfers, "nothing left open"


@pytest.mark.asyncio
@pytest.mark.parametrize("members", [3, 4])
@pytest.mark.parametrize("kind", ["file", "image", "voice"])
async def test_a_full_mesh_ends_with_one_copy_and_one_record_per_member(root, members, kind):
    net = _Network()
    nodes = full_mesh(net, root, members)
    author, others = nodes[0], nodes[1:]

    await send(author, kind, root)
    await net.settle()

    assert not net.errors, net.errors
    for node in others:
        _the_member_holds_one_copy_and_one_record(node, author)
    assert len(records(author)) == 1, "the author's own record, once"
    assert not author.manager.active_transfers
    # Each member offers the file once to each member but itself and its
    # author, and each such offer is refused: the network then falls silent.
    relays = (members - 1) * (members - 2)
    assert len([f for f in net.frames if f[2] == "FILE_OFFER"]) == (members - 1) + relays
    assert len(net.cancels(ALREADY_RECEIVED_REASON)) == relays
    before = len(net.frames)
    await asyncio.sleep(0.3)
    assert len(net.frames) == before, "no further offers"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "image", "voice"])
async def test_a_star_still_delivers_to_a_leaf_the_author_cannot_reach(root, kind):
    net = _Network()
    hub, author, other_leaf = star(net, root, leaves=2)

    await send(author, kind, root)
    await net.settle()

    assert not net.errors, net.errors
    for node in (hub, other_leaf):
        assert len(received_files(node)) == 1, (node.me, received_files(node))
        assert len(records(node)) == 1, (node.me, records(node))
    assert len(net.offers_to(other_leaf.me)) == 1, "the hub's relay"
    assert not net.cancels()


@pytest.mark.asyncio
async def test_a_hub_that_writes_the_file_reaches_every_leaf_and_no_leaf_relays(root):
    net = _Network()
    hub, *leaves = star(net, root, leaves=3)

    await send(hub, "image", root)
    await net.settle()

    for leaf in leaves:
        assert len(received_files(leaf)) == 1
        assert len(records(leaf)) == 1
    assert net.offers_to(hub.me) == [], "a leaf never offers the file back to its author"


@pytest.mark.asyncio
async def test_a_relay_never_goes_back_to_its_author(root):
    net = _Network()
    hub, author, other_leaf = star(net, root, leaves=2)

    await send(author, "file", root)
    await net.settle()

    assert net.offers_to(author.me) == []
    relayed = net.offers_to(other_leaf.me)[0][3]
    assert relayed["sender_node_id"] == author.me, "the relay keeps the author"
    assert relayed["message_id"] == net.offers_to(hub.me)[0][3]["message_id"]


@pytest.mark.asyncio
async def test_a_mesh_member_never_offers_the_file_to_its_author(root):
    net = _Network()
    author, *others = full_mesh(net, root, 4)

    await send(author, "voice", root)
    await net.settle()

    assert net.offers_to(author.me) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "image"])
async def test_the_same_file_sent_twice_is_two_messages_and_both_arrive(root, kind):
    """The identity is the message, not the bytes: a second send is not a copy of the first."""
    net = _Network()
    author, b, c = full_mesh(net, root, 3)

    await send(author, kind, root)
    await net.settle()
    await asyncio.sleep(0.01)  # a later millisecond, as any second send is
    await send(author, kind, root)
    await net.settle()

    for node in (b, c):
        assert len(records(node)) == 2, records(node)
        assert len({r["id"] for r in records(node)}) == 2


@pytest.mark.asyncio
async def test_a_member_that_got_the_file_through_a_relay_does_not_offer_it_to_its_author(root):
    """C is connected to the author, but the author's fan-out did not see C yet.

    C gets the file from B, so B is the hop; only the author's name in the
    offer keeps C from offering the file back to the node that wrote it.
    """
    net = _Network()
    ids = _ids(3)
    a, b, c = ids
    author = _make_node(net, root, a, [b], ids)  # the author's fan-out reaches only B
    _make_node(net, root, b, [a, c], ids)
    _make_node(net, root, c, [a, b], ids)  # C has a link to the author all the same

    await send(author, "file", root)
    await net.settle()

    assert len(net.offers_to(c)) == 1, "B's relay"
    assert net.offers_to(a) == []


async def _offer_again(net, to_node, frm, offer, **changes):
    """Hand `to_node` the same offer once more, under a new transfer id."""
    payload = {k: v for k, v in offer.items() if k not in ("message_id", "sender_node_id")}
    payload.update(changes, transfer_id="t-again")
    net.in_flight += 1
    await net._deliver(to_node.me, frm, {"command": "FILE_OFFER", "payload": payload})
    await net.settle()
    return [f for f in net.cancels(ALREADY_RECEIVED_REASON) if f[3]["transfer_id"] == "t-again"]


@pytest.mark.asyncio
async def test_the_author_refuses_its_own_file_relayed_back_without_the_fields(root):
    """An older relay drops the fields and may offer the file to anyone, its author too."""
    net = _Network()
    author, b = full_mesh(net, root, 2)
    await send(author, "image", root)
    await net.settle()
    offer = net.offers_to(b.me)[0][3]

    refused = await _offer_again(net, author, b.me, offer)

    assert len(refused) == 1, "known by its hash, which the author noted as it sent"
    assert len(records(author)) == 1


@pytest.mark.asyncio
async def test_a_node_refuses_an_offer_that_names_it_as_the_author(root):
    """Even with nothing remembered, as after a restart: it wrote that file."""
    net = _Network()
    author, b = full_mesh(net, root, 2)
    await send(author, "file", root)
    await net.settle()
    offer = net.offers_to(b.me)[0][3]
    author.manager._group_files_held.clear()

    refused = await _offer_again(net, author, b.me, offer,
                                 message_id="not-remembered", sender_node_id=author.me)

    assert len(refused) == 1


async def _relay_of_a_held_file(net, root):
    """B holds the author's file; returns (b, c, the offer C would relay to B)."""
    author, b, c = full_mesh(net, root, 3)
    await send(author, "file", root)
    await net.settle()
    offer = dict(net.offers_to(b.me)[0][3], transfer_id="t-from-c")
    assert b.manager.holds_group_file(GROUP, offer["message_id"], offer["sender_node_id"], offer["hash"])
    return b, c, offer


async def _deliver_offer(net, to_node, frm, offer):
    net.in_flight += 1
    await net._deliver(to_node.me, frm, {"command": "FILE_OFFER", "payload": offer})
    await net.settle()
    return [f[3] for f in net.cancels() if f[3]["transfer_id"] == offer["transfer_id"]]


@pytest.mark.asyncio
async def test_a_peer_the_firewall_refuses_is_told_firewall_denied_never_whether_the_file_is_here(root):
    """`already_received` answered first would tell a refused peer what this node holds."""
    net = _Network()
    b, c, offer = await _relay_of_a_held_file(net, root)
    b.service.firewall.rules["file_transfer"]["nodes"][c.me] = {"file_transfer.allow": "deny"}

    answers = await _deliver_offer(net, b, c.me, offer)

    assert [a["reason"] for a in answers] == ["firewall_denied"]


@pytest.mark.asyncio
async def test_a_stranger_relaying_a_held_file_is_answered_already_received_and_nobody_is_asked(root):
    net = _Network()
    b, c, offer = await _relay_of_a_held_file(net, root)
    del b.service.firewall.rules["file_transfer"]["nodes"][c.me]
    from dpc_client_core.managers.file_transfer_manager import FILE_TRANSFER_ASK
    decision = await b.manager._file_transfer_decision(
        c.me, offer["filename"], offer["size_bytes"], offer["mime_type"])
    assert decision.outcome == FILE_TRANSFER_ASK, "C is a stranger: no rule, no proved connection"
    shown_before = len(b.api.named("file_transfer_offered"))

    answers = await _deliver_offer(net, b, c.me, offer)

    assert [a["reason"] for a in answers] == [ALREADY_RECEIVED_REASON]
    assert len(b.api.named("file_transfer_offered")) == shown_before, "no 'allow files from X?'"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "image", "voice"])
async def test_an_offer_from_an_older_peer_without_the_fields_still_completes(root, kind):
    net = _Network()
    author, receiver = full_mesh(net, root, 2)
    net.strip_identity_from.add(author.me)

    await send(author, kind, root)
    await net.settle()

    offer = net.offers_to(receiver.me)[0][3]
    assert "message_id" not in offer and "sender_node_id" not in offer
    assert not net.errors, net.errors
    _the_member_holds_one_copy_and_one_record(receiver, author)


@pytest.mark.asyncio
async def test_a_mesh_of_older_relays_still_settles_on_the_hash(root):
    """Two members strip the fields from what they relay, as a node from before them would."""
    net = _Network()
    author, *others = full_mesh(net, root, 4)
    net.strip_identity_from.update(n.me for n in others[:2])

    await send(author, "file", root)
    await net.settle()

    assert not net.errors, net.errors
    for node in others:
        _the_member_holds_one_copy_and_one_record(node, author)


@pytest.mark.asyncio
async def test_already_received_frees_the_relays_upload_and_shows_nothing(root):
    net = _Network()
    author, b, c = full_mesh(net, root, 3)

    await send(author, "file", root)
    await net.settle()

    assert len(net.cancels(ALREADY_RECEIVED_REASON)) == 2
    for node in (b, c):
        assert not [t for t in node.manager.active_transfers.values() if t.direction == "upload"]
        assert node.api.named("file_transfer_cancelled") == [], "a refusal of a copy is not a failure"


@pytest.mark.asyncio
async def test_a_relaying_member_adds_no_sent_record_of_its_own(root):
    net = _Network()
    hub, author, other_leaf = star(net, root, leaves=2)

    await send(author, "file", root)
    await net.settle()

    recs = records(hub)
    assert [r["sender_node_id"] for r in recs] == [author.me], recs
    assert not [e for e in hub.api.named("group_file_received") if e["sender_node_id"] == "user"], \
        "no 'You' bubble for a file the hub only passed on"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "image", "voice"])
async def test_the_authors_record_carries_the_id_its_offer_announces(root, kind):
    net = _Network()
    author, receiver = full_mesh(net, root, 2)

    await send(author, kind, root)
    await net.settle()

    offered = net.offers_to(receiver.me)[0][3]["message_id"]
    assert [r["id"] for r in records(author)] == [offered]
    assert [e["message_id"] for e in author.api.named("group_file_received")] == [offered]
    assert [r["id"] for r in records(receiver)] == [offered], "the member's note is keyed by it too"


@pytest.mark.asyncio
async def test_a_relay_past_the_upload_cap_is_logged_not_left_to_the_loop(root, caplog):
    net = _Network()
    hub, author, other_leaf = star(net, root, leaves=2)
    hub.manager.max_concurrent_transfers = 0
    unretrieved = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: unretrieved.append(ctx))

    with caplog.at_level(logging.WARNING):
        await send(author, "file", root)
        await net.settle()
        import gc
        gc.collect()
        await asyncio.sleep(0.05)

    assert unretrieved == []
    assert any("Could not relay group file" in r.getMessage() for r in caplog.records)
    assert net.offers_to(other_leaf.me) == []
