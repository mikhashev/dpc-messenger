# dpc-protocol/dpc_protocol/message_signing.py
"""Canonical preimage for a signed DPTP message.

A signature is only worth what it covers. The first preimage in this project
covered four fields joined by "|", which left `group_id` (replay a message
into another group), `sender_name` (rename the author), `sender_type` (present
a human as an agent) and `tool_calls` (rewrite an agent's audit trail) outside
it — each forgeable while the signature still verified.

Two properties are deliberate here:

**Length prefixes, not separators.** ``"|".join`` was injective only because
the field count was fixed: four fields always meant three separators, so no
field could absorb the boundary of the next. That is a property of the field
count, not of the encoding — the first optional field would have ended it
silently. ``len(bytes) + ":" + bytes`` is injective on its own terms, so the
field set can grow without anyone re-deriving the argument.

**One implementation, both ends.** Signer and verifier that each build the
preimage from their own view of the fields will eventually disagree about the
spelling of the same message, and every disagreement rejects an honest one.
Timestamp normalisation is the likeliest place — Python writes UTC as
``+00:00``, other stacks write ``Z``, and both are the same instant — so it is
absorbed here rather than left to whoever writes the caller.

See specs/dptp_v1.md §4.1.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

PREIMAGE_VERSION = "dptp-msg-v1"

__all__ = ["PREIMAGE_VERSION", "message_preimage", "message_content_hash"]


def _canonical_timestamp(timestamp: Optional[str]) -> str:
    """The same instant, spelled one way, whatever the sender's platform.

    An unparseable value is passed through rather than dropped: it is still
    part of the message, and silently excluding it would put a field outside
    the signature — the exact failure this module exists to end.
    """
    if not timestamp:
        return ""
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical_json(value: Any) -> str:
    """Structured fields, spelled one way.

    Sorted keys and tight separators so that two nodes holding the same call
    produce the same bytes; ``ensure_ascii=False`` so the bytes are the UTF-8
    the rest of the preimage already uses.
    """
    if value is None or value == [] or value == {}:
        return ""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def message_preimage(
    *,
    conversation_id: Optional[str],
    message_id: Optional[str],
    sender_node_id: Optional[str],
    sender_name: Optional[str] = None,
    sender_type: Optional[str] = None,
    agent_owner: Optional[str] = None,
    timestamp: Optional[str] = None,
    content: str = "",
    tool_calls: Any = None,
) -> bytes:
    """Build the exact bytes a message signature covers.

    Keyword-only on purpose: a positional call site that later drifts out of
    order would keep signing happily and produce hashes nobody can reproduce.

    Field order is part of the format and must not be reordered. New fields
    append, and appending requires a new PREIMAGE_VERSION — the version tag
    opens the preimage so signatures never cross field sets.
    """
    fields = [
        PREIMAGE_VERSION,
        conversation_id or "",
        message_id or "",
        sender_node_id or "",
        sender_name or "",
        sender_type or "",
        agent_owner or "",
        _canonical_timestamp(timestamp),
        content or "",
        _canonical_json(tool_calls),
    ]

    out = bytearray()
    for field in fields:
        encoded = field.encode("utf-8")
        out += f"{len(encoded)}:".encode("utf-8")
        out += encoded
    return bytes(out)


def message_content_hash(**fields: Any) -> str:
    """SHA256 of the canonical preimage, hex — the value that gets signed."""
    return hashlib.sha256(message_preimage(**fields)).hexdigest()
