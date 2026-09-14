"""The node ledger: one usage row per model call, written by the node that ran it.

ADR-041 D3. An agent's call, a peer's call and a gateway client's call all spend
the same node's card and the same node's vendor key, so the record of a call is
kept by the node, not by the caller. A row says who called (`caller`,
`caller_kind`), what ran (`alias`, `model`, `route`), what it took
(`prompt_tokens`, `completion_tokens`, `thinking_tokens`, `duration_s`), whose
numbers those are (`counts_source`), whether `completion_tokens` already holds
the reasoning (`output_includes_thinking`: `includes` | `excludes` | `unknown`,
set where the count was made; a row written before the column reads as
`unknown`), where the thinking count came from (`thinking_source`: `engine` |
`estimated` | null for a node that said nothing; under `includes` the count is
clamped to `completion_tokens` and marked `estimated` rather than written
larger than the total it is inside), at which reasoning effort the call actually ran (`served_effort`:
one word of the shared scale off/low/medium/high/max, the host's word after its
clamp — on the host's own row from the clamp, on the requester's row from the
wire; None means no effort control was applied, which is not `off`, and a row
written before the column reads as None), whether the node at the other end of
the call had its key proved and over which tier (`peer_proved`,
`peer_connection_type`; see `usage_row` for what «proved» means on each side)
and what it cost (`billing`, `cost_usd`) —
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

Four more, optional and travelling as one group, with a fifth beside them:
`tariff_in`, `tariff_out`, `tariff_currency`, `tariff_at` — the applied values of the owner's tariff
(`compute.serving_tariff`) at the moment of the call, frozen with their
currency, because rows are forever and the declaration is not: the rates per
1M tokens, the ISO 4217 unit they are in, and the `from` day of the entry that
applied. Absent is «not declared», the gift; zero is «declared free»; more is
paid (ADR-041 D3, amendment). `cost_usd` beside them is the host's own cost
and stays USD — what the call cost this node, not what it charges for it.
`tariff_amount` is what those rates came to on this call's own counts, in
`tariff_currency` — computed by `tariff_amount_for` at write time and never
again — or null where the counts' convention is unknown and nothing may be
billed from them.

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
import math
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .firewall import ISO_4217_CODES, parse_iso_date

log = logging.getLogger(__name__)

CALLER_KINDS = ("agent", "peer", "gateway")
ROUTES = ("local", "peer")
COUNTS_SOURCES = ("ours", "engine")
OUTPUT_INCLUDES_THINKING = ("includes", "excludes", "unknown")
# Where `thinking_tokens` came from: the engine reported the split, or the node
# that wrote the row estimated it from the reasoning text. None is «nobody said»
# — an older row, a node with no word for it, or a call with no reasoning at all.
THINKING_SOURCES = ("engine", "estimated")
# The tariff as it travels: four applied values that go together and the amount
# they came to. Named once here, where the columns live, and read by every site
# that copies the group from the wire onto a row.
TARIFF_FIELDS = ("tariff_in", "tariff_out", "tariff_currency", "tariff_at", "tariff_amount")


def stated_output_includes_thinking(value: Any, *, peer: str, log: logging.Logger) -> str:
    """The word a peer sent, when it is one of the three; `unknown` for anything
    else, with one WARNING naming the peer and the value. Checked where the wire
    is read: `usage_row` refuses a fourth word, and a refusal there would cost the
    requester its row on every call while the answer was still delivered."""
    if value in OUTPUT_INCLUDES_THINKING:
        return value
    log.warning(
        "Peer %s sent output_includes_thinking=%r, which is none of %s; the row says unknown",
        peer, value, "/".join(OUTPUT_INCLUDES_THINKING),
    )
    return "unknown"


def stated_thinking_source(value: Any, *, peer: str, log: logging.Logger) -> Optional[str]:
    """The word a peer sent for the provenance of its thinking count, when it is
    one of the two; None for anything else, with one WARNING naming the peer and
    the value. Absent stays absent: an older host says nothing about provenance,
    and None is that silence, not a claim that the engine counted."""
    if value is None or value in THINKING_SOURCES:
        return value
    log.warning(
        "Peer %s sent thinking_source=%r, which is neither of %s; the row says nothing",
        peer, value, "/".join(THINKING_SOURCES),
    )
    return None
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
    tariff_in: Any = None,
    tariff_out: Any = None,
    tariff_currency: Optional[str] = None,
    tariff_at: Any = None,
    tariff_amount: Any = None,
    output_includes_thinking: str = "unknown",
    thinking_source: Optional[str] = None,
    served_effort: Optional[str] = None,
    served_by: Optional[str] = None,
    peer_proved: Optional[bool] = None,
    peer_connection_type: Optional[str] = None,
) -> Dict[str, Any]:
    """One row in D3's column order.

    A value outside the vocabulary is refused here rather than written: a row
    saying `caller_kind=stranger` would be read by nothing. `gateway` is
    accepted and emitted by nothing yet — it is reserved for the gateway child.

    The four rate columns are written when `tariff_in` is given and
    then all together: half a tariff would be a price to one reader and a
    gift to another, so a group with a member missing is refused.
    `tariff_amount` is written beside them — null when the counts' convention
    is `unknown` and the arithmetic may not be done — and is refused without
    them, because its unit is `tariff_currency` and its basis is those rates.

    `thinking_source` says where `thinking_tokens` came from — `engine` when the
    vendor reported the split, `estimated` when a node derived it from the
    reasoning text — and None when nobody said, which a row written before the
    column also reads as. It is provenance, not arithmetic: `counts_source`
    already says who produced `prompt_tokens` and `completion_tokens`, and a
    provider can report exact totals with an estimated split inside them.

    Under `includes` the reasoning is inside the output count, so
    `thinking_tokens <= completion_tokens` is the arithmetic every reader does.
    An estimate is not bounded by the engine's exact total, and one that
    overflowed it reached this function from a peer on 2026-09-14 (live rows
    ab08ff95: completion 22, thinking 24). Refusing the row would lose the
    record of a call that was made and paid for, so the count is clamped to the
    completion it is inside, marked `estimated`, and the overflow is named in one
    WARNING with the request id. The invariant then holds on disk whatever a
    peer's provider does.

    `peer_proved` is what ADR-041 D2 draws its line on, written by whoever
    knows the connection this call travelled over. True means the far end's
    key was proved on that connection, and that is two different checks:
    inbound, `P2PManager._verify_hello_identity` required the certificate's CN
    to be the claimed node_id, its public key to hash to that node_id, and an
    RSA-PSS signature over a nonce this node chose to verify under it;
    outbound, `_validate_peer_certificate` required the same CN and the same
    key hash, TLS having already made the far end prove it holds the private
    half. False means a tier that runs neither — the name is then the Hub's
    assertion, this node's own intention or an envelope field. None means
    there is no far end to prove: an agent or a gateway client on this machine
    is not reached over a tier, and a row written before the column reads the
    same way. `peer_connection_type` is the connection's own word for its
    tier, which is where the answer came from.

    `served_by` is the node that ran a call this node only consumed: the host
    id on a `route=peer` row, written beside the tariff group because the two
    answer one question together — what this call was charged and by whom. It
    is optional and written only when given, like `task_id`: on a row this node
    ran itself there is no other node to name, and a null there would read as
    «served by nobody» rather than «served here». `alias` stays what it always
    was, the name the host was asked for, which is unique only under its host.
    """
    if not request_id:
        raise ValueError("a usage row needs a request_id")
    tariff = _tariff_columns(tariff_in, tariff_out, tariff_currency, tariff_at)
    if tariff:
        tariff["tariff_amount"] = _tariff_amount_column(tariff_amount)
    elif tariff_amount is not None:
        raise ValueError(
            f"tariff_amount={tariff_amount!r} without a tariff: the amount is in the row's "
            "tariff_currency and comes from its rates, so it cannot stand without them"
        )
    for name, value, allowed in (
        ("caller_kind", caller_kind, CALLER_KINDS),
        ("route", route, ROUTES),
        ("counts_source", counts_source, COUNTS_SOURCES),
        ("output_includes_thinking", output_includes_thinking, OUTPUT_INCLUDES_THINKING),
        ("billing", billing, BILLINGS),
    ):
        if value not in allowed:
            raise ValueError(f"{name}={value!r} is not one of {allowed}")
    if started_at.tzinfo is None:
        raise ValueError("started_at must carry a timezone; the row is priced by the UTC hour")
    if thinking_source is not None and thinking_source not in THINKING_SOURCES:
        raise ValueError(f"thinking_source={thinking_source!r} is not one of {THINKING_SOURCES}")
    thinking_tokens, thinking_source = _thinking_inside_the_output(
        request_id=request_id,
        completion_tokens=completion_tokens,
        thinking_tokens=thinking_tokens,
        output_includes_thinking=output_includes_thinking,
        thinking_source=thinking_source,
    )
    if served_effort is not None and not isinstance(served_effort, str):
        raise ValueError(f"served_effort={served_effort!r} is not a word of the effort scale")
    if served_by is not None and not isinstance(served_by, str):
        raise ValueError(f"served_by={served_by!r} is not the node id of the host that served the call")
    if peer_proved is not None and not isinstance(peer_proved, bool):
        raise ValueError(f"peer_proved={peer_proved!r} is not True, False or None")
    if peer_connection_type is not None and not isinstance(peer_connection_type, str):
        raise ValueError(f"peer_connection_type={peer_connection_type!r} is not a connection's word for itself")
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
        "output_includes_thinking": output_includes_thinking,
        "thinking_source": thinking_source,
        "served_effort": served_effort,
        "peer_proved": peer_proved,
        "peer_connection_type": peer_connection_type,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "duration_s": round(float(duration_s), 3),
        "billing": billing,
        "cost_usd": None if cost_usd is None else float(cost_usd),
    }
    if billing == "pay_per_use" and cost_usd is None:
        log.warning("Usage row %s is pay_per_use with no cost: the price was not computed", request_id)
    row.update(tariff)
    if served_by:
        row["served_by"] = served_by
    if task_id:
        row["task_id"] = task_id
    if conversation_id:
        row["conversation_id"] = conversation_id
    return row


def _thinking_inside_the_output(
    *,
    request_id: Any,
    completion_tokens: Any,
    thinking_tokens: Any,
    output_includes_thinking: str,
    thinking_source: Optional[str],
) -> tuple:
    """`(thinking_tokens, thinking_source)` with the `includes` invariant held.

    A thinking count larger than the output count it sits inside came from an
    estimate over the reasoning text, never from an engine that counted both, so
    it is clamped and marked for what it is. Clamped rather than refused: the row
    records a call already made and paid for, and a peer's arithmetic is not ours
    to reject. A value that will not read as an integer is left to `_count`.
    """
    if output_includes_thinking != "includes":
        return thinking_tokens, thinking_source
    try:
        completion, thinking = int(completion_tokens), int(thinking_tokens)
    except (TypeError, ValueError):
        return thinking_tokens, thinking_source
    if thinking <= completion:
        return thinking_tokens, thinking_source
    log.warning(
        "Usage row %s says completion_tokens=%d with thinking_tokens=%d inside it: "
        "the thinking count is an estimate that overflowed the count it is inside; "
        "the row is written with thinking_tokens=%d and thinking_source=estimated",
        request_id, completion, thinking, completion,
    )
    return completion, "estimated"


def _tariff_columns(tariff_in: Any, tariff_out: Any, tariff_currency: Any, tariff_at: Any) -> Dict[str, Any]:
    """The four tariff columns as a group, or an empty dict when none was given."""
    given = {
        "tariff_in": tariff_in, "tariff_out": tariff_out,
        "tariff_currency": tariff_currency, "tariff_at": tariff_at,
    }
    if all(value is None for value in given.values()):
        return {}
    missing = [name for name, value in given.items() if value is None]
    if missing:
        raise ValueError(f"a tariff is written whole or not at all; missing {missing}")
    for name in ("tariff_in", "tariff_out"):
        rate = given[name]
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate < 0:
            raise ValueError(f"{name}={rate!r} is not a non-negative number per 1M tokens")
        # The rules file refuses a non-finite rate too (`firewall._tariff_errors`);
        # this is the same refusal for a rate that arrived over the wire, where a
        # NaN passes `rate < 0` and would sit in a partition for ever.
        if not math.isfinite(rate):
            raise ValueError(f"{name}={rate!r} is not a finite number per 1M tokens")
    if tariff_currency not in ISO_4217_CODES:
        raise ValueError(f"tariff_currency={tariff_currency!r} is not an ISO 4217 code")
    if isinstance(tariff_at, date) and not isinstance(tariff_at, datetime):
        tariff_at = tariff_at.isoformat()
    if parse_iso_date(tariff_at) is None:
        raise ValueError(f"tariff_at={tariff_at!r} is not an ISO date YYYY-MM-DD")
    return {
        "tariff_in": float(tariff_in),
        "tariff_out": float(tariff_out),
        "tariff_currency": tariff_currency,
        "tariff_at": tariff_at,
    }


def _tariff_amount_column(amount: Any) -> Optional[float]:
    """The amount as it is written, or None; anything unusable is refused.

    None is «not billed» — the `unknown` state, or a call whose numbers nobody
    could price — and is written as a null so a reader can tell it from a
    declared zero. A bool, a string, a negative or a non-finite number is
    refused rather than written: a row is forever and nothing re-derives it.
    """
    if amount is None:
        return None
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValueError(f"tariff_amount={amount!r} is not a number in the row's tariff_currency")
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"tariff_amount={amount!r} is not a finite, non-negative amount")
    return float(amount)


def tariff_amount_for(
    *,
    prompt_tokens: Any,
    completion_tokens: Any,
    thinking_tokens: Any,
    output_includes_thinking: str,
    tariff_in: Any,
    tariff_out: Any,
) -> Optional[float]:
    """What a call's counts come to at the rates applied to it, or None.

    The only place this arithmetic lives, and it is called **once, at write
    time, by the node that made the call** — never again on a stored row: D3
    prices a call at `started_at`, and a reader that recomputed an amount from
    `tariff_in` and `tariff_out` would be answering a different question from
    the one the row answers.

    Reasoning is billable output at `tariff_out` and has no rate of its own
    (ADR-041 D3, amendment of 2026-09-13), so which tokens the output rate
    covers is decided by the convention of the count itself — not by
    `counts_source`, which says who produced the number, not what is inside it:

        includes -> tariff_out x completion_tokens
        excludes -> tariff_out x (completion_tokens + thinking_tokens)
        unknown  -> None: nothing is billed from a count nobody can read

    `unknown` returns None whatever the rates are, zero rates included — one
    rule rather than two. Rates of None are «no tariff declared», the v1 gift,
    and also return None, which is not the statement a declared 0.0 makes.
    The input side is always `tariff_in x prompt_tokens`.
    """
    if tariff_in is None or tariff_out is None:
        return None
    if output_includes_thinking not in ("includes", "excludes"):
        return None
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    thinking = max(0, int(thinking_tokens or 0))
    billable_out = completion if output_includes_thinking == "includes" else completion + thinking
    return (prompt * float(tariff_in) + billable_out * float(tariff_out)) / 1_000_000.0


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
                            row.setdefault("output_includes_thinking", "unknown")
                            if "tariff_in" in row:
                                # Only where a tariff applied: a row with no
                                # group has no amount column to be missing.
                                row.setdefault("tariff_amount", None)
                            if row.get("route") == "peer":
                                # Only on a consumed row: a row this node ran
                                # itself has no host to name, and the column is
                                # absent there by construction (`usage_row`).
                                # None on an older consumed row is «this node
                                # did not record which host served it».
                                row.setdefault("served_by", None)
                            row.setdefault("thinking_source", None)
                            row.setdefault("served_effort", None)
                            row.setdefault("peer_proved", None)
                            row.setdefault("peer_connection_type", None)
                            yield row
            except FileNotFoundError:
                continue

    def spent_today(
        self,
        alias: str,
        *,
        caller: Optional[str],
        caller_kind: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> float:
        """USD this caller has spent on this alias in the current UTC day.

        The first reader of the ledger, and the one a vendor quota is checked
        against (ADR-041 D5). The ceiling is per caller — each caller's own
        sum, never a total over callers — so the caller is a required
        argument even where it has one value. `caller_kind` narrows further
        when given. A null `cost_usd` is a call nobody priced and adds
        nothing; the row's `started_at` is already UTC, so the day is its
        first ten characters.
        """
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        day = moment.date().isoformat()
        total = 0.0
        for row in self.rows(month=day[:7]):
            if row.get("alias") != alias or row.get("caller") != caller:
                continue
            if caller_kind is not None and row.get("caller_kind") != caller_kind:
                continue
            if not str(row.get("started_at", "")).startswith(day):
                continue
            cost = row.get("cost_usd")
            if cost is not None:
                total += float(cost)
        return total


def owner_rows(rows: Iterator[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """This node's own vendor spend — what a burn reader wants in place of the
    provider's own usage log line (ADR-041 D3, 2026-09-13: the ledger is the
    record). One predicate, `route == "local"`: this node made the vendor call
    itself, whoever asked — its own agent, its own gateway client, or a guest
    this node served on its own key (the guest's tokens, this node's dollars;
    what the guest is charged is `tariff_amount`, not `cost_usd`). `route=peer`
    is excluded regardless of `caller_kind`: the money stayed with the node
    that ran the call. Filtered on `route`, not on `cost_usd is not None`,
    because `route` is the row's own answer to who ran the call.
    """
    for row in rows:
        if row.get("route") == "local":
            yield row


def served_rows(rows: Iterator[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """What this node ran for somebody else: `route == "local"` and
    `caller_kind == "peer"`. The owner's side of a shared call — the tokens are
    the guest's, the dollars in `cost_usd` are this node's, and what the guest
    owes for them is `tariff_amount`. A `gateway` caller is not here: the
    gateway door is local, so its client is this node's own user (D3), and its
    rows belong to `own_rows`.
    """
    for row in rows:
        if row.get("route") == "local" and row.get("caller_kind") == "peer":
            yield row


def consumed_rows(rows: Iterator[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """What another node ran for this one: `route == "peer"`, whoever here
    asked — an agent, or a gateway client of this node's own door. The guest's
    side: `cost_usd` is null by construction (this node priced nothing) and the
    tariff group is the host's copy of what it charges, so `tariff_amount` is
    what is owed and `served_by` is whom it is owed to.
    """
    for row in rows:
        if row.get("route") == "peer":
            yield row


def own_rows(rows: Iterator[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """This node's own consumption on its own hardware and its own key:
    `route == "local"` with a caller that is not a peer. The complement of
    `served_rows` inside `owner_rows`, which stays what the burn reader wants —
    every local row, this node's dollars whoever asked.
    """
    for row in rows:
        if row.get("route") == "local" and row.get("caller_kind") != "peer":
            yield row


def _parse_started_at(value: Any) -> datetime:
    """An ISO-8601 datetime, timezone-aware; raises ValueError naming `value`
    otherwise, so a malformed `since`/`until` reaches the API as a refusal."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"expected an ISO-8601 datetime string, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{value!r} is not an ISO-8601 datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _new_group_entry() -> Dict[str, Any]:
    return {
        "row_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "thinking_tokens": 0,
        "cost_usd": 0.0,
        "unpriced": 0,
        "peer_proved": {"true": 0, "false": 0, "none": 0},
        "output_includes_thinking": {"includes": 0, "excludes": 0, "unknown": 0},
    }


