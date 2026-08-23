"""Nothing bounded the number of tasks an agent could queue.

`TaskQueue.schedule` appended, sorted by priority and saved to disk; no caller
counted, and the queue is reloaded at start, so a runaway did not need one long
session to accumulate — it survived restarts. `clear_completed()` removes only
finished ones and is called by hand.

The shipped example config carries `max_concurrent_tasks = 3`, which is read by
nothing in the tree and describes execution rather than the queue; the processor
runs one task at a time regardless. So the only number a reader could find was
both unenforced and about something else.

The ceiling refuses rather than drops: both callers turn the exception into a
message — the tool answers «Error scheduling task: …», the WebSocket command
answers a status of error — so the agent that asked is told.
"""

import pytest

from dpc_client_core.dpc_agent.task_queue import TaskQueue, TaskPriority


def _queue(tmp_path, **kw):
    return TaskQueue(tmp_path, **kw)


def _fill(q, n, task_type="chat"):
    for i in range(n):
        q.schedule(task_type=task_type, data={"i": i})


def test_the_queue_refuses_once_it_is_full(tmp_path):
    q = _queue(tmp_path, max_pending=3)
    _fill(q, 3)

    with pytest.raises(RuntimeError) as exc:
        q.schedule(task_type="chat", data={})

    assert "full" in str(exc.value).lower()
    # The message has to carry both numbers, or the agent reading it cannot tell
    # whether to wait or to cancel something.
    assert "3" in str(exc.value)


def test_up_to_the_ceiling_is_accepted(tmp_path):
    q = _queue(tmp_path, max_pending=3)
    _fill(q, 3)
    assert q.get_stats()["total_pending"] == 3


def test_finishing_a_task_makes_room_again(tmp_path):
    """The ceiling counts what is *waiting*, not what has ever been scheduled —
    otherwise a long-lived agent would seize up after fifty tasks in its life."""
    q = _queue(tmp_path, max_pending=2)
    _fill(q, 2)

    task = q.get_next()
    q.mark_running(task)
    q.mark_complete(task, result="done")

    q.schedule(task_type="chat", data={})   # must not raise
    assert q.get_stats()["total_pending"] == 2


def test_a_running_task_does_not_occupy_a_pending_slot(tmp_path):
    """A task the processor has picked up is no longer waiting. Counting it as
    pending would make the effective ceiling one lower than it says."""
    q = _queue(tmp_path, max_pending=1)
    _fill(q, 1)
    q.mark_running(q.get_next())

    q.schedule(task_type="chat", data={})   # the slot is free again
    assert q.get_stats()["total_pending"] == 1


def test_the_default_is_far_above_anything_observed(tmp_path):
    """Fifty is chosen from the logs: across 382 process starts the queue held 0
    pending 377 times and 1 pending 5 times, never more. The ceiling exists for
    a runaway, not for ordinary use, and a default that ordinary use can reach
    would be a worse bug than the one it fixes."""
    q = _queue(tmp_path)
    assert q.max_pending == TaskQueue.DEFAULT_MAX_PENDING == 50


def test_a_ceiling_can_be_switched_off_explicitly(tmp_path):
    """Zero means no ceiling, so the old behaviour is still expressible — but it
    has to be asked for by name rather than being what you get by saying nothing."""
    q = _queue(tmp_path, max_pending=0)
    _fill(q, 60)
    assert q.get_stats()["total_pending"] == 60


def test_the_ceiling_survives_a_restart_because_the_queue_does(tmp_path):
    """The queue is persisted and reloaded; a cap that only applied in the
    session that filled it would miss the case this entry is actually about."""
    first = _queue(tmp_path, max_pending=2)
    _fill(first, 2)

    second = TaskQueue(tmp_path, max_pending=2)
    assert second.get_stats()["total_pending"] == 2
    with pytest.raises(RuntimeError):
        second.schedule(task_type="chat", data={})
