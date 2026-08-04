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