def _fold(bucket: Dict[str, Any], key: Any, row: Dict[str, Any]) -> None:
    # A missing group key lands under "none", matching how JSON renders a
    # None dict key, so a direct call and a round trip agree. cost_usd is
    # added as the row carries it (D3: never re-priced); a null adds to
    # unpriced instead.
    group_key = str(key) if key is not None else "none"
    entry = bucket.setdefault(group_key, _new_group_entry())
    entry["row_count"] += 1
    entry["prompt_tokens"] += row.get("prompt_tokens") or 0
    entry["completion_tokens"] += row.get("completion_tokens") or 0
    entry["thinking_tokens"] += row.get("thinking_tokens") or 0
    cost = row.get("cost_usd")
    if cost is None:
        entry["unpriced"] += 1
    else:
        entry["cost_usd"] += float(cost)
    proved = row.get("peer_proved")
    proved_key = "true" if proved is True else "false" if proved is False else "none"
    entry["peer_proved"][proved_key] += 1
    includes = row.get("output_includes_thinking")
    if includes not in OUTPUT_INCLUDES_THINKING:
        includes = "unknown"
    entry["output_includes_thinking"][includes] += 1


def _within(started_at: Any, since_dt: Optional[datetime], until_dt: Optional[datetime]) -> bool:
    """Whether `started_at` falls inside the window, both bounds inclusive.
    With no window every row is in, its timestamp unread; with a window, a row
    whose `started_at` will not parse is out."""
    if since_dt is None and until_dt is None:
        return True
    try:
        moment = _parse_started_at(started_at) if started_at else None
    except ValueError:
        moment = None
    if moment is None:
        return False
    if since_dt is not None and moment < since_dt:
        return False
    if until_dt is not None and moment > until_dt:
        return False
    return True


