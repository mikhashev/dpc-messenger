"""A record that arrived without its file says so, and the file can be asked for.

History sync carries records, never bytes. On 2026-11-19 the Linux node of
group 1234 was re-added after Mike sent an mp3, synced record #69 with the
Windows node's path in it, and showed a player and "completed" for a file it
did not have; no FILE_OFFER ever followed. Mike's call the same day: show that
the file is not on this node and let the node request it; the automatic fetch
is a card of its own (A-NODE-THAT-SYNCS-A-RECORD-NEVER-FETCHES-THE-FILE-IT-LACKS).

What is checked here, over the in-process network of
test_a_group_file_crosses_a_full_mesh_once with the real FileTransferManager,
offer, complete and cancel handlers and the new FILE_REQUEST handlers:

- a record whose `file_path` is not a file here is served to the UI with
  `available: False`, and one whose file is here exactly as before;
- `request_attachment` asks the author first; the holder answers with an
  ordinary offer carrying the `request_id`; the requester's existing record
  gains the local path, and no side writes a second record;
- a node that is not a member of the group, or asks for a group file as if it
  were a one-to-one one, is answered `not_held` and offered nothing;
- with nobody online holding the file the requester hears `not_held`, and with
  nobody online at all `no_peers`;
- a peer that predates FILE_REQUEST drops it; the requester moves on to the
  next peer, and says `no_answer` when nobody else has it.

Card: A-PASTE-MADE-WHILE-THE-PEER-IS-OFFLINE-SYNCS-THE-RECORD-AND-NEVER-THE-BYTES.
"""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.managers.file_request import (
    FILE_REQUEST,
    FILE_REQUEST_DECLINED,
    NOT_HELD_REASON,
    mark_availability,
)
from dpc_client_core.message_handlers.file_request_handler import (
    FileRequestDeclinedHandler,
    FileRequestHandler,
)
from dpc_client_core.service import CoreService
from tests.test_a_group_file_crosses_a_full_mesh_once import (
    GROUP, _ids, _make_node, _Network, records, send,
)

TERMINAL = ("received", "not_held", "no_answer", "failed")


class _RequestNetwork(_Network):
    """The harness network, which also carries FILE_REQUEST and its answer.

    A node in `old` predates the command: its router logs an unknown command
    and drops it (message_router.py), so nothing is answered.
    """

    def __init__(self):
        super().__init__()
        self.old = set()

    async def _deliver(self, to, frm, message):
        command = message["command"]
        if command not in (FILE_REQUEST, FILE_REQUEST_DECLINED):
            return await super()._deliver(to, frm, message)
        try:
            await asyncio.sleep(0)
            self.frames.append((to, frm, command, message["payload"]))
            if to in self.old:
                return
            handler = FileRequestHandler if command == FILE_REQUEST else FileRequestDeclinedHandler
            await handler(self.nodes[to].service).handle(frm, message["payload"])
        except Exception as e:  # noqa: BLE001 - reported by the test
            self.errors.append(f"{command} to {to[9:10]}: {type(e).__name__}: {e}")
        finally:
            self.in_flight -= 1

    def named(self, command, to=None):
        return [f for f in self.frames if f[2] == command and (to is None or f[0] == to)]


@pytest.fixture
def root(tmp_path, monkeypatch):
    from dpc_client_core import service as service_mod
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path / "home")
    return tmp_path


def _statuses(node):
    return [p for p in node.api.named("file_request_status")]


async def _outcome(net, node, request_id, limit_s=10.0):
    """The request's last state once it is terminal, after the network is quiet."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while loop.time() < deadline:
        states = [s for s in _statuses(node) if s["request_id"] == request_id]
        if states and states[-1]["state"] in TERMINAL:
            await net.settle()
            return states[-1]
        await asyncio.sleep(0.02)
    raise AssertionError(f"request {request_id} never ended: {_statuses(node)}")


def _synced_copy(record, root):
    """The author's record as history sync hands it over: the author's path, no file here."""
    foreign = str(root / "the-authors-disk" / "files" / record["attachments"][0]["filename"])
    return dict(record, attachments=[dict(record["attachments"][0], file_path=foreign)])


async def _a_file_sent_while_c_was_away(net, root, kind="file"):
    """A sends a group file (or image, or voice message) to B while C is not
    connected; C then syncs A's record."""
    a_id, b_id, c_id = _ids(3)
    members = [a_id, b_id, c_id]
    a_peers, b_peers, c_peers = [b_id], [a_id], [a_id, b_id]
    a = _make_node(net, root, a_id, a_peers, members)
    b = _make_node(net, root, b_id, b_peers, members)
    c = _make_node(net, root, c_id, c_peers, members)
    sent = await send(a, kind, root)
    await net.settle()
    assert not net.errors, net.errors
    (authors_record,) = records(a)
    c.service._get_or_create_conversation_monitor(GROUP).message_history.append(
        _synced_copy(authors_record, root))
    # C comes back: A and B now see it connected.
    a_peers.append(c_id)
    b_peers.append(c_id)
    return a, b, c, sent


