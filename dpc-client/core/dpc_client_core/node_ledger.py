"""The node ledger: one usage row per model call, written by the node that ran it.

ADR-041 D3. An agent's call, a peer's call and a gateway client's call all spend
the same node's card and the same node's vendor key, so the record of a call is
kept by the node, not by the caller. A row says who called (`caller`,
`caller_kind`), what ran (`alias`, `model`, `route`), what it took
(`prompt_tokens`, `completion_tokens`, `thinking_tokens`, `duration_s`), whose
numbers those are (`counts_source`) and what it cost (`billing`, `cost_usd`) —
priced at `started_at` by the node that made the call and never re-priced,
which is the invariant `dpc_agent/pricing.py` states for itself. A null
`cost_usd` is a call nobody priced; a zero is a price.

Two columns beyond D3's list, both optional: `task_id` and `conversation_id`.
D3's own consistency rule — the sum of a task's rows equals the
`task_complete.cost_usd` the burn series already carries — needs a join key,
and the task id is it. One caveat on today's value rather than on the column:
`agent.py` hands `run_llm_loop` the conversation id as its `task_id`, so on the
chat path a row's `task_id` is the conversation, and the join to
`task_complete` goes through `conversation_id` and the task's start and
completion timestamps until that call passes the id it minted.

Storage is `<DPC_HOME>/ledger/usage-YYYY-MM.jsonl`, one partition per month of
`started_at`. Not `dpc_agent.utils.append_jsonl`: that rotates at 5 MB by
renaming the file to `.1` and deleting the previous `.1`, and a financial
record must never lose rows that way. Nothing here renames or deletes a
partition. What is copied from it is the lock discipline — a `.lock` sibling
taken with O_CREAT|O_EXCL, stale after 10 s — and the
O_WRONLY|O_CREAT|O_APPEND write.

Append-only never truncates, so a process killed mid-write leaves at most one
torn last line. The next append starts on a fresh line so the torn one cannot
swallow it, and the reader skips a line it cannot parse rather than failing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

log = logging.getLogger(__name__)

CALLER_KINDS = ("agent", "peer", "gateway")
ROUTES = ("local", "peer")
COUNTS_SOURCES = ("ours", "engine")
BILLINGS = ("subscription", "pay_per_use")

LOCK_TIMEOUT_S = 2.0
LOCK_STALE_S = 10.0
LOCK_SLEEP_S = 0.01


def ledger_dir() -> Path:
    """Where this node keeps its ledger; `DPC_HOME` is honoured as elsewhere."""
    return Path(os.environ.get("DPC_HOME", Path.home() / ".dpc")) / "ledger"


def _count(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def usage_row(
    *,
    request_id: str,
    caller: Optional[str],
    caller_kind: str,
    alias: Optional[str],
    model: Optional[str],
    route: str,
    prompt_tokens: Any,
    completion_tokens: Any,
    thinking_tokens: Any,
    counts_source: str,
    started_at: datetime,
    duration_s: float,
    billing: str,
    cost_usd: Any,
    task_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """One row in D3's column order.

    A value outside the vocabulary is refused here rather than written: a row
    saying `caller_kind=stranger` would be read by nothing. `gateway` is
    accepted and emitted by nothing yet — it is reserved for the gateway child.
    """
    if not request_id:
        raise ValueError("a usage row needs a request_id")
    for name, value, allowed in (
        ("caller_kind", caller_kind, CALLER_KINDS),
        ("route", route, ROUTES),
        ("counts_source", counts_source, COUNTS_SOURCES),
        ("billing", billing, BILLINGS),
    ):
        if value not in allowed:
            raise ValueError(f"{name}={value!r} is not one of {allowed}")
    if started_at.tzinfo is None:
        raise ValueError("started_at must carry a timezone; the row is priced by the UTC hour")
    row: Dict[str, Any] = {
        "request_id": str(request_id),
        "caller": caller,
        "caller_kind": caller_kind,
        "alias": alias,
        "model": model,
        "route": route,
        "prompt_tokens": _count(prompt_tokens),
        "completion_tokens": _count(completion_tokens),
        "thinking_tokens": _count(thinking_tokens),
        "counts_source": counts_source,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "duration_s": round(float(duration_s), 3),
        "billing": billing,
        "cost_usd": None if cost_usd is None else float(cost_usd),
    }
    if billing == "pay_per_use" and cost_usd is None:
        log.warning("Usage row %s is pay_per_use with no cost: the price was not computed", request_id)
    if task_id:
        row["task_id"] = task_id
    if conversation_id:
        row["conversation_id"] = conversation_id
    return row


def _ends_mid_line(path: Path) -> bool:
    """True when the last byte is not a newline: the mark a killed writer leaves."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return False
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except FileNotFoundError:
        return False


