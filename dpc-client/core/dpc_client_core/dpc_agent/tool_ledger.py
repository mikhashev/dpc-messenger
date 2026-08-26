"""The tool ledger: what was attempted, and what came back.

A call used to leave one line, written after it returned. A call that never
returned left nothing, so "called and hung" and "never called" were the same
bytes on disk. The attempt row makes the second one countable.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, Iterator, List, Optional

from .utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)

PHASE_ATTEMPT = "attempt"
PHASE_OUTCOME = "outcome"

_swept_dirs: set = set()


def is_outcome(row: Dict[str, Any]) -> bool:
    """True for every row a reader of finished calls should count.

    Rows written before the field existed carry no phase and are outcomes.
    """
    return row.get("phase") != PHASE_ATTEMPT


def record_attempt(
    logs_dir: pathlib.Path,
    *,
    tool: str,
    tool_call_id: str,
    args: Dict[str, Any],
    task_id: str = "",
    round_number: int = 0,
    session_id: str = "",
) -> Dict[str, Any]:
    """Write the row that says the call was made, before it is made."""
    row = {
        "ts": utc_now_iso(),
        "phase": PHASE_ATTEMPT,
        "tool": tool,
        "tool_call_id": tool_call_id,
        "task_id": task_id,
        "args": args,
        "round": round_number,
        "pid": os.getpid(),
    }
    if session_id:
        row["session_id"] = session_id
    append_jsonl(logs_dir / "tools.jsonl", row)
    return row


def _ledger_paths(logs_dir: pathlib.Path) -> List[pathlib.Path]:
    rotated = sorted(logs_dir.glob("tools.jsonl.*"), reverse=True)
    return [p for p in [*rotated, logs_dir / "tools.jsonl"] if p.exists()]


def _iter_rows(path: pathlib.Path) -> Iterator[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def unfinished_calls(
    logs_dir: pathlib.Path, *, exclude_pid: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Attempts with no outcome beside them, oldest first.

    Rotation can drop an attempt row but never invent one, so the answer errs
    towards silence. `exclude_pid` is how a call still in flight is told apart
    from an abandoned one; a liveness probe would not do, because pids are
    reused, and a second live process over the same agent would read as dead.
    """
    attempts: Dict[str, Dict[str, Any]] = {}
    finished: set = set()
    for path in _ledger_paths(logs_dir):
        for row in _iter_rows(path):
            call_id = row.get("tool_call_id")
            if not call_id:
                continue
            if row.get("phase") == PHASE_ATTEMPT:
                attempts[call_id] = row
            else:
                finished.add(call_id)
    open_calls = [
        row
        for call_id, row in attempts.items()
        if call_id not in finished
        and (exclude_pid is None or row.get("pid") != exclude_pid)
    ]
    open_calls.sort(key=lambda r: str(r.get("ts", "")))
    return open_calls


def close_abandoned(logs_dir: pathlib.Path, row: Dict[str, Any]) -> None:
    """Give an abandoned attempt the outcome its own process never wrote."""
    append_jsonl(logs_dir / "tools.jsonl", {
        "ts": utc_now_iso(),
        "phase": PHASE_OUTCOME,
        "tool": row.get("tool", ""),
        "tool_call_id": row.get("tool_call_id", ""),
        "task_id": row.get("task_id", ""),
        "args": row.get("args", {}),
        "result_preview": "⚠️ TOOL_ABANDONED: the process that made this call did not return",
        "is_error": True,
        "error_category": "never_returned",
        "round": row.get("round", 0),
        "attempt_ts": row.get("ts", ""),
        "attempt_pid": row.get("pid"),
        "recorded_by": "sweep",
    })


def sweep_unfinished(logs_dir: pathlib.Path) -> List[Dict[str, Any]]:
    """Report and close the calls a dead process left open. Once per dir."""
    key = str(logs_dir)
    if key in _swept_dirs:
        return []
    _swept_dirs.add(key)
    try:
        rows = unfinished_calls(logs_dir, exclude_pid=os.getpid())
    except Exception:
        log.debug("tool ledger: sweep of %s failed", logs_dir, exc_info=True)
        return []
    for row in rows:
        log.warning(
            "Tool call never returned: %s (task=%s, pid=%s, attempted at %s)",
            row.get("tool") or "?", row.get("task_id") or "-",
            row.get("pid"), row.get("ts") or "?",
        )
        append_jsonl(logs_dir / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "tool_call_never_returned",
            "task_id": row.get("task_id", ""),
            "tool": row.get("tool", ""),
            "tool_call_id": row.get("tool_call_id", ""),
            "attempt_ts": row.get("ts", ""),
            "pid": row.get("pid"),
        })
        close_abandoned(logs_dir, row)
    return rows