def _new_role_entry() -> Dict[str, Any]:
    """The role reader's group: what was spent, on whose counts, and what is
    owed for it — the last of the three `_new_group_entry` does not answer."""
    return {
        "row_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "thinking_tokens": 0,
        "duration_s": 0.0,
        "counts_source": {"ours": 0, "engine": 0},
        "peer_proved": {"true": 0, "false": 0, "none": 0},
        "cost_usd": 0.0,
        "unpriced": 0,
        "tariff": {},
        "tariff_unpriceable": 0,
        "untariffed": 0,
    }


def _fold_role(
    bucket: Dict[str, Any], key: Any, row: Dict[str, Any], **fields: Any
) -> Dict[str, Any]:
    """Add one row to its group and return the group. `fields` identify the
    group and are set only when it is created.

    Money is added as the row carries it and never re-derived (D3):
    `tariff_amount` sums per `tariff_currency`, because two currencies do not
    add, and the two states that are not an amount are counted rather than
    summed as zero — `tariff_unpriceable` is a tariff that applied over counts
    nobody could price, `untariffed` is a call with no tariff declared, the
    gift. `unpriced` does the same for a null `cost_usd`. A row whose
    `counts_source` is neither word counts into `row_count` and into neither
    side of that split.
    """
    group_key = str(key) if key is not None else "none"
    entry = bucket.get(group_key)
    if entry is None:
        entry = _new_role_entry()
        entry.update(fields)
        bucket[group_key] = entry

    entry["row_count"] += 1
    entry["prompt_tokens"] += row.get("prompt_tokens") or 0
    entry["completion_tokens"] += row.get("completion_tokens") or 0
    entry["thinking_tokens"] += row.get("thinking_tokens") or 0
    entry["duration_s"] = round(entry["duration_s"] + float(row.get("duration_s") or 0.0), 3)

    source = row.get("counts_source")
    if source in COUNTS_SOURCES:
        entry["counts_source"][source] += 1

    proved = row.get("peer_proved")
    proved_key = "true" if proved is True else "false" if proved is False else "none"
    entry["peer_proved"][proved_key] += 1

    cost = row.get("cost_usd")
    if cost is None:
        entry["unpriced"] += 1
    else:
        entry["cost_usd"] += float(cost)

    currency = row.get("tariff_currency")
    amount = row.get("tariff_amount")
    if currency is None:
        entry["untariffed"] += 1
    elif amount is None:
        entry["tariff_unpriceable"] += 1
    else:
        owed = entry["tariff"].setdefault(str(currency), {"amount": 0.0, "rows": 0})
        owed["amount"] += float(amount)
        owed["rows"] += 1
    return entry


