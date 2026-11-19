"""A file asked for with FILE_REQUEST is taken only when it is the file requested.

Found by Zcode in review of 9dbf0866, 2026-11-19: the requester auto-accepted
an offer that answered its request after checking only `request_id`, that the
peer was one it asked, and the group. The offer's name, size and hash were never
compared with the record's, so any peer that was asked could push any bytes,
and the transfer completed someone else's record with them.

Now, over the in-process network of the FILE_REQUEST tests (real
FileTransferManager, offer / complete / cancel handlers, FILE_REQUEST handlers):

- an offer whose hash is not the record's hash (another hash, or "none"), whose
  size is not the record's size, or whose name is not the requested name, is
  refused with FILE_CANCEL `not_the_requested_file`, and the next member is asked;
- an offer with the record's hash is taken and completes the record;
- bytes that do not hash to the record's hash are dropped at completion
  (`hash_mismatch`) even when the offer declared the right hash and even with
  `verify_hash` off, and the next member is asked;
- a record with no hash (a sender's own record of a group file has none) is
  completed on its name and size and marked `content_verified: False`; a record
  with neither hash nor size is not requested at all.

And the holder's and requester's limits from the same review: one file to one
requester at most once a minute and never while it is being sent (`busy` with
`retry_after_sec`); the requester waits out a short `retry_after_sec` once and
asks again; the per-requester bookkeeping forgets a peer that stopped asking.

Cards: A-NODE-THAT-SYNCS-A-RECORD-NEVER-FETCHES-THE-FILE-IT-LACKS,
A-PASTE-MADE-WHILE-THE-PEER-IS-OFFLINE-SYNCS-THE-RECORD-AND-NEVER-THE-BYTES.
"""

import asyncio
import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.managers import file_request as file_request_mod
from dpc_client_core.managers.file_request import (
    BUSY_REASON,
    FILE_REQUEST,
    FILE_REQUEST_DECLINED,
    NOT_THE_REQUESTED_FILE_REASON,
    SAME_FILE_INTERVAL_S,
)
from dpc_client_core.managers.file_transfer_manager import FileTransfer, TransferStatus
from dpc_client_core.service import CoreService
from tests.test_a_group_file_crosses_a_full_mesh_once import GROUP, records
from tests.test_a_synced_record_without_its_file_can_be_requested_from_a_peer_online import (
    _a_file_sent_while_c_was_away,
    _RequestNetwork,
)

TERMINAL = ("received", "not_held", "no_answer", "failed", "busy")
CONTENT = b"x" * 1000  # what the harness's author sends as notes.txt
CONTENT_HASH = hashlib.sha256(CONTENT).hexdigest()


class _Network(_RequestNetwork):
    """The FILE_REQUEST network, where a frame from one node to another can be altered."""

    def __init__(self):
        super().__init__()
        self.mutate = {}  # (from, to, command) -> payload -> payload

    async def _deliver(self, to, frm, message):
        change = self.mutate.get((frm, to, message["command"]))
        if change is not None:
            message = dict(message, payload=change(dict(message["payload"])))
        return await super()._deliver(to, frm, message)


@pytest.fixture
def root(tmp_path, monkeypatch):
    from dpc_client_core import service as service_mod
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", tmp_path / "home")
    return tmp_path


def _states(node, request_id):
    return [s for s in node.api.named("file_request_status") if s["request_id"] == request_id]


