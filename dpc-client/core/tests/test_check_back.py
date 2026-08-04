"""An agent may hand itself the next wake-up, but only a few times, and only once at a time.

`reminder` re-sends text without an LLM, so an agent that says "I'll check the
render in 20 minutes" and picks it gets an echo nobody acts on. `check_back`
runs the model. The two guards protect different failures: the depth cap stops
an agent postponing forever, the overlap guard stops ten identical wake-ups.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.tools.core import _CHECK_BACK_MAX_DEPTH, schedule_task

CONV = "group-work"


class _NoApproval:
    """Firewall stub for an agent exempted from the queue gate."""

    def get_tool_setting(self, _profile, _tool, _key):
        return False


def _ctx(depth=0, queued=(), firewall=None):
    queue = SimpleNamespace(_queue=list(queued))
    scheduled = []

    def _schedule(task_type, data, priority, delay_seconds):
        task = SimpleNamespace(
            id=f"task-{len(scheduled)}", task_type=task_type, data=data,
            status="pending", scheduled_at=None,
        )
        scheduled.append(task)
        queue._queue.append(task)
        return task

    agent = SimpleNamespace(queue=queue, schedule_task=_schedule, _task_handlers={})
    return SimpleNamespace(
        _agent=agent, current_task_id=CONV, check_back_depth=depth,
        reply_telegram_chat_id=None, _scheduled=scheduled,
        firewall=firewall if firewall is not None else _NoApproval(),
    )


def _pending(conv=CONV, tid="task-existing"):
    return SimpleNamespace(
        id=tid, task_type="check_back", status="pending",
        data={"_reply_conversation_id": conv}, scheduled_at="2026-08-04T12:00:00",
    )


def test_a_first_check_back_is_accepted_and_carries_its_depth():
    ctx = _ctx()
    out = schedule_task(ctx, "check_back", '{"text": "look at the render"}', delay_seconds=1200)

    assert out.startswith("✓")
    assert ctx._scheduled[0].data["_check_back_depth"] == 1
    assert ctx._scheduled[0].data["_reply_conversation_id"] == CONV


def test_the_chain_stops_at_the_cap():
    """An agent that will never finish has to say so instead of postponing."""
    ctx = _ctx(depth=_CHECK_BACK_MAX_DEPTH)

    out = schedule_task(ctx, "check_back", '{"text": "again"}', delay_seconds=60)

    assert out.startswith("⚠️")
    assert "limit" in out
    assert not ctx._scheduled, "the wake-up was queued despite the cap"


def test_depth_comes_from_the_context_not_from_the_model():
    """A depth the model could pass is a depth the model can reset."""
    ctx = _ctx(depth=_CHECK_BACK_MAX_DEPTH)

    out = schedule_task(
        ctx, "check_back", '{"text": "again", "_check_back_depth": 0}', delay_seconds=60
    )

    assert out.startswith("⚠️"), "a payload field overrode the cap"


def test_a_second_wake_up_for_the_same_conversation_is_refused():
    ctx = _ctx(queued=[_pending()])

    out = schedule_task(ctx, "check_back", '{"text": "check again"}', delay_seconds=60)

    assert out.startswith("⚠️")
    assert "task-existing" in out
    assert not ctx._scheduled


def test_another_conversation_is_not_blocked():
    """The guard is per conversation — one busy chat must not freeze the others."""
    ctx = _ctx(queued=[_pending(conv="group-other")])

    out = schedule_task(ctx, "check_back", '{"text": "check"}', delay_seconds=60)

    assert out.startswith("✓")


def test_a_plain_chat_task_is_unaffected():
    """Neither guard may leak onto the task types that had no problem."""
    ctx = _ctx(depth=_CHECK_BACK_MAX_DEPTH, queued=[_pending()])

    out = schedule_task(ctx, "chat", '{"text": "unrelated"}', delay_seconds=0)

    assert out.startswith("✓")
    assert "_check_back_depth" not in ctx._scheduled[0].data


# ── the digest ──────────────────────────────────────────────────────────
# The guards above are worth nothing if the agent never learns the type
# exists. schedule_task was enabled for a month and used once.

def test_the_agent_sees_its_own_pending_wake_ups(tmp_path):
    import json
    from dpc_client_core.dpc_agent.context import _deferred_tasks_digest

    state = tmp_path / "state"
    state.mkdir()
    (state / "task_queue.json").write_text(json.dumps({"tasks": [
        {"id": "t2", "task_type": "check_back", "status": "pending",
         "scheduled_at": "2026-08-04T13:00:00", "data": {"text": "later one"}},
        {"id": "t1", "task_type": "check_back", "status": "pending",
         "scheduled_at": "2026-08-04T12:00:00", "data": {"text": "проверить рендер"}},
        {"id": "t0", "task_type": "chat", "status": "completed", "data": {"text": "done"}},
    ]}), encoding="utf-8")

    digest = _deferred_tasks_digest(tmp_path)

    assert [d["id"] for d in digest] == ["t1", "t2"], "not ordered by deadline"
    assert digest[0]["about"] == "проверить рендер"
    assert all(d["id"] != "t0" for d in digest), "a finished task is not pending"


def test_no_queue_means_no_section(tmp_path):
    from dpc_client_core.dpc_agent.context import _deferred_tasks_digest
    assert _deferred_tasks_digest(tmp_path) is None


def test_the_executor_passes_the_depth_through():
    """Wiring: the cap is only real if the depth reaches the tool context."""
    import inspect
    from dpc_client_core.dpc_agent.agent import DpcAgent

    src = inspect.getsource(DpcAgent._execute_task)
    assert 'task.task_type == "check_back"' in src, "the type is not handled at all"
    assert "check_back_depth=depth" in src, "depth never reaches process()"

    proc = inspect.getsource(DpcAgent.process)
    assert "check_back_depth=check_back_depth" in proc, "depth never reaches ToolContext"


# ── the approval gate ───────────────────────────────────────────────────
# Mike: approval happens before anything enters the queue, so a person sees
# what was planned and for when. Fail-closed by construction.


def test_by_default_an_agent_must_ask_before_queueing():
    """No firewall opinion means ask — a new agent is not silently autonomous."""
    ctx = _ctx(firewall=None)
    ctx.firewall = None  # nothing configured at all
    ctx.dpc_service = None

    out = schedule_task(ctx, "check_back", '{"text": "check"}', delay_seconds=60)

    assert out.startswith("⚠️")
    assert not ctx._scheduled, "queued without anyone approving"


def test_an_unanswerable_request_is_a_refusal_not_a_wait():
    """The headless web-auth lesson: broadcasting to nobody can only time out."""
    from types import SimpleNamespace as NS

    class _AskingFirewall:
        def get_tool_setting(self, *_a):
            return True

    ctx = _ctx(firewall=_AskingFirewall())
    ctx.dpc_service = NS(local_api=NS(has_clients=lambda: False))

    out = schedule_task(ctx, "check_back", '{"text": "check"}', delay_seconds=60)

    assert "no UI client is connected" in out
    assert not ctx._scheduled


def test_an_exempt_agent_queues_without_a_prompt():
    ctx = _ctx()  # _NoApproval stub

    out = schedule_task(ctx, "check_back", '{"text": "check"}', delay_seconds=60)

    assert out.startswith("✓")


def test_resolving_an_unknown_request_is_not_a_crash():
    from dpc_client_core.dpc_agent.tools.core import resolve_schedule_approval
    assert resolve_schedule_approval("nope", True) is False
