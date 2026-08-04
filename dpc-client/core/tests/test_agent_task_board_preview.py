"""The task board must describe the tasks an agent deferred for itself.

The preview read `message` / `task` / `prompt`. The chat handler reads `text`
(`agent.py._execute_task`), so a task the agent scheduled with the documented
field arrived on the board with an empty preview — blank on exactly the tasks
the board exists to show.
"""

import json

import pytest

from dpc_client_core.agent_service import AgentService

AGENT = "agent_preview_test"


def _queue(tmp_path, tasks):
    root = tmp_path / AGENT
    (root / "state").mkdir(parents=True)
    (root / "state" / "task_queue.json").write_text(
        json.dumps({"tasks": tasks}), encoding="utf-8"
    )
    return root


@pytest.fixture
def _agent_root(tmp_path, monkeypatch):
    def _make(tasks):
        root = _queue(tmp_path, tasks)
        monkeypatch.setattr(
            "dpc_client_core.dpc_agent.utils.get_agent_root", lambda _id=None: root
        )
        return root
    return _make


def _task(data, tid="task-1"):
    return {
        "id": tid, "task_type": "chat", "data": data, "status": "pending",
        "scheduled_at": "2026-08-04T12:00:00", "started_at": None, "completed_at": None,
    }


async def _scheduled(svc):
    return (await AgentService.get_agent_tasks(svc, AGENT))["scheduled"]


@pytest.mark.asyncio
async def test_a_task_the_agent_deferred_shows_what_it_is(_agent_root):
    """`text` is what the chat handler reads, so it is what an agent writes."""
    _agent_root([_task({"text": "проверить рендер prompt_id=6af11012"})])

    entries = await _scheduled(AgentService.__new__(AgentService))

    assert entries and entries[0]["preview"] == "проверить рендер prompt_id=6af11012"


@pytest.mark.asyncio
async def test_the_ui_scheduling_form_still_wins(_agent_root):
    """`message` is what the board's own form writes; it must keep priority."""
    _agent_root([_task({"message": "from the form", "text": "from the agent"})])

    entries = await _scheduled(AgentService.__new__(AgentService))

    assert entries[0]["preview"] == "from the form"


@pytest.mark.asyncio
async def test_a_task_with_no_known_field_is_not_a_crash(_agent_root):
    _agent_root([_task({"something_else": 1})])

    entries = await _scheduled(AgentService.__new__(AgentService))

    assert entries[0]["preview"] == ""