async def _outcome(net, node, request_id, limit_s=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while loop.time() < deadline:
        states = _states(node, request_id)
        if states and states[-1]["state"] in TERMINAL:
            await net.settle()
            return states[-1]
        await asyncio.sleep(0.02)
    raise AssertionError(f"request {request_id} never ended: {_states(node, request_id)}")


async def _setup(root, record_hash):
    """A sent notes.txt to B while C was away; C holds A's record without the file.

    With `record_hash`, C's record carries the file's content hash (as a
    member's record of a received file does); without, it is the author's own
    record as synced, which carries none.
    """
    net = _Network()
    a, b, c, _ = await _a_file_sent_while_c_was_away(net, root)
    (rec,) = records(c)
    if record_hash:
        rec["attachments"][0]["hash"] = CONTENT_HASH
    else:
        assert not rec["attachments"][0].get("hash"), "the author's record carries no hash"
    return net, a, b, c, rec


def _a_answers_with(a, path: Path, filename=None):
    """A answers every FILE_REQUEST by offering `path` instead of the file requested."""
    async def answer(node_id, payload):
        await a.manager.send_file(
            node_id=node_id, file_path=path, group_id=payload.get("group_id"),
            message_id=payload["message_id"], sender_node_id=a.me,
            filename=filename or payload["filename"], request_id=payload["request_id"])
    a.manager.file_requests.handle_request = answer


def _other_file(root, data, name="notes.txt"):
    path = root / "elsewhere" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _cancels_to(net, node_id, reason):
    return [f for f in net.cancels(reason) if f[0] == node_id]


def _the_record_holds_the_authors_bytes(c, rec):
    (after,) = records(c)
    assert after["id"] == rec["id"], "the requested record, and no second one"
    path = Path(after["attachments"][0]["file_path"])
    assert path.is_file() and path.read_bytes() == CONTENT
    return after["attachments"][0]


# --- (1) an offer of another file is refused, and the next member is asked -----------

@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["another_hash", "hash_none", "another_size", "another_name"])
async def test_an_offer_of_another_file_is_refused_and_the_next_member_serves(root, case):
    record_hash = case in ("another_hash", "hash_none")
    net, a, b, c, rec = await _setup(root, record_hash)
    if case == "another_hash":
        _a_answers_with(a, _other_file(root, b"y" * len(CONTENT)))      # same name and size
    elif case == "hash_none":
        _a_answers_with(a, _other_file(root, b"y" * len(CONTENT)))
        net.mutate[(a.me, c.me, "FILE_OFFER")] = lambda p: dict(p, hash="none", chunk_hashes=None)
    elif case == "another_size":
        _a_answers_with(a, _other_file(root, b"y" * (len(CONTENT) + 1)))
    else:
        _a_answers_with(a, _other_file(root, b"y" * len(CONTENT), "other.txt"), filename="other.txt")

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    assert [f[0] for f in net.named(FILE_REQUEST)] == [a.me, b.me], "A refused, B asked next"
    assert len(_cancels_to(net, a.me, NOT_THE_REQUESTED_FILE_REASON)) == 1
    _the_record_holds_the_authors_bytes(c, rec)
    offers = net.offers_to(c.me)
    assert [o[1] for o in offers] == [a.me, b.me]
    assert not c.manager.active_transfers and not a.manager.active_transfers


@pytest.mark.asyncio
async def test_when_the_only_member_online_offers_another_file_the_request_fails_and_nothing_lands(root):
    net, a, b, c, rec = await _setup(root, record_hash=True)
    c.service.p2p_coordinator = SimpleNamespace(get_connected_peers=lambda: [a.me])
    _a_answers_with(a, _other_file(root, b"y" * len(CONTENT)))

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "failed", final
    (after,) = records(c)
    assert not Path(after["attachments"][0]["file_path"]).is_file()
    assert c.api.named("group_file_received") == []
    received = list((c.root / GROUP).rglob("notes*.txt")) if (c.root / GROUP).exists() else []
    assert received == [], received


# --- (2) the requested file is taken ---------------------------------------------------

@pytest.mark.asyncio
async def test_an_offer_with_the_records_hash_is_taken_and_completes_the_record(root):
    net, a, b, c, rec = await _setup(root, record_hash=True)

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    assert [f[0] for f in net.named(FILE_REQUEST)] == [a.me]
    att = _the_record_holds_the_authors_bytes(c, rec)
    assert "content_verified" not in att, "checked against the record's own hash"
    assert final["attachment_index"] == 0
    assert net.cancels() == []


# --- (3) the bytes are checked against the record's hash at completion -----------------

def _tamper(payload):
    data = base64.b64decode(payload["data"])
    payload["data"] = base64.b64encode(bytes(b ^ 0xFF for b in data)).decode()
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_hash", [True, False])
async def test_bytes_that_do_not_hash_to_the_records_hash_are_dropped_and_the_next_member_serves(
        root, verify_hash):
    # A declares the record's hash and sends other bytes of the same length; it
    # leaves out the per-chunk CRCs, which an older sender does not send either.
    net, a, b, c, rec = await _setup(root, record_hash=True)
    c.manager.verify_hash = verify_hash
    net.mutate[(a.me, c.me, "FILE_OFFER")] = lambda p: dict(p, chunk_hashes=None)
    net.mutate[(a.me, c.me, "FILE_CHUNK")] = _tamper

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    assert len(_cancels_to(net, a.me, "hash_mismatch")) == 1
    assert [f[0] for f in net.named(FILE_REQUEST)] == [a.me, b.me]
    _the_record_holds_the_authors_bytes(c, rec)
    states = [s["state"] for s in _states(c, answer["request_id"])]
    assert states == ["requesting", "receiving", "receiving", "received"], states


# --- (4) a record without a hash --------------------------------------------------------

@pytest.mark.asyncio
async def test_a_record_without_a_hash_is_completed_on_name_and_size_and_marked_unverified(root):
    net, a, b, c, rec = await _setup(root, record_hash=False)

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    att = _the_record_holds_the_authors_bytes(c, rec)
    assert att["content_verified"] is False
    assert final["attachment"]["content_verified"] is False


@pytest.mark.asyncio
async def test_a_record_with_neither_hash_nor_size_is_not_requested(root):
    net, a, b, c, rec = await _setup(root, record_hash=False)
    rec["attachments"][0].pop("size_bytes")

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])

    assert answer["status"] == "error", answer
    assert net.named(FILE_REQUEST) == []


