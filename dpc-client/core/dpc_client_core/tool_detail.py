"""The tool calls the UI is sent: no more than its tool row can show.

A history record keeps every tool output whole, and one output can be
megabytes (a fetched page, a file listing). The UI's expanded tool row shows
the first `EXPANDED_MAX` characters and names the rest
(`ui/src/lib/utils/toolDetail.ts`, `clampDetail`), so everything past that
travelled to the webview only to be hidden. On 2026-10-28 one group record
held 8.9 MiB of tool calls and one group answer was 19.6 MiB.

`clip_tool_calls` makes the copy the UI is sent: each output cut where
`clampDetail` cuts it, with the same notice, and the round's reasoning
(`round_text`) kept on the first call of its round only, which is the one the
UI reads. The record on disk is not touched.
"""

from __future__ import annotations

from typing import Any

# Same number as EXPANDED_MAX in ui/src/lib/utils/toolDetail.ts, counted the
# way JavaScript counts a string's length: in UTF-16 code units.
EXPANDED_MAX = 4000


def _notice(hidden: int) -> str:
    # Word for word what clampDetail appends, so the row reads the same.
    return f"\n… {hidden} more characters (full text in the agent log)"


def clip_detail(text: Any, max_len: int = EXPANDED_MAX) -> Any:
    """Cut `text` where the UI's clampDetail would, with its notice."""
    if not isinstance(text, str) or len(text) <= max_len // 2:
        return text
    units = text.encode("utf-16-le", "surrogatepass")
    total = len(units) // 2
    if total <= max_len:
        return text
    cut = max_len
    # Never end on the first half of a surrogate pair.
    last = int.from_bytes(units[2 * cut - 2:2 * cut], "little")
    if 0xD800 <= last <= 0xDBFF:
        cut -= 1
    head = units[:2 * cut].decode("utf-16-le", "surrogatepass")
    return head + _notice(total - cut)


def clip_tool_calls(calls: Any) -> Any:
    """A copy of `calls` for the UI; anything that is not a list passes as is."""
    if not isinstance(calls, list):
        return calls
    rounds_seen: set = set()
    clipped = []
    for call in calls:
        if not isinstance(call, dict):
            clipped.append(call)
            continue
        copy = dict(call)
        if "output" in copy:
            copy["output"] = clip_detail(copy["output"])
        # The UI groups by `round ?? 0` and reads round_text off the first
        # call of each group (AgentProgressCollapsible.groupByRound).
        round_key = copy.get("round")
        if round_key is None:
            round_key = 0
        try:
            first_of_round = round_key not in rounds_seen
            rounds_seen.add(round_key)
        except TypeError:
            first_of_round = True
        if not first_of_round:
            copy.pop("round_text", None)
        clipped.append(copy)
    return clipped
