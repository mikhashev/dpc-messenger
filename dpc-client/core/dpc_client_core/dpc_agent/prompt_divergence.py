"""Where an agent request stops sharing a prefix with the request before it.

A local engine (llama-server, Strata) reuses its cache only up to the first
message that changed, so the index logged here is how much of the prompt the
next request has to re-read. A request that only appends diverges at the old
length; anything earlier, or a changed tool list or reasoning effort (both are
rendered at the head of the prompt by some chat templates), is a prefix rewrite.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_MAX_CONVERSATIONS = 256
_FINGERPRINT_KEYS = ("role", "content", "tool_calls", "tool_call_id", "thinking")


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def message_fingerprint(message: Dict[str, Any]) -> str:
    """A stable hash of what a message puts on the wire."""
    return _digest({k: message.get(k) for k in _FINGERPRINT_KEYS})


def first_divergence(previous: List[str], current: List[str]) -> Optional[int]:
    """First index where two fingerprint lists differ; None when they are equal.

    When one list is a prefix of the other the answer is the shorter length,
    so a pure append reports the previous request's length.
    """
    for i, (a, b) in enumerate(zip(previous, current)):
        if a != b:
            return i
    if len(previous) == len(current):
        return None
    return min(len(previous), len(current))


class PromptDivergenceTracker:
    """The last request seen per conversation, kept in memory (bounded)."""

    def __init__(self, max_conversations: int = _MAX_CONVERSATIONS) -> None:
        self._max = max_conversations
        self._last: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def observe(
        self,
        conversation_id: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        reasoning_effort: Optional[str],
        round_idx: int = 0,
    ) -> Dict[str, Any]:
        fingerprints = [message_fingerprint(m) for m in messages]
        tools_digest = _digest(tools) if tools else None
        previous = self._last.get(conversation_id)
        if previous is None:
            first_diff = 0 if fingerprints else None
            tools_changed = effort_changed = False
            rewritten = False
        else:
            first_diff = first_divergence(previous["fingerprints"], fingerprints)
            tools_changed = previous["tools"] != tools_digest
            effort_changed = previous["effort"] != reasoning_effort
            rewritten = (
                (first_diff is not None and first_diff < len(previous["fingerprints"]))
                or tools_changed or effort_changed
            )
        self._last[conversation_id] = {
            "fingerprints": fingerprints, "tools": tools_digest, "effort": reasoning_effort,
        }
        self._last.move_to_end(conversation_id)
        while len(self._last) > self._max:
            self._last.popitem(last=False)
        log.info(
            "prompt divergence: conv=%s round=%s first_diff_msg=%s/%d tools_changed=%s effort_changed=%s",
            conversation_id, round_idx, "none" if first_diff is None else first_diff,
            len(fingerprints), tools_changed, effort_changed,
        )
        return {
            "first_diff_msg": first_diff,
            "messages": len(fingerprints),
            "tools_changed": tools_changed,
            "effort_changed": effort_changed,
            "first_request": previous is None,
            "prefix_rewritten": rewritten,
        }


TRACKER = PromptDivergenceTracker()
