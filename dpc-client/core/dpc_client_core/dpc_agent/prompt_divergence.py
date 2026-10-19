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
from typing import Any, Dict, List, Optional, Tuple

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
    """The last request seen per conversation and agent, kept in memory (bounded)."""

    def __init__(self, max_conversations: int = _MAX_CONVERSATIONS) -> None:
        self._max = max_conversations
        self._last: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()

    def observe(
        self,
        conversation_id: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        reasoning_effort: Optional[str],
        round_idx: int = 0,
        agent_id: str = "",
    ) -> Dict[str, Any]:
        """Compare with the last request this agent sent in this conversation.

        Keyed by both: in a group chat several agents share one conversation,
        and each one's prompt is a different prefix in the engine's cache.
        Logs nothing: the caller hands the result to `log_observation`.
        """
        fingerprints = [message_fingerprint(m) for m in messages]
        tools_digest = _digest(tools) if tools else None
        key = (conversation_id, agent_id)
        previous = self._last.get(key)
        if previous is None:
            # Nothing to compare with — not a rewrite at message 0.
            first_diff = None
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
        self._last[key] = {
            "fingerprints": fingerprints, "tools": tools_digest, "effort": reasoning_effort,
        }
        self._last.move_to_end(key)
        while len(self._last) > self._max:
            self._last.popitem(last=False)
        return {
            "conversation_id": conversation_id,
            "round": round_idx,
            "agent_id": agent_id,
            "first_diff_msg": first_diff,
            "messages": len(fingerprints),
            "tools_changed": tools_changed,
            "effort_changed": effort_changed,
            "first_request": previous is None,
            "prefix_rewritten": rewritten,
        }


def log_observation(seen: Optional[Dict[str, Any]], request_id: Optional[str]) -> None:
    """The divergence line for one request, named by its usage row's `request_id`.

    Written once the call has returned rather than when it was observed: the
    adapter knows the row's id only then, and on the peer route the id is the
    wire's, which arrives with the answer. `request_id=-` is a call that left
    no row (it raised, or was killed); `seen` None was never measured.
    """
    if seen is None:
        return
    if seen["first_request"]:
        shown = "first"
    else:
        shown = "none" if seen["first_diff_msg"] is None else str(seen["first_diff_msg"])
    log.info(
        "prompt divergence: conv=%s round=%s first_diff_msg=%s/%d tools_changed=%s "
        "effort_changed=%s agent=%s request_id=%s",
        seen["conversation_id"], seen["round"], shown, seen["messages"], seen["tools_changed"],
        seen["effort_changed"], seen["agent_id"] or "-", request_id or "-",
    )


TRACKER = PromptDivergenceTracker()
