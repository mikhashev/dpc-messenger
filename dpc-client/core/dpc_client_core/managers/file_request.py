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

The automatic version (fetch after sync, on reconnect, on being added) is card
A-NODE-THAT-SYNCS-A-RECORD-NEVER-FETCHES-THE-FILE-IT-LACKS, under the epic of
ADR-047; it is meant to call `FileRequests.request` too.
"""

from __future__ import annotations

import asyncio
import logging
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

# The UI event and its states.
FILE_REQUEST_STATUS_EVENT = "file_request_status"
STATE_REQUESTING = "requesting"
STATE_RECEIVING = "receiving"
STATE_RECEIVED = "received"
STATE_NOT_HELD = "not_held"     # every peer asked answered that it does not serve the file
STATE_NO_ANSWER = "no_answer"   # nobody offered it and at least one peer never answered
STATE_FAILED = "failed"         # an offer came, and the transfer ended without the file

# How long the requester waits for one peer before asking the next; the
# precedent is SKILL_SEARCH's 10 s (skill_coordinator.py).
REQUEST_ANSWER_TIMEOUT_S = 10.0
# A request is remembered this long, so an offer that comes late is still taken.
REQUEST_MEMORY_S = 3600.0
# Requests one peer may put to this node per minute; more are answered `busy`.
REQUESTS_PER_MINUTE = 20

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
    created: float = field(default_factory=time.monotonic)
    declined: Dict[str, str] = field(default_factory=dict)  # node -> reason
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
    def _find_attachment(record: Dict[str, Any], filename: Optional[str]) -> Optional[Dict[str, Any]]:
        for att in record.get("attachments") or ():
            if not isinstance(att, dict) or att.get("type") not in ATTACHMENT_KINDS:
                continue
            if filename and att.get("filename") != filename:
                continue
            return att
        return None

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
        attachment = self._find_attachment(record, filename)
        if attachment is None:
            return {"status": "error", "message": "That message carries no file"}
        if file_is_here(attachment.get("file_path")):
            return {"status": "available", "file_path": attachment["file_path"]}

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

        size = attachment.get("size_bytes")
        request = OpenRequest(
            request_id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            group_id=conversation_id if is_group else None,
            message_id=message_id,
            filename=str(attachment.get("filename") or ""),
            size_bytes=size if isinstance(size, int) and not isinstance(size, bool) else None,
            file_hash=_real_hash(attachment.get("hash")),
            candidates=candidates,
        )
        self._open[request.request_id] = request
        await self._emit(request, STATE_REQUESTING, asking=list(candidates))
        request.task = asyncio.get_running_loop().create_task(self._ask_in_turn(request))
        logger.info("FILE_REQUEST %s for %s in %s: asking %d peer(s), author first",
                    request.request_id[:8], request.filename, conversation_id, len(candidates))
        return {"status": "requesting", "request_id": request.request_id,
                "asking": list(candidates)}

    async def _ask_in_turn(self, request: OpenRequest) -> None:
        silent: List[str] = []
        for node_id in request.candidates:
            if request.transfer_id:
                return
            request.answered.clear()
            try:
                await self.manager.p2p_manager.send_message_to_peer(node_id, {
                    "command": FILE_REQUEST, "payload": request.wire_payload(),
                })
            except Exception as e:  # noqa: BLE001 - a dropped peer is a peer that did not answer
                logger.info("FILE_REQUEST %s could not reach %s: %s",
                            request.request_id[:8], node_id[:20], e)
                silent.append(node_id)
                continue
            try:
                await asyncio.wait_for(request.answered.wait(), self.answer_timeout_s)
            except asyncio.TimeoutError:
                pass
            if request.transfer_id:
                return
            if node_id not in request.declined:
                silent.append(node_id)
        if request.transfer_id:
            return
        if silent:
            await self._emit(request, STATE_NO_ANSWER, silent=silent)
        else:
            await self._emit(request, STATE_NOT_HELD)
        logger.info("FILE_REQUEST %s for %s: %s (%d declined, %d silent)",
                    request.request_id[:8], request.filename, request.state,
                    len(request.declined), len(silent))

    async def handle_declined(self, node_id: str, payload: Dict[str, Any]) -> None:
        request = self._open.get(payload.get("request_id"))
        if request is None or node_id not in request.candidates:
            return
        reason = payload.get("reason")
        request.declined[node_id] = reason if reason in (NOT_HELD_REASON, BUSY_REASON) else NOT_HELD_REASON
        request.answered.set()

    def open_request_for_offer(self, node_id: str, payload: Dict[str, Any]) -> Optional[OpenRequest]:
        """The open request an incoming FILE_OFFER answers, or None.

        It must come from a peer the request was put to, for the same group.
        """
        request = self._open.get(payload.get("request_id"))
        if request is None or node_id not in request.candidates:
            return None
        if (payload.get("group_id") or None) != request.group_id:
            return None
        return request

    async def offer_taken(self, request: OpenRequest, node_id: str, transfer_id: str) -> None:
        request.transfer_id = transfer_id
        request.holder = node_id
        request.answered.set()
        await self._emit(request, STATE_RECEIVING, transfer_id=transfer_id, node_id=node_id)

    async def offer_refused(self, request: OpenRequest, reason: str) -> None:
        """This node's own firewall refused the offer that answers the request."""
        await self._emit(request, STATE_FAILED, reason=reason)

    async def received(self, request_id: Optional[str], attachment: Dict[str, Any]) -> None:
        request = self._open.pop(request_id, None) if request_id else None
        if request is None:
            return
        await self._emit(request, STATE_RECEIVED, attachment=dict(attachment))

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
        recent = [t for t in self._asked_by.get(node_id, []) if now - t < 60.0]
        recent.append(now)
        self._asked_by[node_id] = recent
        return len(recent) > REQUESTS_PER_MINUTE

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
            )
            logger.info("FILE_REQUEST %s from %s: offering %s", request_id[:8], node_id[:20],
                        att.get("filename"))
        except MaxConcurrentTransfersExceeded:
            await self._decline(node_id, request_id, BUSY_REASON)
        except Exception as e:  # noqa: BLE001 - one request's failure is its own answer
            logger.warning("FILE_REQUEST %s from %s: could not offer %s: %s",
                           request_id[:8], node_id[:20], att.get("filename"), e)
            await self._decline(node_id, request_id, NOT_HELD_REASON)
