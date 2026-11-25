"""An agent's own past tool calls, read back in a bounded form.

Each assistant message in history.json and in a session archive keeps the
tool calls of its turn (`tool_calls`: tool, input, output, is_error, round,
round_text; written by loop.py). They never leave the owner node (peers get
only `tool_calls_digest`, ADR-042), so reading them back is the agent reading
its own record. One output can be megabytes, so every view here is cut to a
fixed size and says what it cut.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

ARGS_CHARS = 300
OUTPUT_CHARS = 300
CALLS_PER_MESSAGE = 20


def _name(call: Dict[str, Any]) -> str:
    return str(call.get("tool") or call.get("name") or "?")


def _args_text(call: Dict[str, Any]) -> str:
    args = call.get("input")
    if args is None:
        args = call.get("args", call.get("arguments"))
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [{len(text) - limit} more chars cut]"


def calls_of(message: Any) -> List[Dict[str, Any]]:
    """The message's tool calls that are dicts; [] when it has none."""
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    if not isinstance(calls, list):
        return []
    return [c for c in calls if isinstance(c, dict)]


def bounded_calls(message: Any, args_chars: int = ARGS_CHARS,
                  output_chars: int = OUTPUT_CHARS,
                  max_calls: int = CALLS_PER_MESSAGE) -> Dict[str, Any]:
    """{"calls": [...], "omitted": n} — each call as name, args, output, round,
    is_error, with args and output cut to the given sizes."""
    calls = calls_of(message)
    shown = []
    for call in calls[:max_calls]:
        output = call.get("output")
        output = "" if output is None else (output if isinstance(output, str) else str(output))
        item: Dict[str, Any] = {
            "name": _name(call),
            "args": _cut(_args_text(call), args_chars),
            "output": _cut(output, output_chars),
            "round": call.get("round"),
        }
        if call.get("is_error"):
            item["is_error"] = True
        shown.append(item)
    return {"calls": shown, "omitted": max(0, len(calls) - max_calls)}


def tool_call_lines(message: Any) -> List[str]:
    """chat_history's lines for one message's tool calls; [] when it has none."""
    view = bounded_calls(message)
    lines = []
    for c in view["calls"]:
        round_str = f" r{c['round']}" if c.get("round") is not None else ""
        err = " ERROR" if c.get("is_error") else ""
        lines.append(f"  [TOOL{round_str}{err}] {c['name']}({c['args']}) -> {c['output']}")
    if view["omitted"]:
        lines.append(f"  [TOOL] … {view['omitted']} more tool calls in this message not shown")
    return lines


def search_text(message: Any) -> Optional[str]:
    """Tool names and arguments of one message, one call per line, for search.
    Outputs are left out: they are large and are what the call got, not what it did."""
    lines = [f"{_name(c)} {_args_text(c)}".strip() for c in calls_of(message)]
    text = "\n".join(line for line in lines if line)
    return text or None


def counts_by_name(message_list: Any) -> Dict[str, int]:
    """How many times each tool was called across `message_list`."""
    counts: Dict[str, int] = {}
    for m in message_list if isinstance(message_list, list) else []:
        for c in calls_of(m):
            counts[_name(c)] = counts.get(_name(c), 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