def consumed_key(row: Dict[str, Any]) -> str:
    """`remote:<served_by>:<alias>` where the host is known, the bare alias
    where it is not: an alias is the name one host answers to, so two hosts
    serving `ollama_local` are one line only to a reader that ignores whose
    alias it is."""
    alias = row.get("alias")
    host = row.get("served_by")
    if host:
        return f"remote:{host}:{alias}"
    return str(alias) if alias is not None else "none"


def usage_by_role(
    rows: Iterator[Dict[str, Any]], *, since: Optional[str] = None, until: Optional[str] = None
) -> Dict[str, Any]:
    """The ledger read by the two sides of a shared call, three series over the
    same rows (THE-LEDGER-COUNTS-EVERY-SHARED-CALL-AND-NEITHER-SIDE-CAN-SEE-IT-
    IN-THE-UI):

    * `served` — what this node ran for peers, by the peer that asked
      (`by_caller`, each carrying its own `by_alias`) and by the alias that
      answered. `cost_usd` is what this node spent, `tariff` what it is owed.
    * `consumed` — what peers ran for this node, by `consumed_key`, each group
      echoing `node_id` and `alias` so no reader parses the key. `cost_usd` is
      null on every such row by construction, so the money here is `tariff`:
      what this node owes, per currency.
    * `own` — this node's own calls on its own key, by alias: no peer asked,
      and no peer is owed.

    Pure, like `summarize`, and windowed by the same rule.
    """
    since_dt = _parse_started_at(since) if since is not None else None
    until_dt = _parse_started_at(until) if until is not None else None

    served_by_caller: Dict[str, Any] = {}
    served_by_alias: Dict[str, Any] = {}
    consumed_by_source: Dict[str, Any] = {}
    own_by_alias: Dict[str, Any] = {}

    for row in rows:
        if not _within(row.get("started_at"), since_dt, until_dt):
            continue
        route = row.get("route")
        if route == "peer":
            _fold_role(
                consumed_by_source, consumed_key(row), row,
                node_id=row.get("served_by"), alias=row.get("alias"),
            )
        elif route == "local" and row.get("caller_kind") == "peer":
            caller = _fold_role(served_by_caller, row.get("caller"), row)
            _fold_role(caller.setdefault("by_alias", {}), row.get("alias"), row)
            _fold_role(served_by_alias, row.get("alias"), row)
        elif route == "local":
            _fold_role(own_by_alias, row.get("alias"), row)

    return {
        "since": since,
        "until": until,
        "served": {"by_caller": served_by_caller, "by_alias": served_by_alias},
        "consumed": {"by_source": consumed_by_source},
        "own": {"by_alias": own_by_alias},
    }


