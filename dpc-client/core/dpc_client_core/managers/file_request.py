"""Ask the peers online for a file this node has a record of and not the bytes.

History sync carries records, never bytes: a member added to a group after a
file was sent, or offline when it was sent, gets the author's record with a
path on the author's disk and no transfer ever follows (card
A-PASTE-MADE-WHILE-THE-PEER-IS-OFFLINE-SYNCS-THE-RECORD-AND-NEVER-THE-BYTES).
This module is the manual pull Mike asked for on 2026-11-19: the chat shows
that the file is not on this node, and a button asks for it.

The requester sends `FILE_REQUEST` to one connected peer at a time — the
record's author first, then the other connected members of the group, or the
other party of a one-to-one chat. A peer that holds the file answers with an
ordinary `FILE_OFFER` that carries the request's `request_id`, so the existing
receive path completes the existing record (`find_file_record` /
`attach_received_file`) instead of adding a second one. A peer that does not
serve it answers `FILE_REQUEST_DECLINED` with `not_held` (or `busy` when its
upload slots are taken). A peer that predates the command drops it as an
unknown command and never answers: the requester moves on after
`answer_timeout_s` and, if nobody offered the file, says that nobody answered.

The holder's rule, checked on every request: a group file only to a member of
that group on this node, from that group's history; a one-to-one file only to
the other party of that chat, from that chat's history. Every other case —
not a member, no such record, no file on disk — reads `not_held`, the one
answer ADR-047 §4 gives, so the answer tells a non-member nothing about what
this node holds.

The requester takes only the file it asked for (found by Zcode in review,
2026-11-19: an answer was matched on `request_id`, the peer and the group
alone, so a peer that was asked could push any bytes and have them glued onto
the record). An offer that answers a request must carry the requested
`filename`, the record's `size_bytes` when the record has one, and the record's
content `hash` when the record has one; otherwise it is refused with
FILE_CANCEL `not_the_requested_file` and the next peer is asked. When the
record has a hash, the bytes that arrive are hashed and compared with *that*
hash, whatever `verify_hash` says and whatever the offer declared; a mismatch
drops the file (`hash_mismatch`) and the next peer is asked. A sender's own
record of a group file gains its hash once the file is hashed
(`service.note_record_hash`, 2026-11-19); a record without a hash (one written
before that) can only be checked by its name and size: the file is
taken when both match, its bytes are checked against the offer's own hash in
transit, and the completed attachment is marked `content_verified: False`,
because nothing ties those bytes to what the author sent. A record with
neither a hash nor a size is not requested at all.

ADR-047 phase 1 adds the author's signed `file_description`. A record that
holds one (the author's own, or one synced from it) is answered only by an
offer carrying the same description: an offer without it, or with another,
is `not_the_requested_file`, so a stripped signature cannot downgrade the
file. A holder re-serves the description its record holds, byte for byte. An
offer whose description does not hold is refused (`file_signature_rejected`)
and the next peer is asked. One whose author's certificate this node lacks is
accepted only once the certificate has come (CERT_REQUEST to the holder), or,
when it does not come within `answer_timeout_s`, given up for the next peer;
the last peer's offer is taken single-source and checked again at the end.

The automatic version (fetch after sync, on reconnect, on being added) is card
A-NODE-THAT-SYNCS-A-RECORD-NEVER-FETCHES-THE-FILE-IT-LACKS, under the epic of
ADR-047; it is meant to call `FileRequests.request` too.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

FILE_REQUEST = "FILE_REQUEST"
FILE_REQUEST_DECLINED = "FILE_REQUEST_DECLINED"

# The one refusal, whatever the cause (ADR-047 §4), and the one that is not.
NOT_HELD_REASON = "not_held"
BUSY_REASON = "busy"
# FILE_CANCEL from the requester: the offer answers its request with another
# file (a name, size or hash the requested record does not have).
NOT_THE_REQUESTED_FILE_REASON = "not_the_requested_file"
# The requester's own note for a peer that offered another file, or sent bytes
# that do not hash to the record's hash; never on the wire as a decline.
_OFFERED_ANOTHER_FILE = "offered_another_file"
# The requester's own note for a peer whose offer carried a description whose
# author's certificate did not come within the answer wait; never on the wire.
_CERTIFICATE_WITHHELD = "certificate_withheld"

# The UI event and its states.
FILE_REQUEST_STATUS_EVENT = "file_request_status"
STATE_REQUESTING = "requesting"
STATE_RECEIVING = "receiving"
STATE_RECEIVED = "received"
STATE_NOT_HELD = "not_held"     # every peer asked answered that it does not serve the file
STATE_NO_ANSWER = "no_answer"   # nobody offered it and at least one peer never answered
STATE_BUSY = "busy"             # nobody offered it and a peer that may hold it answered busy
STATE_FAILED = "failed"         # an offer came, and the transfer ended without the file

# How long the requester waits for one peer before asking the next; the
# precedent is SKILL_SEARCH's 10 s (skill_coordinator.py).
REQUEST_ANSWER_TIMEOUT_S = 10.0
# A request is remembered this long, so an offer that comes late is still taken.
REQUEST_MEMORY_S = 3600.0
# Requests one peer may put to this node per minute; more are answered `busy`.
REQUESTS_PER_MINUTE = 20
# One file is served to one requester at most once per this many seconds, and
# never while an upload of it to that requester runs: a multi-GB file asked for
# again and again would otherwise take the holder's upload for good. A
# requester that got the file never asks again (its record names a file that
# is here), so only one whose transfer failed meets the wait.
SAME_FILE_INTERVAL_S = 60.0
# A `busy` answer whose `retry_after_sec` is at most this is waited out once and
# the peer asked again; a longer one ends the request as `busy`.
MAX_RETRY_WAIT_S = 120.0

ATTACHMENT_KINDS = ("file", "image", "voice")
_MAX_FIELD_CHARS = 128
_MAX_FILENAME_CHARS = 1024


def file_is_here(file_path: Any) -> bool:
    """Whether `file_path` names a file on this machine.

    A record synced from another node carries that node's absolute path
    (`C:\\Users\\...` on Linux, `/home/...` on Windows); on this machine it is
    no file. A string the OS cannot even form a path from is no file either.
    """
    if not isinstance(file_path, str) or not file_path:
        return False
    try:
        return Path(file_path).is_file()
    except (OSError, ValueError):
        return False


def mark_availability(attachment: Dict[str, Any]) -> Dict[str, Any]:
    """`attachment` with `available: False` when it names a file that is not here.

    Only a file, image or voice attachment that carries a `file_path` is judged:
    one without a path (a screenshot the owner chose not to keep) never had a
    file to show and keeps its thumbnail. An attachment whose file is here is
    returned without the field, as before.
    """
    if attachment.get("type") not in ATTACHMENT_KINDS or not attachment.get("file_path"):
        return attachment
    if file_is_here(attachment.get("file_path")):
        attachment.pop("available", None)
        return attachment
    attachment["available"] = False
    return attachment


def _short_field(value: Any, limit: int = _MAX_FIELD_CHARS) -> Optional[str]:
    if isinstance(value, str) and value and len(value) <= limit:
        return value
    return None


def _real_hash(value: Any) -> Optional[str]:
    value = _short_field(value)
    return value if value and value != "none" else None


def _held_description(attachment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The author's description an attachment holds, or None (none, or one this node rejected)."""
    from ..signing import FILE_REJECTED
    from .file_transfer_manager import wire_file_description
    if attachment.get("file_verdict") == FILE_REJECTED:
        return None
    return wire_file_description(attachment.get("file_description"))