# --- (1) availability as the UI receives it ----------------------------------------

def test_a_path_that_is_not_a_file_here_is_marked_unavailable_and_a_file_here_is_untouched(tmp_path):
    here = tmp_path / "song.mp3"
    here.write_bytes(b"ID3")
    for foreign in ("C:\\Users\\mikha\\.dpc\\conversations\\g\\files\\song.mp3",
                    "/home/mike/.dpc/conversations/g/files/song.mp3",
                    str(tmp_path / "gone" / "song.mp3")):
        for kind in ("file", "image", "voice"):
            att = mark_availability({"type": kind, "filename": "song.mp3", "file_path": foreign})
            assert att["available"] is False, (kind, foreign)
    present = {"type": "file", "filename": "song.mp3", "file_path": str(here), "status": "completed"}
    assert mark_availability(dict(present)) == present, "a file that is here renders as today"
    # No path at all (a screenshot not kept) is not judged: its thumbnail is the picture.
    assert "available" not in mark_availability({"type": "image", "filename": "s.png", "thumbnail": "data:"})


@pytest.mark.asyncio
async def test_a_synced_group_record_reaches_the_ui_marked_unavailable(tmp_path):
    conv_dir = tmp_path / GROUP
    conv_dir.mkdir()
    here = conv_dir / "files" / "mine.mp3"
    here.parent.mkdir()
    here.write_bytes(b"ID3 mine")
    messages = [
        {"id": "m-1", "role": "assistant", "content": "", "sender_node_id": "dpc-node-" + "b" * 32,
         "attachments": [{"type": "file", "filename": "song.mp3", "size_bytes": 3,
                          "status": "completed", "mime_type": "audio/mpeg",
                          "file_path": "C:\\Users\\mikha\\.dpc\\conversations\\x\\files\\song.mp3"}]},
        {"id": "m-2", "role": "user", "content": "", "sender_node_id": "dpc-node-" + "a" * 32,
         "attachments": [{"type": "file", "filename": "mine.mp3", "size_bytes": 8,
                          "status": "completed", "mime_type": "audio/mpeg", "file_path": str(here)}]},
    ]
    (conv_dir / "history.json").write_text(json.dumps({"messages": messages}), encoding="utf-8")
    fake = SimpleNamespace(
        conversation_monitors={}, llm_manager=None,
        group_manager=SimpleNamespace(_get_conversation_dir=lambda g: conv_dir),
    )

    page = await CoreService.get_conversation_history(fake, GROUP)

    assert page["status"] == "success", page
    synced, mine = page["messages"]
    assert synced["attachments"][0]["available"] is False
    assert "available" not in mine["attachments"][0]
    on_disk = json.loads((conv_dir / "history.json").read_text(encoding="utf-8"))
    assert "available" not in on_disk["messages"][0]["attachments"][0], "a view, never stored"


@pytest.mark.asyncio
async def test_a_one_to_one_record_is_marked_and_a_local_ai_chat_is_not(tmp_path):
    gone = {"type": "image", "filename": "shot.png", "size_bytes": 3, "status": "completed",
            "file_path": str(tmp_path / "elsewhere" / "shot.png"), "thumbnail": "data:image/png;base64,"}
    record = {"id": "m-1", "role": "assistant", "content": "", "attachments": [gone]}
    monitor = SimpleNamespace(
        get_message_history=lambda: [record], get_token_usage=lambda: {},
        _tokens_after_last_response=0, _tokens_after_last_response_at=None,
        live_history_boundary=None,
    )
    peer = "dpc-node-" + "b" * 32
    fake = SimpleNamespace(conversation_monitors={peer: monitor, "local_ai": monitor},
                           _voice_transcriptions={})

    one_to_one = await CoreService.get_conversation_history(fake, peer)
    ai_chat = await CoreService.get_conversation_history(fake, "local_ai")

    assert one_to_one["messages"][0]["attachments"][0]["available"] is False
    assert "available" not in ai_chat["messages"][0]["attachments"][0], "an AI chat has nobody to ask"
    assert "available" not in record["attachments"][0], "the stored record is untouched"


