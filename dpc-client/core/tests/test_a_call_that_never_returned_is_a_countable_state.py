"""A call that hangs forever must not read as a call that was never made.

Every failure path already wrote a row: the exception path, the timeout path and
the normal one. The case with no row at all is the call whose process died with
it — the 25 August incident, where a shell call held the interpreter for nine and
a half hours and left the log empty. One row written before the call turns that
silence into an attempt with no outcome, which can be counted.
"""
import asyncio
import json
import os

import pytest

from dpc_client_core.dpc_agent import tool_ledger
from dpc_client_core.dpc_agent.tool_ledger import (
    is_outcome,
    record_attempt,
    sweep_unfinished,
    unfinished_calls,
)


@pytest.fixture
def logs_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    tool_ledger._swept_dirs.discard(str(d))
    return d


def _rows(path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _outcome(logs_dir, call_id, *, tool="run_shell", name="tools.jsonl"):
    row = {
        "ts": "2026-08-26T10:00:01+00:00",
        "phase": "outcome",
        "tool": tool,
        "tool_call_id": call_id,
        "is_error": False,
    }
    with open(logs_dir / name, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _kill_the_process_that_wrote(logs_dir):
    """Rewrite the recorded pid so the attempt reads as left by a dead process."""
    path = logs_dir / "tools.jsonl"
    path.write_text(
        path.read_text(encoding="utf-8").replace(str(os.getpid()), "424242"),
        encoding="utf-8",
    )


# --- the row itself ---


def test_the_attempt_row_names_the_call_and_the_process(logs_dir):
    record_attempt(logs_dir, tool="run_shell", tool_call_id="c1", args={"cmd": "ls"})
    (row,) = _rows(logs_dir / "tools.jsonl")
    assert row["phase"] == "attempt"
    assert row["tool_call_id"] == "c1"
    assert row["pid"] == os.getpid()


def test_a_row_written_before_the_field_existed_counts_as_an_outcome():
    assert is_outcome({"tool": "read_file", "args": {"path": "a.md"}}) is True
    assert is_outcome({"phase": "attempt", "tool": "read_file"}) is False


# --- the pairing ---


def test_an_attempt_with_its_outcome_beside_it_is_not_unfinished(logs_dir):
    record_attempt(logs_dir, tool="run_shell", tool_call_id="c1", args={})
    _outcome(logs_dir, "c1")
    assert unfinished_calls(logs_dir) == []


def test_an_attempt_with_no_outcome_is_reported(logs_dir):
    record_attempt(
        logs_dir, tool="run_shell", tool_call_id="c1", args={"cmd": "sleep 99999"}
    )
    (open_call,) = unfinished_calls(logs_dir)
    assert open_call["tool_call_id"] == "c1"
    assert open_call["args"]["cmd"] == "sleep 99999"


def test_a_call_in_flight_in_this_process_is_not_reported_as_abandoned(logs_dir):
    record_attempt(logs_dir, tool="run_shell", tool_call_id="c1", args={})
    assert unfinished_calls(logs_dir, exclude_pid=os.getpid()) == []
    assert len(unfinished_calls(logs_dir, exclude_pid=os.getpid() + 1)) == 1


def test_rotation_between_the_attempt_and_the_outcome_does_not_invent_a_hang(logs_dir):
    """The attempt lands in the rotated file, the outcome in the live one."""
    with open(logs_dir / "tools.jsonl.1", "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2026-08-26T09:59:59+00:00",
                    "phase": "attempt",
                    "tool": "run_shell",
                    "tool_call_id": "c1",
                    "pid": 4242,
                }
            )
            + "\n"
        )
    _outcome(logs_dir, "c1")
    assert unfinished_calls(logs_dir) == []


def test_rows_from_before_the_ledger_are_not_read_as_open_calls(logs_dir):
    with open(logs_dir / "tools.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-08-01T00:00:00+00:00", "tool": "read_file"}) + "\n")
    assert unfinished_calls(logs_dir) == []


# --- the reader ---


def test_the_sweep_names_the_abandoned_call_and_closes_it(logs_dir):
    record_attempt(logs_dir, tool="run_shell", tool_call_id="c1", args={"cmd": "x"})
    _kill_the_process_that_wrote(logs_dir)

    reported = sweep_unfinished(logs_dir)
    assert [r["tool_call_id"] for r in reported] == ["c1"]

    events = _rows(logs_dir / "events.jsonl")
    assert [e["type"] for e in events] == ["tool_call_never_returned"]
    assert events[0]["tool"] == "run_shell"

    closing = [r for r in _rows(logs_dir / "tools.jsonl") if r.get("recorded_by") == "sweep"]
    assert len(closing) == 1
    assert closing[0]["error_category"] == "never_returned"
    assert unfinished_calls(logs_dir) == []