@dataclass
class OpenRequest:
    """One request this node put to its peers, until its file arrives or it is forgotten."""

    request_id: str
    conversation_id: str
    group_id: Optional[str]
    message_id: str
    filename: str
    size_bytes: Optional[int]
    file_hash: Optional[str]
    candidates: List[str]
    # Where the attachment is in the record, so the UI completes that one.
    attachment_index: Optional[int] = None
    # The author's signed description the record holds (ADR-047 phase 1), or
    # None: an answer must carry the same one.
    file_description: Optional[Dict[str, Any]] = None
    created: float = field(default_factory=time.monotonic)
    declined: Dict[str, str] = field(default_factory=dict)  # node -> reason
    retry_after: Dict[str, float] = field(default_factory=dict)  # busy node -> seconds it named
    next_candidate: int = 0        # the index in `candidates` asked next
    asking: Optional[str] = None   # the peer whose answer is awaited now
    silent: List[str] = field(default_factory=list)
    retried: bool = False          # the busy peers have had their second ask
    transfer_id: Optional[str] = None
    holder: Optional[str] = None
    state: str = STATE_REQUESTING
    answered: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: Any = field(default=None, repr=False)

    def wire_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "request_id": self.request_id,
            "message_id": self.message_id,
            "filename": self.filename,
        }
        if self.group_id:
            payload["group_id"] = self.group_id
        if isinstance(self.size_bytes, int):
            payload["size_bytes"] = self.size_bytes
        if self.file_hash:
            payload["hash"] = self.file_hash
        return payload


