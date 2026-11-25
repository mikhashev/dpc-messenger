"""A check_back that names the agent's own unfinished music job in its text asks no approval.

Forge wrote the job id into the check_back's text instead of `music_job_id`; core did not
recognise its own job and put the approval card in front of the person (ADR-051 D49 says
a return to one's own still-queued-or-running job asks nothing). Without `music_job_id`,
a job id in the text fields now counts, under the same strict check: proposed by this
agent, still queued or running, and only one such job named. Someone else's job, a
finished one, or a `music_job_id` that fails the check is asked about as before.

Real code: the music commands, the job store, the schedule_task tool through the loop's
dispatcher and the approval round-trip (the rig of the music-tools test).
"""

import json

import pytest

from dpc_client_core.dpc_agent.tools import music as music_tools
from dpc_client_core.music import job_schemas as js
from tests._acestep_helpers import Rig, from_text
from tests.test_the_owners_agent_makes_music_through_the_music_tools_with_approval_limits_and_the_self_wait_rule import (  # noqa: F401
    MusicService, Person, QueueAgent, _clean, call, home, make_ctx, registry_for,
)


async def _setup(home, rig):
    person = Person(True)
    service = MusicService(home, rig, person)
    ctx = make_ctx(home, service)
    ctx._agent = QueueAgent(ctx.agent_root)
    reg = registry_for(ctx)
    await call(reg, ctx, "music_propose_job", params=from_text())
    [job] = rig.store.list_jobs()
    assert len(person.asked) == 1  # the job's own card
    return person, ctx, reg, job


async def _check_back(reg, ctx, data, delay=60):
    return await call(reg, ctx, "schedule_task", task_type="check_back",
                      task_data=json.dumps(data), delay_seconds=delay)


def _queued(ctx):
    return [t for t in ctx._agent.queue._queue if t.status == "pending"]


@pytest.mark.asyncio
async def test_its_own_queued_job_named_in_the_text_asks_no_approval(home):
    async with Rig(home, start_runner=False) as rig:
        person, ctx, reg, job = await _setup(home, rig)
        out = await _check_back(reg, ctx, {"text": f"check on {job['id']} and report the takes"})
        assert out.startswith("✓ Scheduled task"), out
        assert len(person.asked) == 1, "a return to its own unfinished job asked the person"
        [task] = _queued(ctx)
        assert task.data["music_job_id"] == job["id"]
        assert task.data["text"] == music_tools.music_return_text(job)


@pytest.mark.asyncio
async def test_someone_elses_job_named_in_the_text_is_asked_about(home):
    async with Rig(home, start_runner=False) as rig:
        person, ctx, reg, _job = await _setup(home, rig)
        ui_job = rig.store.create_job("generate_from_text", from_text(style="from the ui"))
        out = await _check_back(reg, ctx, {"text": f"look at {ui_job['id']}"})
        assert out.startswith("✓ Scheduled task"), out
        assert len(person.asked) == 2 and person.asked[-1]["task_type"] == "check_back"
        [task] = _queued(ctx)
        assert "music_job_id" not in task.data


@pytest.mark.asyncio
async def test_its_own_finished_job_named_in_the_text_is_asked_about(home):
    async with Rig(home, start_runner=False) as rig:
        person, ctx, reg, job = await _setup(home, rig)
        await call(reg, ctx, "music_cancel_job", job_id=job["id"])
        assert rig.store.get_job(job["id"])["state"] not in (js.QUEUED, js.RUNNING)
        out = await _check_back(reg, ctx, {"text": f"check on {job['id']}"})
        assert out.startswith("✓ Scheduled task"), out
        assert len(person.asked) == 2 and person.asked[-1]["task_type"] == "check_back"


@pytest.mark.asyncio
async def test_a_music_job_id_field_that_fails_the_check_is_not_saved_by_the_text(home):
    """`music_job_id` decides when present: its own job in the text does not overrule it."""
    async with Rig(home, start_runner=False) as rig:
        person, ctx, reg, job = await _setup(home, rig)
        ui_job = rig.store.create_job("generate_from_text", from_text(style="from the ui"))
        out = await _check_back(reg, ctx, {"music_job_id": ui_job["id"], "text": f"also {job['id']}"})
        assert out.startswith("✓ Scheduled task"), out
        assert len(person.asked) == 2


def test_an_id_inside_a_longer_token_is_not_a_job_id():
    hex16 = "0123456789abcdef"
    found = music_tools._JOB_ID_IN_TEXT_RE.findall
    assert found(f"see job_{hex16}.") == [f"job_{hex16}"]
    assert found(f"xjob_{hex16}") == [] and found(f"job_{hex16}0") == []
