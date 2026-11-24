"""Handler for VOICE_TRANSCRIPTION command - distributed voice message transcription.

A transcription is bound to one conversation and one sender (follow-up 11a). The
conversation is the group the payload names, after the membership gate, or else the
one-to-one conversation with the node that handed it over: a node that names no group
speaks for its own conversation with us and nothing else. It may attach only to a voice
record of that conversation:

- one-to-one: a record of our conversation with the sender that carries the transfer
  (both sides of a one-to-one transfer share its id), or a transfer still open with
  that sender;
- group: any voice record of that group, since any member that holds the voice message
  may transcribe it (specs/dptp_v1.md 3.12). The record is found by its message id
  (one id for every member's copy), else by this node's own transfer id, else by its
  file name when exactly one record of the group carries that name.

File names are `voice_<UTC date_time>` to the second with no node part, so a name alone
never decides across conversations, and inside a group two records of one name decide
nothing. The registry entry keeps its binding (`conversation_id`, `sender_node_id`, the
resolved `message_id`) and the history view joins on it (`transcription_for`).
"""

import time
from typing import Dict, Any, Optional, Tuple
from . import MessageHandler
from .group_access import off_the_roster, refuse_group_access

# A member's transcription can arrive before the voice record it names: the sender
# transcribes after the first FILE_COMPLETE, or a star relay forwards it, while a
# member is still downloading the file. It waits (`_park`) and lands on the record
# when the record arrives (`records_arrived`, called from the receive path and from
# the history view), within a bound so a member cannot grow this node's memory
# without limit. The bound is checked wherever the parked set is read: on a record's
# arrival, on a history read and on a new transcription, so it holds whether or not
# another transcription comes.
PARKED_PER_GROUP = 32
PARKED_MAX_AGE_SECONDS = 6 * 3600

# Two records of one group carry the file name a transcription names.
AMBIGUOUS = object()

# What a voice attachment shows of a transcription; the binding stays in the registry.
_SHOWN_FIELDS = ("text", "filename", "transcriber_node_id", "provider", "confidence",
                 "language", "timestamp", "success", "remote_provider_node_id")


def _text_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _voice_attachments(record: Any):
    if not isinstance(record, dict):
        return
    for attachment in record.get("attachments") or ():
        if isinstance(attachment, dict) and attachment.get("type") == "voice":
            yield attachment


def find_voice_record(history: Any, transfer_id: Optional[str], filename: Optional[str],
                      message_id: Optional[str]):
    """The (record, voice attachment) of one conversation a transcription names.

    By message id first, then by this node's own transfer id, then by file name when
    exactly one record carries it. Returns None when nothing matches and AMBIGUOUS
    when the name is carried by more than one record.
    """
    records = [r for r in (history or ()) if isinstance(r, dict)]
    if message_id:
        for record in records:
            if record.get("id") != message_id:
                continue
            voices = list(_voice_attachments(record))
            named = [a for a in voices if filename and a.get("filename") == filename]
            if named or voices:
                return record, (named or voices)[0]
    if transfer_id:
        for record in records:
            for attachment in _voice_attachments(record):
                if attachment.get("transfer_id") == transfer_id:
                    return record, attachment
    if filename:
        found = [(record, attachment) for record in records
                 for attachment in _voice_attachments(record)
                 if attachment.get("filename") == filename]
        if len({id(record) for record, _ in found}) > 1:
            return AMBIGUOUS
        if found:
            return found[0]
    return None