class FileRequests:
    """Both sides of `FILE_REQUEST` for one node; owned by its FileTransferManager."""

    def __init__(self, manager):
        self.manager = manager
        self.answer_timeout_s = REQUEST_ANSWER_TIMEOUT_S
        self._open: Dict[str, OpenRequest] = {}
        self._asked_by: Dict[str, List[float]] = {}  # requester -> request times (holder side)
        # (requester, path) -> when this node last offered it that file (holder side)
        self._served: Dict[tuple, float] = {}

    # --- shared ------------------------------------------------------------

    @property
    def _service(self):
        return self.manager.service

    @property
    def _me(self) -> Optional[str]:
        return getattr(self.manager.p2p_manager, "node_id", None)

    def _connected(self) -> List[str]:
        coordinator = getattr(self._service, "p2p_coordinator", None)
        if coordinator is not None and hasattr(coordinator, "get_connected_peers"):
            return list(coordinator.get_connected_peers() or [])
        return list(getattr(self.manager.p2p_manager, "peers", {}) or {})

    def _monitor(self, conversation_id: str):
        factory = getattr(self._service, "_get_or_create_conversation_monitor", None)
        return factory(conversation_id) if callable(factory) else None

    def _group(self, group_id: str):
        group_manager = getattr(self._service, "group_manager", None)
        return group_manager.get_group(group_id) if group_manager else None

    async def _emit(self, request: OpenRequest, state: str, **extra) -> None:
        request.state = state
        api = self.manager.local_api
        if not api:
            return
        event = {
            "request_id": request.request_id,
            "conversation_id": request.conversation_id,
            "message_id": request.message_id,
            "filename": request.filename,
            "state": state,
            **extra,
        }
        try:
            await api.broadcast_event(FILE_REQUEST_STATUS_EVENT, event)
        except Exception as e:  # noqa: BLE001 - a closed UI must not stop the request
            logger.debug("Could not broadcast %s: %s", FILE_REQUEST_STATUS_EVENT, e)

    def _forget_old(self) -> None:
        now = time.monotonic()
        for rid in [r for r, req in self._open.items() if now - req.created > REQUEST_MEMORY_S]:
            self._open.pop(rid, None)

    # --- requester ---------------------------------------------------------

    @staticmethod
    def _find_attachment(record: Dict[str, Any], filename: Optional[str]):
        """(index, attachment) of the first file attachment named `filename`, or (None, None)."""
        for index, att in enumerate(record.get("attachments") or ()):
            if not isinstance(att, dict) or att.get("type") not in ATTACHMENT_KINDS:
                continue
            if filename and att.get("filename") != filename:
                continue
            return index, att
        return None, None

    async def request(self, conversation_id: str, message_id: str,
                      filename: Optional[str] = None) -> Dict[str, Any]:
        """Ask the peers online for the file of record `message_id` in `conversation_id`.

        Returns at once: `requesting` (with `request_id` and the peers that will
        be asked, author first), `available` when the file is already here,
        `no_peers` when nobody who may hold it is connected, or `error`. The
        outcome arrives as `file_request_status` events.
        """
        self._forget_old()
        if not isinstance(conversation_id, str) or not isinstance(message_id, str) or not message_id:
            return {"status": "error", "message": "conversation_id and message_id are required"}
        is_group = conversation_id.startswith("group-")
        if not is_group and not conversation_id.startswith("dpc-node-"):
            return {"status": "error",
                    "message": "A file can be requested only in a group or a one-to-one chat"}
        group = self._group(conversation_id) if is_group else None
        if is_group and group is None:
            return {"status": "error", "message": f"Group {conversation_id} not found"}

        monitor = self._monitor(conversation_id)
        records = getattr(monitor, "message_history", None) or []
        record = next((r for r in records if r.get("id") == message_id), None)
        if record is None:
            return {"status": "error", "message": f"No message {message_id} in this chat"}
        index, attachment = self._find_attachment(record, filename)
        if attachment is None:
            return {"status": "error", "message": "That message carries no file"}
        if file_is_here(attachment.get("file_path")):
            return {"status": "available", "file_path": attachment["file_path"]}
        size = attachment.get("size_bytes")
        size = size if isinstance(size, int) and not isinstance(size, bool) else None
        file_hash = _real_hash(attachment.get("hash"))
        description = _held_description(attachment)
        if size is None and file_hash is None:
            # Nothing but a name to check an answer against: any bytes under that
            # name would complete the record.
            return {"status": "error",
                    "message": "The record names neither the size nor the hash of its file, "
                               "so a file sent for it could not be checked"}

        for open_request in self._open.values():
            if (open_request.conversation_id == conversation_id
                    and open_request.message_id == message_id
                    and open_request.state in (STATE_REQUESTING, STATE_RECEIVING)):
                return {"status": open_request.state, "request_id": open_request.request_id,
                        "asking": list(open_request.candidates)}

        connected = set(self._connected())
        if is_group:
            members = [m for m in getattr(group, "members", []) or [] if m != self._me]
            author = record.get("sender_node_id")
            ordered = ([author] if author in members else []) + [m for m in members if m != author]
            candidates = [m for m in ordered if m in connected]
        else:
            candidates = [conversation_id] if conversation_id in connected else []
        if not candidates:
            return {"status": "no_peers",
                    "message": "No member of this chat who may hold the file is online"}

        request = OpenRequest(
            request_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            group_id=conversation_id if is_group else None,
            message_id=message_id,
            filename=str(attachment.get("filename") or ""),
            size_bytes=size,
            file_hash=file_hash,
            candidates=candidates,
            attachment_index=index,
            file_description=description,
        )
        self._open[request.request_id] = request
        await self._emit(request, STATE_REQUESTING, asking=list(candidates))
        request.task = asyncio.get_running_loop().create_task(self._ask_in_turn(request))
        logger.info("FILE_REQUEST %s for %s in %s: asking %d peer(s), author first",
                    request.request_id[:8], request.filename, conversation_id, len(candidates))
        return {"status": "requesting", "request_id": request.request_id,
                "asking": list(candidates)}

    async def _ask(self, request: OpenRequest, node_id: str) -> None:
        """Put the request to `node_id` and wait for its answer or the timeout."""
        request.answered.clear()
        request.asking = node_id
        try:
            await self.manager.p2p_manager.send_message_to_peer(node_id, {
                "command": FILE_REQUEST, "payload": request.wire_payload(),
            })
        except Exception as e:  # noqa: BLE001 - a dropped peer is a peer that did not answer
            logger.info("FILE_REQUEST %s could not reach %s: %s",
                        request.request_id[:8], node_id[:20], e)
            request.asking = None
            if node_id not in request.silent:
                request.silent.append(node_id)
            return
        try:
            await asyncio.wait_for(request.answered.wait(), self.answer_timeout_s)
        except asyncio.TimeoutError:
            pass
        request.asking = None
        if (not request.transfer_id and node_id not in request.declined
                and node_id not in request.silent):
            request.silent.append(node_id)

    def _busy_to_ask_again(self, request: OpenRequest) -> Dict[str, float]:
        """The busy peers whose `retry_after_sec` is short enough to wait out, once."""
        if request.retried:
            return {}
        return {node: wait for node, wait in request.retry_after.items()
                if request.declined.get(node) == BUSY_REASON and 0 < wait <= MAX_RETRY_WAIT_S}

    async def _ask_in_turn(self, request: OpenRequest) -> None:
        """Ask the candidates not yet asked, one at a time, until one offers the file.

        Run again, from where it stopped, when the file a peer sent turned out
        not to be the requested one (`wrong_bytes`).
        """
        while request.next_candidate < len(request.candidates):
            if request.transfer_id:
                return
            node_id = request.candidates[request.next_candidate]
            request.next_candidate += 1
            await self._ask(request, node_id)
            if request.transfer_id:
                return
        again = self._busy_to_ask_again(request)
        if again:
            # The peer said when asking again makes sense (`retry_after_sec`):
            # the request waits it out once, still `requesting`, then asks again.
            request.retried = True
            wait = max(again.values())
            await self._emit(request, STATE_REQUESTING, asking=list(again), retry_after_sec=wait)
            await asyncio.sleep(wait)
            for node_id in sorted(again, key=again.get):
                if request.transfer_id:
                    return
                request.declined.pop(node_id, None)
                await self._ask(request, node_id)
                if request.transfer_id:
                    return
        if request.transfer_id:
            return
        await self._end_without_file(request)

    async def _end_without_file(self, request: OpenRequest) -> None:
        reasons = set(request.declined.values())
        if request.silent:
            await self._emit(request, STATE_NO_ANSWER, silent=list(request.silent))
        elif BUSY_REASON in reasons:
            busy = [n for n, r in request.declined.items() if r == BUSY_REASON]
            waits = [request.retry_after[n] for n in busy if n in request.retry_after]
            extra = {"retry_after_sec": max(waits)} if waits else {}
            await self._emit(request, STATE_BUSY, busy=busy, **extra)
        elif _OFFERED_ANOTHER_FILE in reasons:
            await self._emit(request, STATE_FAILED,
                             reason="a peer sent a file other than the one requested")
        elif _CERTIFICATE_WITHHELD in reasons:
            await self._emit(request, STATE_FAILED,
                             reason="no peer sent the certificate that checks the file's author")
        else:
            await self._emit(request, STATE_NOT_HELD)
        logger.info("FILE_REQUEST %s for %s: %s (%d declined, %d silent)",
                    request.request_id[:8], request.filename, request.state,
                    len(request.declined), len(request.silent))

    async def handle_declined(self, node_id: str, payload: Dict[str, Any]) -> None:
        request = self._open.get(payload.get("request_id"))
        if request is None or node_id not in request.candidates:
            return
        reason = payload.get("reason")
        reason = reason if reason in (NOT_HELD_REASON, BUSY_REASON) else NOT_HELD_REASON
        request.declined[node_id] = reason
        wait = payload.get("retry_after_sec")
        if (reason == BUSY_REASON and isinstance(wait, (int, float))
                and not isinstance(wait, bool) and wait > 0):
            request.retry_after[node_id] = float(wait)
        if node_id in request.silent:
            request.silent.remove(node_id)  # it answered after its wait
        # Only the answer awaited now ends the wait: a late answer from a peer
        # asked earlier must not cut short the wait for the one asked now.
        if request.asking == node_id:
            request.answered.set()

    def open_request_for_offer(self, node_id: str, payload: Dict[str, Any]) -> Optional[OpenRequest]:
        """The open request an incoming FILE_OFFER answers, or None.

        It must come from a peer the request was put to, for the same group.
        Whether it offers the file requested is `offer_mismatch`'s question.
        """
        request = self._open.get(payload.get("request_id"))
        if request is None or node_id not in request.candidates:
            return None
        if (payload.get("group_id") or None) != request.group_id:
            return None
        return request

    @staticmethod
    def offer_mismatch(request: OpenRequest, payload: Dict[str, Any]) -> Optional[str]:
        """Why an offer that answers `request` is not the requested file, or None.

        The name always (a holder offers the name its record has, which is the
        requested one); the size when the record has one; the content hash when
        the record has one. The hash an offer declares is checked again against
        the bytes once they are here (`_finalize_download`, `requested_hash`).
        """
        if payload.get("filename") != request.filename:
            return f"name {str(payload.get('filename'))[:80]!r}, requested {request.filename[:80]!r}"
        size = payload.get("size_bytes")
        if request.size_bytes is not None and (
                isinstance(size, bool) or not isinstance(size, int) or size != request.size_bytes):
            return f"size {size!r}, requested {request.size_bytes}"
        if request.file_hash and _real_hash(payload.get("hash")) != request.file_hash:
            return f"hash {str(payload.get('hash'))[:16]!r}, requested {request.file_hash[:16]!r}"
        from .file_transfer_manager import wire_file_description
        offered = wire_file_description(payload.get("file_description"))
        if request.file_description is not None:
            # Downgrade protection, under either legacy policy: the record holds
            # the author's description, so the file comes with it or not at all.
            if offered is None:
                return "no file description, and the record holds the author's"
            if offered != request.file_description:
                return "another file description than the record's"
        elif offered is not None and request.group_id and offered.get("message_id") != request.message_id:
            return (f"a description of message {str(offered.get('message_id'))[:20]!r}, "
                    f"requested {request.message_id[:20]!r}")
        return None

    @staticmethod
    def has_next_candidate(request: OpenRequest) -> bool:
        """Whether the request still has a peer it has not asked."""
        return request.next_candidate < len(request.candidates)

    def accept_once_verified(self, request: OpenRequest, node_id: str, transfer) -> None:
        """Hold an offer whose author's certificate is not here until it is (W2).

        Runs as its own task: the offer handler has registered the transfer and
        asked `node_id` for the certificate. Verified within `answer_timeout_s`:
        FILE_ACCEPT, and the receive begins. Rejected once the certificate is
        here: `file_signature_rejected`. Still unverified: the offer is given up
        with `no_answer` (this node did not answer it in time) and the next
        peer is asked, so a holder that withholds the certificate cannot keep
        the file from being checked.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._accept_once_verified(request, node_id, transfer))

    async def _accept_once_verified(self, request: OpenRequest, node_id: str, transfer) -> None:
        from ..signing import FILE_REJECTED, FILE_VERIFIED
        from .file_transfer_manager import (
            FILE_SIGNATURE_REJECTED_REASON, NO_ANSWER_REASON, TransferStatus,
        )
        manager = self.manager
        author = (transfer.file_description or {}).get("sender_node_id")
        await manager.wait_for_certificate(author, self.answer_timeout_s)
        tid = transfer.transfer_id
        if manager.active_transfers.get(tid) is not transfer or request.transfer_id != tid:
            return  # cancelled meanwhile
        verdict = manager.file_description_verdict_now(node_id, transfer)
        if verdict == FILE_VERIFIED:
            transfer.file_verdict = verdict
            await manager.p2p_manager.send_message_to_peer(node_id, {
                "command": "FILE_ACCEPT", "payload": {"transfer_id": tid},
            })
            transfer.status = TransferStatus.TRANSFERRING
            manager.begin_receiving(tid)
            logger.info("FILE_REQUEST %s: the author's certificate came; offer %s accepted",
                        request.request_id[:8], tid)
            return
        if verdict == FILE_REJECTED:
            reason, note = FILE_SIGNATURE_REJECTED_REASON, _OFFERED_ANOTHER_FILE
            logger.warning("FILE_REQUEST %s: the description %s offered does not hold; refused",
                           request.request_id[:8], node_id[:20])
        else:
            reason, note = NO_ANSWER_REASON, _CERTIFICATE_WITHHELD
            logger.warning("FILE_REQUEST %s: no certificate for %s came from %s within %.0fs; "
                           "asking the next peer", request.request_id[:8], str(author)[:20],
                           node_id[:20], self.answer_timeout_s)
        # The request moves on before the transfer leaves the table, so the
        # leaving reads as no failure of the request (`transfer_ended`).
        request.transfer_id = None
        request.holder = None
        request.declined[node_id] = note
        await manager._send_file_cancel(node_id, tid, reason)
        manager.active_transfers.pop(tid, None)
        if request.task is None or request.task.done():
            request.task = asyncio.get_running_loop().create_task(self._ask_in_turn(request))

    async def offer_mismatched(self, request: OpenRequest, node_id: str, why: str) -> None:
        """The offer from `node_id` was refused as another file: ask the next peer."""
        logger.warning("FILE_REQUEST %s: %s offered another file (%s); refused",
                       request.request_id[:8], node_id[:20], why)
        request.declined[node_id] = _OFFERED_ANOTHER_FILE
        if node_id in request.silent:
            request.silent.remove(node_id)
        if request.asking == node_id:
            request.answered.set()

    async def offer_taken(self, request: OpenRequest, node_id: str, transfer_id: str) -> None:
        request.transfer_id = transfer_id
        request.holder = node_id
        request.answered.set()
        await self._emit(request, STATE_RECEIVING, transfer_id=transfer_id, node_id=node_id)

    async def offer_refused(self, request: OpenRequest, reason: str) -> None:
        """This node's own firewall refused the offer that answers the request."""
        await self._emit(request, STATE_FAILED, reason=reason)

    async def received(self, request_id: Optional[str], attachment: Dict[str, Any],
                       attachment_index: Optional[int] = None) -> None:
        request = self._open.pop(request_id, None) if request_id else None
        if request is None:
            return
        if attachment_index is None:
            attachment_index = request.attachment_index
        extra = {"attachment_index": attachment_index} if attachment_index is not None else {}
        await self._emit(request, STATE_RECEIVED, attachment=dict(attachment), **extra)

    def wrong_bytes(self, request_id: Optional[str], transfer_id: str, node_id: str) -> None:
        """The file `node_id` sent does not hash to the requested record's hash.

        The caller drops the transfer; the request goes on with the peers not
        yet asked, as after a refused offer.
        """
        request = self._open.get(request_id) if request_id else None
        if request is None or request.transfer_id != transfer_id:
            return
        logger.warning("FILE_REQUEST %s: the file %s sent does not match the record's hash",
                       request.request_id[:8], node_id[:20])
        request.transfer_id = None
        request.holder = None
        request.declined[node_id] = _OFFERED_ANOTHER_FILE
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if request.task is None or request.task.done():
            request.task = loop.create_task(self._ask_in_turn(request))

    def transfer_ended(self, request_id: Optional[str], transfer_id: str, reason: str) -> None:
        """A download that answered a request left the table without its file."""
        request = self._open.get(request_id) if request_id else None
        if request is None or request.transfer_id != transfer_id:
            return
        request.transfer_id = None
        request.holder = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._emit(request, STATE_FAILED, reason=reason))

    # --- holder ------------------------------------------------------------

    def _too_many(self, node_id: str) -> bool:
        now = time.monotonic()
        # A peer that asked nothing for a minute is forgotten, so a peer that
        # left, and every peer that ever asked, does not stay in the table.
        for peer in [p for p, times in self._asked_by.items()
                     if not times or now - times[-1] >= 60.0]:
            del self._asked_by[peer]
        recent = [t for t in self._asked_by.get(node_id, []) if now - t < 60.0]
        recent.append(now)
        self._asked_by[node_id] = recent
        return len(recent) > REQUESTS_PER_MINUTE

    def _same_file_wait(self, node_id: str, path: Path) -> Optional[int]:
        """Seconds before `node_id` may be offered `path` again, or None when it may now.

        Never while an upload of that file to that requester runs, and at most
        once per SAME_FILE_INTERVAL_S from the last offer of it.
        """
        now = time.monotonic()
        for key in [k for k, when in self._served.items() if now - when >= SAME_FILE_INTERVAL_S]:
            del self._served[key]
        for transfer in list(self.manager.active_transfers.values()):
            if (transfer.direction == "upload" and transfer.request_id
                    and transfer.node_id == node_id and transfer.file_path == path):
                return int(SAME_FILE_INTERVAL_S)
        last = self._served.get((node_id, str(path)))
        if last is None:
            return None
        return max(1, math.ceil(SAME_FILE_INTERVAL_S - (now - last)))

    def _served_attachment(self, node_id: str, payload: Dict[str, Any]):
        """(record, attachment, path) this node may send `node_id` for the request, or None."""
        from ..conversation_monitor import attachment_matches

        message_id = _short_field(payload.get("message_id"))
        filename = _short_field(payload.get("filename"), _MAX_FILENAME_CHARS)
        size = payload.get("size_bytes")
        size = size if isinstance(size, int) and not isinstance(size, bool) else None
        file_hash = _real_hash(payload.get("hash"))
        group_id = payload.get("group_id")
        if not message_id or not filename:
            return None

        if group_id is not None:
            group_id = _short_field(group_id)
            if not group_id or not group_id.startswith("group-"):
                return None
            group = self._group(group_id)
            members = getattr(group, "members", None) or []
            if group is None or node_id not in members or self._me not in members:
                return None
            conversation_id = group_id
        else:
            conversation_id = node_id  # the one-to-one chat with the requester, nothing else
        monitor = self._monitor(conversation_id)
        records = getattr(monitor, "message_history", None) or []

        def here(att):
            return (isinstance(att, dict) and att.get("type") in ATTACHMENT_KINDS
                    and attachment_matches(att, filename, size, file_hash)
                    and file_is_here(att.get("file_path")))

        for record in records:
            if record.get("id") != message_id:
                continue
            for att in record.get("attachments") or ():
                if here(att):
                    return record, att, Path(att["file_path"])
        if file_hash:
            # A one-to-one record has a different id on each side; the content
            # hash (with the name) finds the same file in the same chat.
            for record in records:
                for att in record.get("attachments") or ():
                    if here(att) and att.get("hash") == file_hash:
                        return record, att, Path(att["file_path"])
        return None

    async def _decline(self, node_id: str, request_id: str, reason: str, **extra) -> None:
        try:
            await self.manager.p2p_manager.send_message_to_peer(node_id, {
                "command": FILE_REQUEST_DECLINED,
                "payload": {"request_id": request_id, "reason": reason, **extra},
            })
        except Exception as e:  # noqa: BLE001 - the requester times out instead
            logger.debug("Could not answer FILE_REQUEST %s to %s: %s", request_id[:8], node_id[:20], e)

    async def handle_request(self, node_id: str, payload: Dict[str, Any]) -> None:
        request_id = _short_field(payload.get("request_id"))
        if not request_id:
            return
        if self._too_many(node_id):
            await self._decline(node_id, request_id, BUSY_REASON, retry_after_sec=60)
            return
        found = self._served_attachment(node_id, payload)
        if found is None:
            logger.info("FILE_REQUEST %s from %s: not served", request_id[:8], node_id[:20])
            await self._decline(node_id, request_id, NOT_HELD_REASON)
            return
        record, att, path = found
        # After the lookup, never before: only a requester this node may serve
        # meets the wait, so `busy` here tells nobody else what this node holds.
        wait = self._same_file_wait(node_id, path)
        if wait is not None:
            logger.info("FILE_REQUEST %s from %s: %s was offered to it within %ds; busy",
                        request_id[:8], node_id[:20], att.get("filename"), int(SAME_FILE_INTERVAL_S))
            await self._decline(node_id, request_id, BUSY_REASON, retry_after_sec=wait)
            return
        group_id = payload.get("group_id")
        kind = att.get("type")
        image_metadata = None
        voice_metadata = None
        if kind == "image":
            image_metadata = {
                "dimensions": att.get("dimensions") or {},
                "thumbnail_base64": att.get("thumbnail") or "",
                "source": "request",
            }
        elif kind == "voice" and isinstance(att.get("voice_metadata"), dict):
            voice_metadata = att["voice_metadata"]
        from .file_transfer_manager import MaxConcurrentTransfersExceeded
        try:
            await self.manager.send_file(
                node_id=node_id,
                file_path=path,
                image_metadata=image_metadata,
                voice_metadata=voice_metadata,
                group_id=group_id,
                message_id=record.get("id") if group_id else None,
                sender_node_id=record.get("sender_node_id") if group_id else None,
                filename=att.get("filename"),
                request_id=request_id,
                # The author's description the record holds, byte for byte (ADR-047 phase 1)
                file_description=_held_description(att),
            )
            self._served[(node_id, str(path))] = time.monotonic()
            logger.info("FILE_REQUEST %s from %s: offering %s", request_id[:8], node_id[:20],
                        att.get("filename"))
        except MaxConcurrentTransfersExceeded:
            await self._decline(node_id, request_id, BUSY_REASON)
        except Exception as e:  # noqa: BLE001 - one request's failure is its own answer
            logger.warning("FILE_REQUEST %s from %s: could not offer %s: %s",
                           request_id[:8], node_id[:20], att.get("filename"), e)
            await self._decline(node_id, request_id, NOT_HELD_REASON)