def test_the_same_abandoned_call_is_not_reported_twice(logs_dir):
    record_attempt(logs_dir, tool="run_shell", tool_call_id="c1", args={})
    _kill_the_process_that_wrote(logs_dir)
    sweep_unfinished(logs_dir)

    tool_ledger._swept_dirs.discard(str(logs_dir))  # as if a later process started
    assert sweep_unfinished(logs_dir) == []
    assert len(_rows(logs_dir / "events.jsonl")) == 1


def test_the_sweep_runs_once_per_directory_per_process(logs_dir):
    record_attempt(logs_dir, tool="run_shell", tool_call_id="c1", args={})
    _kill_the_process_that_wrote(logs_dir)
    assert len(sweep_unfinished(logs_dir)) == 1
    assert sweep_unfinished(logs_dir) == []


# --- the writer, through the code that actually calls it ---


class _FakeRegistry:
    def __init__(self, result="ok"):
        self.result = result

    def execute(self, fn_name, args, ctx=None):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _call(fn_name="run_shell", arguments='{"cmd": "ls"}'):
    return {"id": "call-1", "function": {"name": fn_name, "arguments": arguments}}


def test_a_completed_call_leaves_an_attempt_and_an_outcome_that_pair(logs_dir):
    from dpc_client_core.dpc_agent.loop import _execute_with_timeout

    asyncio.run(
        _execute_with_timeout(
            _FakeRegistry(), _call(), logs_dir, timeout_sec=10, task_id="t1"
        )
    )
    rows = _rows(logs_dir / "tools.jsonl")
    assert [r["phase"] for r in rows] == ["attempt", "outcome"]
    assert rows[0]["tool_call_id"] == rows[1]["tool_call_id"] == "call-1"
    assert unfinished_calls(logs_dir) == []


def test_a_call_that_raised_still_pairs(logs_dir):
    from dpc_client_core.dpc_agent.loop import _execute_with_timeout

    asyncio.run(
        _execute_with_timeout(
            _FakeRegistry(RuntimeError("boom")), _call(), logs_dir, timeout_sec=10
        )
    )
    assert unfinished_calls(logs_dir) == []
    assert [r["phase"] for r in _rows(logs_dir / "tools.jsonl")] == ["attempt", "outcome"]


def test_unparsable_arguments_leave_an_outcome_rather_than_a_hanging_attempt(logs_dir):
    """This path used to write no row at all, which the ledger would now misread."""
    from dpc_client_core.dpc_agent.loop import _execute_with_timeout

    asyncio.run(
        _execute_with_timeout(
            _FakeRegistry(), _call(arguments="{not json"), logs_dir, timeout_sec=10
        )
    )
    rows = _rows(logs_dir / "tools.jsonl")
    assert [r["phase"] for r in rows] == ["attempt", "outcome"]
    assert rows[1]["error_category"] == "tool_arg_error"
    assert unfinished_calls(logs_dir) == []


def test_a_call_that_timed_out_is_not_a_call_that_vanished(logs_dir):
    from dpc_client_core.dpc_agent.loop import _execute_with_timeout

    class _Slow(_FakeRegistry):
        def execute(self, fn_name, args, ctx=None):
            import time

            time.sleep(1.5)
            return "late"

    result = asyncio.run(_execute_with_timeout(_Slow(), _call(), logs_dir, timeout_sec=1))
    assert "TOOL_TIMEOUT" in result["result"]
    timeout_rows = [
        r for r in _rows(logs_dir / "tools.jsonl") if r.get("error_category") == "timeout"
    ]
    assert len(timeout_rows) == 1
    assert timeout_rows[0]["tool_call_id"] == "call-1"
    assert timeout_rows[0]["args"] == {"cmd": "ls"}


# --- the readers that were already there ---


def test_the_attempt_row_does_not_double_a_documents_read_count(tmp_path):
    from dpc_client_core.dpc_agent.active_recall import _build_access_counts

    (tmp_path / "state").mkdir()
    (tmp_path / "logs").mkdir()
    with open(tmp_path / "logs" / "tools.jsonl", "a", encoding="utf-8") as f:
        for phase, ts in (("attempt", "10:00:00"), ("outcome", "10:00:01")):
            f.write(
                json.dumps(
                    {
                        "ts": f"2026-08-26T{ts}+00:00",
                        "phase": phase,
                        "tool": "read_file",
                        "tool_call_id": "c1",
                        "args": {"path": "knowledge/a.md"},
                    }
                )
                + "\n"
            )

    counts = _build_access_counts(tmp_path)
    assert counts.reads_for({"source_file": "knowledge/a.md"}) == 1