def transcription_for(registry: Dict[str, Dict[str, Any]], conversation_id: str,
                      record: Any, attachment: Any) -> Optional[Dict[str, Any]]:
    """The registry entry that belongs to this voice attachment of this conversation.

    This node's own transfer id is its own key. Otherwise only an entry bound to the
    same conversation is considered, and one resolved to a record (`message_id`)
    matches that record alone.
    """
    if not isinstance(attachment, dict) or attachment.get("type") != "voice":
        return None
    transfer_id = attachment.get("transfer_id")
    filename = attachment.get("filename")
    record_id = record.get("id") if isinstance(record, dict) else None
    own = registry.get(transfer_id) if transfer_id else None
    if isinstance(own, dict) and own.get("conversation_id") in (None, conversation_id):
        return own
    by_name = None
    for entry in registry.values():
        if not isinstance(entry, dict) or entry.get("conversation_id") != conversation_id:
            continue
        bound_to = entry.get("message_id")
        if bound_to and record_id:
            if bound_to == record_id:
                return entry
            continue
        if transfer_id and entry.get("transfer_id") == transfer_id:
            return entry
        if by_name is None and filename and entry.get("filename") == filename:
            by_name = entry
    return by_name


async def settle_parked_transcriptions(service: Any, conversation_id: str) -> None:
    """Land the transcriptions parked for this conversation whose voice records are here
    now, and drop the ones past their age limit (`VoiceTranscriptionHandler.records_arrived`).

    A function of the service rather than a method: the history view is also run on
    service doubles that hold no handler.
    """
    handler = getattr(service, "voice_transcription_handler", None)
    if handler is None:
        return
    try:
        await handler.records_arrived(conversation_id)
    except Exception as e:  # noqa: BLE001 - a view or a transcription must not fail on it
        handler.logger.error("Could not settle parked transcriptions of %s: %s",
                             conversation_id, e, exc_info=True)


def served_voice_attachment(service: Any, conversation_id: str, record: Any,
                            attachment: Dict[str, Any]) -> Dict[str, Any]:
    """A voice attachment (a copy) as the history view serves it: with the transcription
    that belongs to this record of this conversation (`transcription_for`), if any."""
    if attachment.get("type") != "voice":
        return attachment
    entry = transcription_for(getattr(service, "_voice_transcriptions", None) or {},
                              conversation_id, record, attachment)
    if entry:
        attachment["transcription"] = {
            "text": entry.get("text", ""),
            "provider": entry.get("provider", ""),
            "transcriber_node_id": entry.get("transcriber_node_id", ""),
            "confidence": entry.get("confidence"),
            "language": entry.get("language", ""),
            "timestamp": entry.get("timestamp", ""),
        }
    return attachment