# --- (5) the holder serves one file to one requester at most once a minute -------------

def _payload(rec, request_id):
    att = rec["attachments"][0]
    return {"request_id": request_id, "message_id": rec["id"], "filename": att["filename"],
            "size_bytes": att["size_bytes"], "group_id": GROUP}


@pytest.mark.asyncio
async def test_the_same_file_asked_again_by_the_same_member_is_answered_busy_with_a_wait(root, monkeypatch):
    net, a, b, c, rec = await _setup(root, record_hash=False)
    offered = []

    async def send_file(**kw):
        offered.append((kw["node_id"], kw["request_id"]))
        return "t"

    monkeypatch.setattr(a.manager, "send_file", send_file)
    holder = a.manager.file_requests

    await holder.handle_request(c.me, _payload(rec, "r-1"))
    await holder.handle_request(c.me, _payload(rec, "r-2"))
    await holder.handle_request(b.me, _payload(rec, "r-3"))  # another member is not held up
    await net.settle()

    assert offered == [(c.me, "r-1"), (b.me, "r-3")]
    (busy,) = net.named(FILE_REQUEST_DECLINED, to=c.me)
    assert busy[3]["request_id"] == "r-2" and busy[3]["reason"] == BUSY_REASON
    assert 0 < busy[3]["retry_after_sec"] <= SAME_FILE_INTERVAL_S

    # A minute later the file is served again.
    for key in list(holder._served):
        holder._served[key] -= SAME_FILE_INTERVAL_S
    await holder.handle_request(c.me, _payload(rec, "r-4"))
    assert offered[-1] == (c.me, "r-4")

    # Never while an upload of it to that member runs, however long ago it began.
    holder._served.clear()
    path = Path(records(a)[0]["attachments"][0]["file_path"])
    a.manager.active_transfers["up-1"] = FileTransfer(
        transfer_id="up-1", filename=path.name, size_bytes=1000, hash="h", mime_type="text/plain",
        chunk_size=65536, node_id=c.me, direction="upload", status=TransferStatus.TRANSFERRING,
        chunks_received=set(), total_chunks=1, file_path=path, request_id="r-4")
    await holder.handle_request(c.me, _payload(rec, "r-5"))
    await net.settle()
    assert offered[-1] == (c.me, "r-4")
    last = net.named(FILE_REQUEST_DECLINED, to=c.me)[-1][3]
    assert last["request_id"] == "r-5" and last["reason"] == BUSY_REASON
    assert last["retry_after_sec"] == int(SAME_FILE_INTERVAL_S)


def test_a_member_that_stopped_asking_is_forgotten(monkeypatch):
    holder = file_request_mod.FileRequests(SimpleNamespace(service=None, p2p_manager=None))
    clock = [1000.0]
    monkeypatch.setattr(file_request_mod.time, "monotonic", lambda: clock[0])
    holder._too_many("dpc-node-gone")
    assert "dpc-node-gone" in holder._asked_by
    clock[0] += 61
    holder._too_many("dpc-node-here")
    assert list(holder._asked_by) == ["dpc-node-here"]


# --- (6) the requester honours retry_after_sec ------------------------------------------

def _a_is_busy_first(a, retry_after_sec):
    real = a.manager.file_requests.handle_request
    calls = []

    async def answer(node_id, payload):
        calls.append(payload["request_id"])
        if len(calls) == 1:
            await a.manager.file_requests._decline(
                node_id, payload["request_id"], BUSY_REASON, retry_after_sec=retry_after_sec)
        else:
            await real(node_id, payload)
    a.manager.file_requests.handle_request = answer
    return calls


@pytest.mark.asyncio
async def test_a_short_retry_after_is_waited_out_once_and_the_peer_asked_again(root):
    net, a, b, c, rec = await _setup(root, record_hash=False)
    c.service.p2p_coordinator = SimpleNamespace(get_connected_peers=lambda: [a.me])
    calls = _a_is_busy_first(a, 0.3)

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert not net.errors, net.errors
    assert final["state"] == "received", final
    assert len(calls) == 2
    waiting = [s for s in _states(c, answer["request_id"]) if s.get("retry_after_sec")]
    assert waiting and waiting[0]["state"] == "requesting" and waiting[0]["retry_after_sec"] == 0.3
    _the_record_holds_the_authors_bytes(c, rec)


@pytest.mark.asyncio
async def test_a_long_retry_after_ends_the_request_as_busy_with_the_wait(root):
    net, a, b, c, rec = await _setup(root, record_hash=False)
    c.service.p2p_coordinator = SimpleNamespace(get_connected_peers=lambda: [a.me])
    calls = _a_is_busy_first(a, 600)

    answer = await CoreService.request_attachment(c.service, GROUP, rec["id"])
    final = await _outcome(net, c, answer["request_id"])

    assert final["state"] == "busy", final
    assert final["retry_after_sec"] == 600 and final["busy"] == [a.me]
    assert len(calls) == 1