# --- (2) the author serves it, the requester's record gains the path ---------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "image", "voice"])
async def test_the_author_offers_the_file_and_the_synced_record_gains_the_local_path(root, kind):
    net = _RequestNetwork()
    a, b, c, sent = await _a_file_sent_while_c_was_away(net, root, kind)
    # A screenshot the owner does not keep is still kept when the owner asked for it.
    c.service.firewall.rules["image_transfer"]["save_screenshots_to_disk"] = False
    (before,) = records(c)
    authors_file = Path(records(a)[0]["attachments"][0]["file_path"])
    a_records = len(records(a))

    answer = await CoreService.request_attachment(c.service, GROUP, before["id"])
    assert answer["status"] == "requesting", answer
    assert answer["asking"][0] == a.me, "the author is asked first"
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    (after,) = records(c)
    assert after["id"] == before["id"], "the same record, not a second one"
    path = Path(after["attachments"][0]["file_path"])
    assert path.is_file() and path.read_bytes() == authors_file.read_bytes()
    assert final["attachment"]["file_path"] == str(path)
    assert len(records(a)) == a_records, "the holder writes no record for a requested file"
    assert [f[0] for f in net.named(FILE_REQUEST)] == [a.me], "B was not asked: A answered"
    (offer,) = net.offers_to(c.me)
    assert offer[3]["request_id"] == answer["request_id"]
    assert offer[3]["message_id"] == before["id"]
    assert len(net.offers_to(b.me)) == 1, "the author's own send only: a requested file is not relayed on"
    states = [s["state"] for s in _statuses(c)]
    assert states == ["requesting", "receiving", "received"], states
    shown = c.api.named("group_file_received")[-1]
    assert shown["message_id"] == before["id"]
    assert not c.manager.active_transfers and not a.manager.active_transfers


@pytest.mark.asyncio
async def test_a_file_already_here_is_not_requested(root):
    net = _RequestNetwork()
    a, b, c, sent = await _a_file_sent_while_c_was_away(net, root)
    (authors,) = records(a)

    answer = await CoreService.request_attachment(a.service, GROUP, authors["id"])

    assert answer["status"] == "available"
    assert net.named(FILE_REQUEST) == []


@pytest.mark.asyncio
async def test_a_one_to_one_file_is_found_by_its_hash_and_completes_the_requesters_record(root):
    net = _RequestNetwork()
    a_id, c_id = _ids(3)[0], _ids(3)[2]
    a = _make_node(net, root, a_id, [c_id], [a_id, c_id])
    c = _make_node(net, root, c_id, [a_id], [a_id, c_id])
    data = b"%PDF one to one"
    digest = hashlib.sha256(data).hexdigest()
    kept = root / a_id / "sent" / "report.pdf"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(data)
    att = {"type": "file", "filename": "report.pdf", "size_bytes": len(data), "hash": digest,
           "mime_type": "application/pdf", "status": "completed"}
    # Each side of a one-to-one chat ids its record of a file its own way.
    a.service._get_or_create_conversation_monitor(c_id).message_history.append(
        {"id": "a-side", "role": "user", "sender_node_id": a_id,
         "attachments": [dict(att, file_path=str(kept))]})
    c.service._get_or_create_conversation_monitor(a_id).message_history.append(
        {"id": "c-side", "role": "assistant", "sender_node_id": a_id,
         "attachments": [dict(att, file_path="/home/someone/.dpc/report.pdf")]})

    answer = await CoreService.request_attachment(c.service, a_id, "c-side")
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    (record,) = c.monitors[a_id].message_history
    assert Path(record["attachments"][0]["file_path"]).read_bytes() == data
    (offered,) = c.api.named("file_transfer_offered")
    assert offered["request_id"] == answer["request_id"], "the UI opens no accept dialog for it"
    assert len(a.monitors[c_id].message_history) == 1


# --- (3) the firewall: only a member, only from that conversation ------------------