class VoiceTranscriptionHandler(MessageHandler):
    """Handles VOICE_TRANSCRIPTION messages (peer sharing transcription result).

    Parked transcriptions live in this object only: they do not survive a restart.
    """

    def __init__(self, service):
        super().__init__(service)
        # conversation id -> {registry key: (parked_at, the registry entry)}, oldest first.
        self._parked: Dict[str, Dict[str, Tuple[float, Dict[str, Any]]]] = {}
        self._clock = time.monotonic

    @property
    def command_name(self) -> str:
        return "VOICE_TRANSCRIPTION"

    async def handle(self, sender_node_id: str, payload: Dict[str, Any]) -> Optional[Any]:
        """Store a peer's transcription on the voice record it belongs to, and show it.

        Args:
            sender_node_id: Node that handed the transcription over
            payload: Contains transfer_id, transcription_text, provider, etc.
        """
        transfer_id = payload.get("transfer_id")
        transcription_text = payload.get("transcription_text", "")
        transcriber_node_id = payload.get("transcriber_node_id")
        group_id = payload.get("group_id")  # Present for group voice messages (v0.21.0+)

        if transcriber_node_id is not None and not isinstance(transcriber_node_id, str):
            self.logger.warning(
                "Dropped VOICE_TRANSCRIPTION from %s: its transcriber_node_id is a %s, not a node id",
                sender_node_id[:20], type(transcriber_node_id).__name__,
            )
            return None
        if group_id is not None and not isinstance(group_id, str):
            self.logger.warning("Dropped VOICE_TRANSCRIPTION from %s: its group_id is not a string",
                                sender_node_id[:20])
            return None
        if group_id and not await self._may_annotate_group_voice(sender_node_id, payload):
            return None
        if not isinstance(transfer_id, str) or not transfer_id:
            self.logger.warning("Dropped VOICE_TRANSCRIPTION from %s: it names no transfer id",
                                sender_node_id[:20])
            return None
        conversation_id = group_id or sender_node_id
        await self.records_arrived(conversation_id)

        # 1.5. Validate transcription is not empty (v0.13.3+ - prevent empty/malicious transcriptions)
        transcription_text = transcription_text.strip() if isinstance(transcription_text, str) else ""
        if not transcription_text:
            self.logger.warning(
                f"Received empty transcription from {sender_node_id} for {transfer_id}, ignoring"
            )
            return None  # Don't store empty transcriptions

        provider = payload.get("provider", "unknown")
        confidence = payload.get("confidence", 0.0)
        language = payload.get("language", "unknown")
        self.logger.info(
            f"VOICE_TRANSCRIPTION from {sender_node_id} for transfer {transfer_id} "
            f"(provider: {provider}, language: {language}, confidence: {confidence})"
        )

        filename = _text_or_none(payload.get("filename"))
        message_id = _text_or_none(payload.get("message_id"))
        monitor = self.service.conversation_monitors.get(conversation_id)
        found = find_voice_record(getattr(monitor, "message_history", None),
                                  transfer_id, filename, message_id)
        if found is AMBIGUOUS:
            self.logger.warning(
                "Dropped VOICE_TRANSCRIPTION %s in %s: more than one voice record is named %s",
                transfer_id[:12], conversation_id[:20], filename,
            )
            return None
        if found is None and not group_id and not self._open_transfer_with(sender_node_id, transfer_id):
            # A node that names no group speaks for its one-to-one conversation with us,
            # and that conversation holds no such voice message.
            self.logger.warning(
                "Dropped VOICE_TRANSCRIPTION %s from %s: no voice message of our conversation "
                "with it carries that transfer", transfer_id[:12], sender_node_id[:20],
            )
            return None

        transcription_data = {
            "text": transcription_text,
            # One message, one transfer per recipient: the file name and the message
            # id name the message on a node that holds no transfer for it.
            "filename": filename,
            "transcriber_node_id": transcriber_node_id,
            "provider": provider,
            "confidence": confidence,
            "language": language,
            "timestamp": payload.get("timestamp"),
            "success": True,  # Peer's transcription is considered successful
            # The binding: which conversation's record it may join, and who said so.
            "conversation_id": conversation_id,
            "sender_node_id": sender_node_id,
            "transfer_id": transfer_id,
            "message_id": (found[0].get("id") if found else None) or message_id,
        }
        remote_provider_node_id = payload.get("remote_provider_node_id")
        if remote_provider_node_id:
            transcription_data["remote_provider_node_id"] = remote_provider_node_id

        # 1. Duplicate: this conversation already has a successful transcription of
        # that transfer or of that record. Another conversation's entry is no reason.
        key = self._key_for(conversation_id, transfer_id)
        registry = self.service._voice_transcriptions
        held = registry.get(key)
        if held and held.get("success", False):
            self.logger.debug(f"Already have successful transcription for {transfer_id}, ignoring duplicate")
            return None
        if found:
            on_record = transcription_for(registry, conversation_id, found[0], found[1])
            if on_record and on_record.get("success", False):
                self.logger.debug(f"Voice record of {transfer_id} is already transcribed, ignoring")
                return None
        if held:
            self.logger.info(f"Replacing failed transcription for {transfer_id} with peer's transcription")

        # 2. Store transcription locally
        registry[key] = transcription_data

        # 3. Attach it to its record, or park it until the record arrives (group only:
        # a one-to-one transfer still open has its record written on FILE_COMPLETE).
        if found:
            self._attach(monitor, found, transcription_data)
        elif group_id:
            self._park(conversation_id, key, transcription_data)

        # 4. Broadcast to UI: the conversation it belongs to, so the chat window
        # updates that chat only.
        await self._show(transcription_data)

        # 5. Relay to group members that can't reach the transcriber directly (star topology)
        if group_id:
            dedup_key = f"vt:{transfer_id}"
            if dedup_key not in self.service._processed_message_ids:
                self.service._processed_message_ids.add(dedup_key)
                await self._relay_to_group("VOICE_TRANSCRIPTION", payload, sender_node_id, group_id)

        self.logger.debug(f"Processed VOICE_TRANSCRIPTION for {transfer_id}")
        return None

    async def records_arrived(self, conversation_id: str) -> None:
        """Land the parked transcriptions whose voice records are here now; drop the old ones.

        Called when a voice record may have arrived (the receive path, through
        `_maybe_transcribe_voice_message`), when the history is read, and when a new
        transcription arrives. An entry whose record is not here past
        PARKED_MAX_AGE_SECONDS is dropped. A landed entry leaves the bound: it is one per
        record, and a second transcription of a record is a duplicate.
        """
        parked = self._parked.get(conversation_id)
        if not parked:
            return
        now = self._clock()
        monitor = self.service.conversation_monitors.get(conversation_id)
        history = getattr(monitor, "message_history", None)
        waiting = {id(entry) for _, entry in parked.values()}
        settled = {k: v for k, v in self.service._voice_transcriptions.items() if id(v) not in waiting}
        landed = []
        for key, (parked_at, entry) in list(parked.items()):
            found = find_voice_record(history, entry.get("transfer_id"), entry.get("filename"),
                                      entry.get("message_id"))
            if found is None:
                if now - parked_at > PARKED_MAX_AGE_SECONDS:
                    self.logger.info(
                        "Dropped parked VOICE_TRANSCRIPTION %s in %s: no voice record arrived "
                        "within %d s", str(entry.get("transfer_id"))[:12], conversation_id,
                        PARKED_MAX_AGE_SECONDS,
                    )
                    self._forget(conversation_id, key, evict=True)
                continue
            if found is AMBIGUOUS or transcription_for(settled, conversation_id, found[0], found[1]):
                # Two records of that name, or the record has a transcription already.
                self._forget(conversation_id, key, evict=True)
                continue
            entry["message_id"] = found[0].get("id") or entry.get("message_id")
            self._attach(monitor, found, entry)
            self._forget(conversation_id, key, evict=False)
            settled[key] = entry
            landed.append(entry)
        for entry in landed:
            self.logger.info("Parked VOICE_TRANSCRIPTION %s landed on its voice record in %s",
                             str(entry.get("transfer_id"))[:12], conversation_id)
            await self._show(entry)

    async def _show(self, entry: Dict[str, Any]) -> None:
        await self.service.local_api.broadcast_event("voice_transcription_received", {
            **entry,
            "node_id": entry.get("sender_node_id"),
        })

    def _key_for(self, conversation_id: str, transfer_id: str) -> str:
        """The registry key this conversation's transcription of `transfer_id` lives under.

        The bare transfer id unless another conversation's entry holds it: a transfer id
        claimed under one conversation must not silence the same id under another.
        """
        held = self.service._voice_transcriptions.get(transfer_id)
        if held is None or held.get("conversation_id") in (None, conversation_id):
            return transfer_id
        return f"{conversation_id}/{transfer_id}"

    def _open_transfer_with(self, peer: str, transfer_id: str) -> bool:
        """A one-to-one transfer with this peer that is still open here."""
        manager = getattr(self.service, "file_transfer_manager", None)
        transfer = (getattr(manager, "active_transfers", None) or {}).get(transfer_id)
        return (transfer is not None and getattr(transfer, "node_id", None) == peer
                and not getattr(transfer, "group_id", None))

    def _attach(self, monitor: Any, found: Tuple[Dict[str, Any], Dict[str, Any]],
                entry: Dict[str, Any]) -> None:
        """Put the transcription on its record and into the extraction buffers' message."""
        _, attachment = found
        attachment["transcription"] = {
            k: entry[k] for k in _SHOWN_FIELDS if entry.get(k) is not None
        }
        self.logger.debug("Attached transcription to voice message in conversation history")
        transfer_ids = {t for t in (attachment.get("transfer_id"), entry.get("transfer_id")) if t}
        for message_list in (getattr(monitor, "message_buffer", None) or (),
                             getattr(monitor, "full_conversation", None) or ()):
            for msg_obj in message_list:
                if getattr(msg_obj, "attachment_transfer_id", None) in transfer_ids:
                    # Format: "Voice message: [transcription text]"
                    msg_obj.text = f"Voice message: {entry.get('text', '')}"
                    break

    @staticmethod
    def _names_voice(attachment: Dict[str, Any], transfer_id, filename) -> bool:
        """Whether this attachment is the voice message a transcription names.

        The sender's own group record carries no transfer id: one message
        becomes one transfer per recipient. The file name is minted once and
        travels with the offer. An absent key matches nothing.
        """
        if not isinstance(attachment, dict) or attachment.get("type") != "voice":
            return False
        return bool((transfer_id and attachment.get("transfer_id") == transfer_id)
                    or (filename and attachment.get("filename") == filename))

    def _forget(self, conversation_id: str, key: str, evict: bool) -> None:
        """Stop tracking a parked transcription; `evict` also removes it from the registry,
        unless a later transcription (ours, or a replacement) took the slot."""
        _, entry = self._parked[conversation_id].pop(key)
        if evict and self.service._voice_transcriptions.get(key) is entry:
            del self.service._voice_transcriptions[key]
        if not self._parked[conversation_id]:
            del self._parked[conversation_id]

    def _park(self, conversation_id: str, key: str, entry: Dict[str, Any]) -> None:
        parked = self._parked.setdefault(conversation_id, {})
        parked.pop(key, None)  # a replacement restarts the wait
        while len(parked) >= PARKED_PER_GROUP:
            oldest = next(iter(parked))
            self.logger.warning(
                "Dropped the oldest parked VOICE_TRANSCRIPTION %s in %s: %d are waiting for "
                "their voice records", str(oldest)[:12], conversation_id, PARKED_PER_GROUP,
            )
            self._forget(conversation_id, oldest, evict=True)
            parked = self._parked.setdefault(conversation_id, {})
        parked[key] = (self._clock(), entry)
        self.logger.info(
            "Parked VOICE_TRANSCRIPTION %s in %s: its voice record is not here yet",
            str(entry.get("transfer_id"))[:12], conversation_id,
        )

    async def _may_annotate_group_voice(self, sender_node_id: str, payload: Dict[str, Any]) -> bool:
        """Whether a group transcription may be stored, shown and relayed.

        A transcription is text put into a group, so it is a member's
        (`group_access.off_the_roster`): the node handing it over and the
        transcriber it names must both be on our roster. Any member that holds
        the voice message may transcribe it (specs/dptp_v1.md 3.12). The voice
        record need not be here yet: the transcription is parked until it is
        (`_park`). It must name a transfer, which is the key it is kept under.
        """
        group_id = payload.get("group_id")
        transfer_id = payload.get("transfer_id")
        group = self.service.group_manager.get_group(group_id)
        if group is None:
            self.logger.warning(
                "Dropped VOICE_TRANSCRIPTION %s from %s: this node does not hold group %s",
                str(transfer_id)[:12], sender_node_id[:20], str(group_id)[:20],
            )
            return False
        transcriber = payload.get("transcriber_node_id") or sender_node_id
        outsider = off_the_roster(group, sender_node_id, transcriber)
        if outsider == sender_node_id:
            await refuse_group_access(
                self.service.p2p_manager, sender_node_id, group_id, "VOICE_TRANSCRIPTION", self.logger,
            )
            return False
        if outsider:
            self.logger.warning(
                "Dropped VOICE_TRANSCRIPTION %s in group %s relayed by %s: its transcriber %s is "
                "not a member", str(transfer_id)[:12], group_id, sender_node_id[:20], str(outsider)[:20],
            )
            return False
        if not isinstance(transfer_id, str) or not transfer_id:
            self.logger.warning(
                "Dropped VOICE_TRANSCRIPTION in group %s from %s: it names no transfer id",
                group_id, sender_node_id[:20],
            )
            return False
        return True