@contextlib.contextmanager
def _partition_lock(path: Path) -> Iterator[None]:
    """The `.lock` sibling, taken as `append_jsonl` takes it: exclusive create,
    a stale one cleared after 10 s, and after 2 s of waiting the append goes
    ahead without it rather than losing the row."""
    lock_path = path.with_name(path.name + ".lock")
    fd = None
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > LOCK_STALE_S:
                    lock_path.unlink()
                    continue
            except OSError:
                log.debug("Could not read or clear the lock at %s", lock_path, exc_info=True)
            if time.monotonic() >= deadline:
                log.warning(
                    "Lock %s held for over %.0fs; appending without it", lock_path, LOCK_TIMEOUT_S
                )
                break
            time.sleep(LOCK_SLEEP_S)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            try:
                lock_path.unlink()
            except OSError:
                log.debug("Could not remove the lock at %s", lock_path, exc_info=True)


class NodeLedger:
    """The rows this node has written, one file per month of `started_at`."""

    def __init__(self, directory: Optional[Path] = None):
        self.directory = Path(directory) if directory is not None else ledger_dir()

    def partition_for(self, started_at: str) -> Path:
        """`usage-YYYY-MM.jsonl` for an ISO-8601 `started_at`."""
        return self.directory / f"usage-{started_at[:7]}.jsonl"

    def partitions(self) -> List[Path]:
        return sorted(self.directory.glob("usage-????-??.jsonl"))

    def append(self, row: Dict[str, Any]) -> Optional[Path]:
        """Append one row to its month's partition.

        Never raises: the call this row records has already been made and paid
        for, and a ledger fault must not turn it into a failed call. The loss
        is logged at ERROR with the request id, so it is at least findable.
        """
        try:
            path = self.partition_for(row["started_at"])
            self.directory.mkdir(parents=True, exist_ok=True)
            data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
            with _partition_lock(path):
                if _ends_mid_line(path):
                    data = b"\n" + data
                # O_BINARY where it exists: without it the Windows CRT turns the
                # newline into CRLF on the way out, and a partition written on one
                # node would not be byte-identical to one written on another.
                # Measured 2026-09-10 on this box; events.jsonl carries the same CRLF.
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
                fd = os.open(str(path), flags, 0o644)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
            return path
        except Exception:
            log.error(
                "Usage row %s was not written to the node ledger",
                row.get("request_id"), exc_info=True,
            )
            return None

    def rows(self, month: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        """Every row, oldest partition first; `month` is `YYYY-MM` for one.

        A line that does not parse is skipped and named in the log — it is the
        torn tail a killed writer left, and it says nothing about the rows
        around it.
        """
        paths = [self.directory / f"usage-{month}.jsonl"] if month else self.partitions()
        for path in paths:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    for number, line in enumerate(f, 1):
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            log.warning("%s:%d is not a usage row and was skipped", path, number)
                            continue
                        if isinstance(row, dict):
                            yield row
            except FileNotFoundError:
                continue


def default_ledger() -> NodeLedger:
    """The node's own ledger, resolved when asked so a moved home is honoured."""
    return NodeLedger(ledger_dir())