def summarize(
    rows: Iterator[Dict[str, Any]], *, since: Optional[str] = None, until: Optional[str] = None
) -> Dict[str, Any]:
    """The ledger's first reader (A-LEDGER-NOBODY-READS-IS-NOT-YET-AN-
    INSTRUMENT): rows folded by `caller`, by `alias` and by month of
    `started_at`. Pure — takes whatever `NodeLedger.rows()` yields and
    touches no disk. Does not fold `tariff_amount`, and must never derive one:
    the amount is written once by the node that made the call, and computing it
    here from `tariff_in`/`tariff_out` would be the re-derivation D3 forbids
    for `cost_usd`. Summing the amounts a row already carries is the reader's
    own next step, and it has to sum per currency. `since`/`until` are ISO datetimes compared as
    datetimes, both bounds inclusive; a row whose `started_at` will not
    parse is excluded from a windowed summary.
    """
    since_dt = _parse_started_at(since) if since is not None else None
    until_dt = _parse_started_at(until) if until is not None else None

    by_caller: Dict[str, Any] = {}
    by_alias: Dict[str, Any] = {}
    by_month: Dict[str, Any] = {}
    row_count = 0

    for row in rows:
        started_at = row.get("started_at")
        if not _within(started_at, since_dt, until_dt):
            continue

        row_count += 1
        month = str(started_at)[:7] if started_at else "none"
        _fold(by_caller, row.get("caller"), row)
        _fold(by_alias, row.get("alias"), row)
        _fold(by_month, month, row)

    return {
        "row_count": row_count,
        "since": since,
        "until": until,
        "by_caller": by_caller,
        "by_alias": by_alias,
        "by_month": by_month,
    }


def default_ledger() -> NodeLedger:
    """The node's own ledger, resolved when asked so a moved home is honoured."""
    return NodeLedger(ledger_dir())
