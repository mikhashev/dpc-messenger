"""What the runtime appended to a user message when it was sent, kept beside the history.

The per-turn block (Active Recall hints, runtime state, recent activity) rides at the
tail of the current user message rather than ahead of the history, so the prompt of the
next turn is a pure append to the prompt of this one — the only shape an engine that
caches by prefix can reuse. That only holds if the tail is replayed byte-identically the
next time the message is rendered as history, and the tail cannot be recomputed: it holds
a clock, a spend counter and the results of a search for that one query.

The history itself keeps what the human wrote — the UI and every other reader of
history.json see the conversation, not the runtime's annotations. This file holds the
rest, keyed by the message id the history already carries.

One file per conversation, per agent: `<agent_root>/state/sent_annotations/<conv>.json`.
Every write prunes ids that are no longer in the live history, so a reset or a trimmed
conversation does not leave annotations pointing at messages that are gone.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
from typing import Dict, Iterable, Optional

from .index_meta import atomic_write_text

log = logging.getLogger(__name__)

_VERSION = 1
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(conversation_id: str) -> str:
    return _SAFE.sub("_", conversation_id or "conversation") or "conversation"


class SentAnnotationStore:
    def __init__(self, agent_root: pathlib.Path):
        self.root = pathlib.Path(agent_root) / "state" / "sent_annotations"

    def path_for(self, conversation_id: str) -> pathlib.Path:
        return self.root / f"{_safe_name(conversation_id)}.json"

    def load(self, conversation_id: str) -> Dict[str, str]:
        """message id -> tail text, as sent. Unreadable file reads as empty: a missing
        annotation costs one cold prefill, a crash here would cost the turn."""
        path = self.path_for(conversation_id)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("sent_annotations unreadable, treating as empty: %s", path)
            return {}
        raw = data.get("annotations") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, str)}

    def record(
        self,
        conversation_id: str,
        message_id: str,
        tail: str,
        live_message_ids: Optional[Iterable[str]] = None,
    ) -> Dict[str, str]:
        """Remember `tail` for `message_id` and drop annotations for messages that
        are no longer in the history. `live_message_ids` is the history as it stands
        now; None means "do not prune" (caller could not read the history)."""
        annotations = self.load(conversation_id)
        annotations[message_id] = tail
        if live_message_ids is not None:
            live = {str(i) for i in live_message_ids if i}
            live.add(message_id)
            dropped = [k for k in annotations if k not in live]
            for k in dropped:
                del annotations[k]
            if dropped:
                log.debug("sent_annotations: pruned %d stale entries for %s",
                          len(dropped), conversation_id)
        self._write(conversation_id, annotations)
        return annotations

    def prune(self, conversation_id: str, live_message_ids: Iterable[str]) -> int:
        """Drop everything not in the live history; returns how many went."""
        annotations = self.load(conversation_id)
        if not annotations:
            return 0
        live = {str(i) for i in live_message_ids if i}
        dropped = [k for k in annotations if k not in live]
        for k in dropped:
            del annotations[k]
        if dropped:
            self._write(conversation_id, annotations)
        return len(dropped)

    def _write(self, conversation_id: str, annotations: Dict[str, str]) -> None:
        path = self.path_for(conversation_id)
        if not annotations:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.debug("sent_annotations: could not remove empty file %s", path)
            return
        atomic_write_text(path, json.dumps(
            {"version": _VERSION, "annotations": annotations},
            ensure_ascii=False, indent=1,
        ))