@pytest.mark.asyncio
async def test_a_node_outside_the_group_is_answered_not_held_and_offered_nothing(root):
    net = _RequestNetwork()
    a, b, c, sent = await _a_file_sent_while_c_was_away(net, root)
    (authors,) = records(a)
    # D believes it is in the group; on A's roster it is not.
    d_id = "dpc-node-" + "d" * 32
    d = _make_node(net, root, d_id, [a.me], [a.me, d_id])
    d.service._get_or_create_conversation_monitor(GROUP).message_history.append(
        _synced_copy(authors, root))

    answer = await CoreService.request_attachment(d.service, GROUP, authors["id"])
    final = await _outcome(net, d, answer["request_id"])

    assert final["state"] == "not_held", final
    (declined,) = net.named(FILE_REQUEST_DECLINED, to=d_id)
    assert declined[3] == {"request_id": answer["request_id"], "reason": NOT_HELD_REASON}
    assert net.offers_to(d_id) == []


@pytest.mark.asyncio
async def test_a_group_file_asked_for_as_a_one_to_one_file_is_not_served(root):
    # A member asking without the group asks for A's one-to-one chat with it,
    # where the file is not: the group's files are served only as the group's.
    net = _RequestNetwork()
    a, b, c, sent = await _a_file_sent_while_c_was_away(net, root)
    (authors,) = records(a)
    att = authors["attachments"][0]
    await c.manager.p2p_manager.send_message_to_peer(a.me, {"command": FILE_REQUEST, "payload": {
        "request_id": "r-1", "message_id": authors["id"], "filename": att["filename"],
        "size_bytes": att["size_bytes"]}})
    await net.settle()

    (declined,) = net.named(FILE_REQUEST_DECLINED, to=c.me)
    assert declined[3]["reason"] == NOT_HELD_REASON
    assert net.offers_to(c.me) == []


# --- (4) nobody online has it --------------------------------------------------------

@pytest.mark.asyncio
async def test_with_only_a_member_that_lost_the_file_online_the_answer_is_not_held(root):
    net = _RequestNetwork()
    a, b, c, sent = await _a_file_sent_while_c_was_away(net, root)
    (rec,) = records(c)
    # The author goes offline; B deletes its copy.
    c.service.p2p_coordinator = SimpleNamespace(get_connected_peers=lambda: [b.me])
    for f in (b.root / GROUP).rglob("notes*.txt"):
        f.unlink()

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    assert answer["asking"] == [b.me]
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "not_held", final
    assert net.offers_to(c.me) == []
    assert "available" not in records(c)[0]["attachments"][0], "the stored record is untouched"


@pytest.mark.asyncio
async def test_with_nobody_online_the_answer_is_no_peers_and_nothing_is_sent(root):
    net = _RequestNetwork()
    a, b, c, sent = await _a_file_sent_while_c_was_away(net, root)
    (rec,) = records(c)
    c.service.p2p_coordinator = SimpleNamespace(get_connected_peers=lambda: [])

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])

    assert answer["status"] == "no_peers", answer
    assert net.named(FILE_REQUEST) == []


# --- (5) a peer from before FILE_REQUEST ---------------------------------------------

@pytest.mark.asyncio
async def test_an_old_author_is_skipped_after_the_timeout_and_the_next_member_serves(root):
    net = _RequestNetwork()
    a, b, c, sent = await _a_file_sent_while_c_was_away(net, root)
    (rec,) = records(c)
    net.old.add(a.me)
    c.manager.file_requests.answer_timeout_s = 0.2

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    assert [f[0] for f in net.named(FILE_REQUEST)] == [a.me, b.me]
    (offer,) = net.offers_to(c.me)
    assert offer[1] == b.me
    assert len(records(c)) == 1
    assert Path(records(c)[0]["attachments"][0]["file_path"]).is_file()


@pytest.mark.asyncio
async def test_when_every_peer_online_is_old_the_requester_says_nobody_answered(root):
    net = _RequestNetwork()
    a, b, c, sent = await _a_file_sent_while_c_was_away(net, root)
    (rec,) = records(c)
    net.old.update({a.me, b.me})
    c.manager.file_requests.answer_timeout_s = 0.1

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "no_answer", final
    assert sorted(final["silent"]) == sorted([a.me, b.me])
    assert net.offers_to(c.me) == []
    assert not net.errors, net.errors
