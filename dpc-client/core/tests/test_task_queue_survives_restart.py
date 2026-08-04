"""A task caught mid-execution by a restart must come back, not vanish.

`mark_running` persists status="running" and `_load_queue` re-admitted only
"pending", so a crash or restart mid-task deleted the task from existence — no
retry, no failure record, nothing on the board. With check_back that means a
restart between a person's approval and the wake-up silently eats a commitment
they agreed to.
"""

import json
from datetime import datetime, timezone

import pytest

from dpc_client_core.dpc_agent.task_queue import (
    RETRY_BACKOFF_BASE_SEC, Task, TaskQueue,
)


def _queue_with(tmp_path, tasks):
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "task_queue.json").write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return TaskQueue(tmp_path)


def _row(tid, status, **extra):
    row = {
        "id": tid, "task_type": "check_back", "data": {"text": "look again"},
        "priority": "normal", "status": status,
    }
    row.update(extra)
    return row


def test_a_task_that_was_running_comes_back(tmp_path):
    q = _queue_with(tmp_path, [_row("task-1", "running", started_at="2026-08-05T00:00:00Z")])

    assert [t.id for t in q._queue] == ["task-1"], "the in-flight task was dropped"
    task = q._queue[0]
    assert task.status == "pending"
    assert task.retry_count == 1, "the recovery must be counted, or it can loop forever"
    assert task.started_at is None, "a stale start time would misreport how long it ran"


def test_a_finished_task_is_not_resurrected(tmp_path):
    q = _queue_with(tmp_path, [
        _row("done", "completed"),
        _row("dead", "failed"),
    ])
    assert q._queue == [], "a terminal task came back to life"


def test_a_retry_is_paced_instead_of_immediate(tmp_path):
    q = _queue_with(tmp_path, [])
    task = q.schedule("check_back", {"text": "x"})

    q.mark_failed(task, "provider hiccup")

    assert task.status == "pending"
    assert task.scheduled_at, "the docstring promised backoff and there was none"
    due = datetime.fromisoformat(task.scheduled_at)
    waited = (due - datetime.now(timezone.utc)).total_seconds()
    assert RETRY_BACKOFF_BASE_SEC - 5 < waited <= RETRY_BACKOFF_BASE_SEC + 5


def test_each_further_retry_waits_longer(tmp_path):
    q = _queue_with(tmp_path, [])
    task = q.schedule("check_back", {"text": "x"})

    q.mark_failed(task, "first")
    first = datetime.fromisoformat(task.scheduled_at)
    q.mark_failed(task, "second")
    second = datetime.fromisoformat(task.scheduled_at)

    assert second > first, "the second retry was not backed off further than the first"


def test_a_paced_retry_is_not_runnable_yet(tmp_path):
    """Backoff is only real if get_next honours it."""
    q = _queue_with(tmp_path, [])
    task = q.schedule("check_back", {"text": "x"})
    q.mark_failed(task, "boom")

    assert q.get_next() is None, "the backed-off task was handed straight back out"


# ── repeating is a decision, not a default ──────────────────────────────
# A task that reached a side effect before the crash repeats it on replay:
# a message posted twice, a command run twice. Which types may be repeated
# belongs next to their own definition.


def test_only_a_type_that_declared_itself_safe_comes_back(tmp_path):
    q = _queue_with(tmp_path, [
        _row("safe", "running", task_type="check_back"),
        _row("unsafe", "running", task_type="chat"),
    ])

    assert [t.id for t in q._queue] == ["safe"], "an undeclared type was repeated"


def test_the_unsafe_one_is_reported_not_just_dropped(tmp_path, caplog):
    """It must not come back, and it must not go quietly either.

    This queue keeps only live rows on disk — terminal tasks live in
    task_results/ — so "loudly" here means the log says how many were
    abandoned and why, not that the row is persisted as failed.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        _queue_with(tmp_path, [_row("unsafe", "running", task_type="chat")])

    assert "not restart_safe" in caplog.text
    assert "marked failed rather than repeated" in caplog.text


def test_an_unknown_type_is_not_assumed_safe(tmp_path):
    q = _queue_with(tmp_path, [_row("mystery", "running", task_type="something_custom")])
    assert q._queue == [], "an unknown type defaulted to repeatable"


def test_check_back_is_the_one_declared_repeatable():
    """Stated where the type lives, so a new type must answer at birth."""
    from dpc_client_core.dpc_agent.task_types import BUILTIN_TASK_TYPES

    assert BUILTIN_TASK_TYPES["check_back"].restart_safe is True
    others = [n for n, d in BUILTIN_TASK_TYPES.items() if d.restart_safe and n != "check_back"]
    assert others == [], f"these types claim to be repeatable without review: {others}"


@pytest.mark.asyncio
async def test_a_stuck_task_does_not_hold_the_processor_forever(tmp_path, monkeypatch):
    """One task that never returns used to stop every wake-up behind it."""
    import asyncio
    from dpc_client_core.dpc_agent import task_queue as tq

    monkeypatch.setattr(tq, "DEFAULT_TASK_TIMEOUT_SEC", 1)

    q = _queue_with(tmp_path, [])
    task = q.schedule("a_custom_type_with_no_declared_timeout", {"text": "x"})

    async def _never_returns(_task):
        await asyncio.sleep(30)

    import asyncio as _a
    _a.create_task(q.start_processor(_never_returns, poll_interval=0.05))
    await asyncio.sleep(2.0)
    q.stop_processor()

    assert "timed out" in (task.error or ""), "a hung task was not bounded"
