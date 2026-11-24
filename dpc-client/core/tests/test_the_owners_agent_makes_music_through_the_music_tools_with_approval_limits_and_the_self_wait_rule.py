"""ADR-051 D42-D51, ADR-052 section 3.5 (music tools part 2): the owner's agent makes music
through the `music_*` tools.

`music_propose_job` asks the person first, through `schedule_task`'s approval machinery, and a
refusal makes no job; one job per turn, a repeat returns the turn's job; another turn class is
neither offered the tools nor runs them. `music_wait` follows the self-wait rule: a live card
hold on a local alias AND a queued job answers "queued, after this turn" at once; a finished
job is reported as finished whatever the hold; a cloud hold (alias None) waits as usual. Stop
ends a wait between polls, and the loop's Stop cancels the turn's queued job (D48). A turn has
a wait budget. An agent cancels only its own jobs. A check_back to its own job asks nothing.

Everything runs through the live path: `ToolRegistry` with the real modules, the loop's tool
dispatcher (`_execute_with_timeout`, which carries the spawn origin and the card hold into the
tool thread) or `run_llm_loop` itself, the real music command mixin over a real job store,
library and runner, and the runner driving `_fake_acestep_server.py` (no GPU). The person is
a stub UI surface that answers the approval card.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import pytest

from dpc_client_core.dpc_agent import guards
from dpc_client_core.dpc_agent.loop import _execute_with_timeout, run_llm_loop
from dpc_client_core.dpc_agent.task_queue import TaskQueue
from dpc_client_core.dpc_agent.tools import core as core_tools
from dpc_client_core.dpc_agent.tools import music as music_tools
from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolRegistry
from dpc_client_core.managers.llama_server_supervisor import card_held_for
from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.commands import MusicCommandsMixin, path_of_file_uri
from tests._acestep_helpers import Rig, free_env, from_text, install_fake_engine
from tests._music_job_commands_helpers import Settings

AGENT = "agent-a"
CHAT = "agent_a_chat"
DONE = (js.REVIEW, js.FAILED, js.CANCELLED)
BOUND = 20.0  # no test waits this long; a hang fails instead of stalling the suite


class Person:
    """The UI surface the approval card reaches; answers yes or no at once."""

    def __init__(self, answer=True):
        self.answer = answer
        self.asked: list = []
        self.has_clients = True

    async def broadcast_event(self, event, payload):
        pass


class MusicService(MusicCommandsMixin):
    """The real music command mixin of CoreService over the rig's store, runner and library,
    with the schedule approval's two announce methods answered by `Person`."""

    def __init__(self, home, rig, person):
        self.settings = Settings(home)
        self._music_jobs_instance = rig.store
        self._music_runner_instance = rig.runner
        self._music_library_instance = rig.library
        self.local_api = person
        self.person = person

    async def announce_schedule_approval_request(self, request_id, **kw):
        self.person.asked.append(kw)
        core_tools.resolve_schedule_approval(request_id, self.person.answer)

    async def announce_schedule_approval_closed(self, **kw):
        pass


class QueueAgent:
    """What schedule_task needs of the agent: a real task queue on disk."""

    def __init__(self, root):
        self.queue = TaskQueue(root)
        self._task_handlers = {}
        self._firewall_profile = None

    def schedule_task(self, task_type, data, priority, delay_seconds=0):
        return self.queue.schedule(task_type=task_type, data=data, priority=priority, scheduled_at=None)


@pytest.fixture(autouse=True)
def _clean():
    core_tools._pending_schedule_approvals.clear()
    yield
    core_tools._pending_schedule_approvals.clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "dpc"
    install_fake_engine(h)
    free_env(monkeypatch)
    monkeypatch.setattr(music_tools, "POLL_INTERVAL_S", 0.05)
    return h


def make_ctx(home, service, agent=AGENT, turn_class="owner"):
    root = home / "agents" / agent
    (root / "logs").mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(agent_root=root, turn_class=turn_class, message_source="ui",
                      conversation_id=CHAT, dpc_service=service)
    ctx.agent_event_loop = asyncio.get_running_loop()
    return ctx


def registry_for(ctx):
    reg = ToolRegistry(agent_root=ctx.agent_root)
    reg.set_context(ctx)
    return reg


async def call(reg, ctx, name, turn="turn-1", **args):
    """One tool call through the loop's dispatcher, as a round makes it."""
    tc = {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    out = await asyncio.wait_for(_execute_with_timeout(
        reg, tc, ctx.agent_root / "logs", reg.get_timeout(name), ctx=ctx,
        agent_id=ctx.agent_root.name, session_id=CHAT, turn_id=turn), BOUND)
    return out["result"]


def files_under(folder):
    return sorted(p.name for p in folder.rglob("*") if p.is_file()) if folder.exists() else []


# -- (a) approved, waited for, done ------------------------------------------------------


@pytest.mark.asyncio
async def test_an_approved_job_reaches_review_and_music_wait_returns_its_take_ids_and_files(home):
    async with Rig(home) as rig:
        person = Person(True)
        ctx = make_ctx(home, MusicService(home, rig, person))
        reg = registry_for(ctx)

        out = await call(reg, ctx, "music_propose_job", type="generate_from_text", params=from_text(variants=2))
        assert out.startswith("Proposed (approved by the person)."), out
        assert len(person.asked) == 1 and person.asked[0]["task_type"] == "music_job"
        [job] = rig.store.list_jobs()
        assert job["requested_by"] == {"kind": "agent", "agent_id": AGENT, "conversation_id": CHAT,
                                       "turn_id": "turn-1"}

        out = await call(reg, ctx, "music_wait", job_id=job["id"], timeout=60)
        assert out.startswith(f"job {job['id']}: review"), out
        takes = rig.store.list_takes(job["id"])
        assert len(takes) == 2
        for take in takes:
            assert take["id"] in out
            assert str(path_of_file_uri(take["file"]["uri"])) in out
        assert "lm.request.json" not in out and "lyrics" not in out  # nothing of the request (D44)


# -- (b) refused: no job ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_approval_leaves_no_job_and_no_job_file(home):
    async with Rig(home, start_runner=False) as rig:
        person = Person(False)
        ctx = make_ctx(home, MusicService(home, rig, person))
        out = await call(registry_for(ctx), ctx, "music_propose_job", params=from_text())
        assert out == "⚠️ The music job was not proposed: the user declined. No job was made."
        assert len(person.asked) == 1
        assert rig.store.list_jobs() == []
        assert files_under(home / "music" / "generated") == []


# -- (c) another turn class: not offered, not run ----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_class", ["other_node", "stranger", None])
async def test_a_turn_that_is_not_the_owners_is_not_offered_the_tools_and_makes_no_job(home, turn_class):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        ctx = make_ctx(home, MusicService(home, rig, person), turn_class=turn_class)
        reg = registry_for(ctx)
        offered = {s["function"]["name"] for s in reg.schemas(include_restricted=True)}
        assert not offered & music_tools.MUSIC_TOOL_NAMES
        assert music_tools.MUSIC_TOOL_NAMES <= reg.owner_only_names()

        out = await call(reg, ctx, "music_propose_job", params=from_text())
        assert "runs only on the owner's own turns" in out
        # The tool's own second line refuses as well, called past the registry.
        assert "runs only on the owner's own turns" in music_tools.music_propose_job(ctx, params=from_text())
        assert person.asked == [] and rig.store.list_jobs() == []
        assert files_under(home / "music" / "generated") == []


# -- (d), (e), (f) the self-wait rule ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_local_alias_hold_with_a_queued_job_answers_queued_after_this_turn_at_once(home):
    async with Rig(home, start_runner=False) as rig:  # nothing runs the job: it stays queued
        ctx = make_ctx(home, MusicService(home, rig, Person(True)))
        reg = registry_for(ctx)
        await call(reg, ctx, "music_propose_job", params=from_text())
        [job] = rig.store.list_jobs()
        async with card_held_for("strata-27b", "accepted_request", agent_id=AGENT, conversation_id=CHAT):
            began = time.monotonic()
            out = await call(reg, ctx, "music_wait", job_id=job["id"], timeout=600)
            took = time.monotonic() - began
        delay = int(round(music_tools.UNKNOWN_ETA_S + music_tools.CHECK_BACK_COLD_START_S))
        assert out.startswith(f"job {job['id']}: queued; it starts after this turn ends"), out
        assert "'strata-27b'" in out and f"check_back in about {delay} s" in out
        assert took < 2.0
        assert rig.store.get_job(job["id"])["state"] == js.QUEUED


@pytest.mark.asyncio
async def test_the_same_hold_with_a_job_in_review_returns_the_result_not_queued(home):
    async with Rig(home) as rig:
        ctx = make_ctx(home, MusicService(home, rig, Person(True)))
        reg = registry_for(ctx)
        await call(reg, ctx, "music_propose_job", params=from_text(variants=1))
        [job] = rig.store.list_jobs()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        async with card_held_for("strata-27b", "accepted_request", agent_id=AGENT, conversation_id=CHAT):
            out = await call(reg, ctx, "music_wait", job_id=job["id"])
        assert out.startswith(f"job {job['id']}: review"), out
        assert "queued" not in out
        [take] = rig.store.list_takes(job["id"])
        assert take["id"] in out


@pytest.mark.asyncio
async def test_a_cloud_model_hold_stands_on_no_alias_and_the_wait_runs_to_the_result(home):
    async with Rig(home) as rig:
        ctx = make_ctx(home, MusicService(home, rig, Person(True)))
        reg = registry_for(ctx)
        await call(reg, ctx, "music_propose_job", params=from_text(variants=1))
        [job] = rig.store.list_jobs()
        async with card_held_for(None, "accepted_request", agent_id=AGENT, conversation_id=CHAT):
            out = await call(reg, ctx, "music_wait", job_id=job["id"], timeout=60)
        assert out.startswith(f"job {job['id']}: review"), out
        assert rig.store.list_takes(job["id"])[0]["id"] in out


# -- (g) Stop during the wait, and the loop's Stop cancels the turn's queued job ------------


def _usage():
    return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}


class ScriptedLlm:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list = []

    async def chat(self, messages, **kwargs):
        self.calls.append([dict(m) for m in messages])
        step = self.steps.pop(0)
        return step(messages) if callable(step) else step


def _asks(name, args):
    return ({"role": "assistant", "content": "", "tool_calls": [
        {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
        _usage())


@pytest.mark.asyncio
async def test_stop_ends_music_wait_between_polls_and_the_turns_queued_job_is_cancelled(home, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    async with Rig(home, start_runner=False) as rig:
        ctx = make_ctx(home, MusicService(home, rig, Person(True)))
        reg = registry_for(ctx)
        stop, kill = asyncio.Event(), asyncio.Event()
        pressed = {}

        def wait_for_the_job(messages):
            job_id = re.search(r"job (job_\w+)", messages[-1]["content"]).group(1)
            return _asks("music_wait", {"job_id": job_id, "timeout": 600})

        llm = ScriptedLlm(_asks("music_propose_job", {"params": from_text()}), wait_for_the_job,
                          ({"role": "assistant", "content": "Stopped while waiting.", "tool_calls": []}, _usage()))

        def emit(message, tool=None, *rest, **kw):
            if tool == "music_wait" and "at" not in pressed:
                def press():
                    pressed["at"] = time.monotonic()
                    stop.set()
                asyncio.get_running_loop().call_later(0.3, press)

        text, _u, trace = await asyncio.wait_for(run_llm_loop(
            messages=[{"role": "user", "content": "make me a song"}], tools=reg, llm=llm,
            agent_root=ctx.agent_root, emit_progress=emit, task_id="t1", conversation_id=CHAT,
            stop_event=stop, kill_event=kill), BOUND)
        ended = time.monotonic()

        assert trace["turn_end"] == "stopped" and text == "Stopped while waiting."
        assert ended - pressed["at"] < 1.5, "the wait did not see the Stop between polls"
        waited = [m["content"] for m in llm.calls[-1] if m["role"] == "tool"][-1]
        assert "The wait ended: the turn was stopped." in waited
        [job] = rig.store.list_jobs()
        assert job["requested_by"]["turn_id"] and job["requested_by"]["agent_id"] == AGENT
        deadline = time.monotonic() + 5
        while rig.store.get_job(job["id"])["state"] != js.CANCELLED:
            assert time.monotonic() < deadline, "the stopped turn's queued job was not cancelled"
            await asyncio.sleep(0.02)


# -- (h) the turn's wait budget ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_wait_budget_of_a_turn_is_refused_once_spent(home, monkeypatch):
    monkeypatch.setattr(music_tools, "TURN_WAIT_BUDGET_S", 0.3)
    async with Rig(home, start_runner=False) as rig:
        ctx = make_ctx(home, MusicService(home, rig, Person(True)))
        reg = registry_for(ctx)
        await call(reg, ctx, "music_propose_job", params=from_text())
        [job] = rig.store.list_jobs()
        first = await call(reg, ctx, "music_wait", job_id=job["id"], timeout=600)
        assert first.startswith(f"job {job['id']}: queued") and "wait budget is now spent" in first
        again = await call(reg, ctx, "music_wait", job_id=job["id"], timeout=600)
        assert again.startswith("⚠️ music_wait refused: this turn has used its wait budget")
        # The budget is the turn's: another turn waits again.
        other = await call(reg, ctx, "music_wait", turn="turn-2", job_id=job["id"], timeout=1)
        assert other.startswith(f"job {job['id']}: queued")
        assert not re.search(r"\d+(\.\d+)? ?s (elapsed|so far)|waited", first + other)


# -- (i) another agent's job ------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_a_job_the_agent_did_not_propose_is_refused(home):
    async with Rig(home, start_runner=False) as rig:
        service = MusicService(home, rig, Person(True))
        ctx_a, ctx_b = make_ctx(home, service), make_ctx(home, service, agent="agent-b")
        reg = registry_for(ctx_a)
        await call(reg, ctx_a, "music_propose_job", params=from_text())
        [job] = rig.store.list_jobs()
        ui_job = rig.store.create_job("generate_from_text", from_text(style="from the ui"))

        out = await call(reg, ctx_b, "music_cancel_job", job_id=job["id"])
        assert "not_your_job" in out
        out = await call(reg, ctx_a, "music_cancel_job", job_id=ui_job["id"])
        assert "not_your_job" in out
        assert rig.store.get_job(job["id"])["state"] == js.QUEUED
        assert rig.store.get_job(ui_job["id"])["state"] == js.QUEUED

        out = await call(reg, ctx_a, "music_cancel_job", job_id=job["id"], reason="changed my mind")
        assert out.startswith(f"job {job['id']}: cancelled")


# -- (j) one job per turn ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_propose_in_the_same_turn_returns_the_same_job_and_asks_nothing(home):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        ctx = make_ctx(home, MusicService(home, rig, person))
        reg = registry_for(ctx)
        await call(reg, ctx, "music_propose_job", params=from_text())
        [job] = rig.store.list_jobs()
        for params in (from_text(), from_text(style="something else entirely")):
            out = await call(reg, ctx, "music_propose_job", params=params)
            assert "already proposed 1 music job(s)" in out and f"job {job['id']}: queued" in out
        assert len(person.asked) == 1
        assert [j["id"] for j in rig.store.list_jobs()] == [job["id"]]
        # A new turn may propose again (and is asked again).
        await call(reg, ctx, "music_propose_job", turn="turn-2", params=from_text(style="next turn"))
        assert len(rig.store.list_jobs()) == 2 and len(person.asked) == 2


# -- (k) check_back to its own job ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_check_back_to_its_own_queued_job_asks_no_approval_and_another_does(home):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        service = MusicService(home, rig, person)
        ctx = make_ctx(home, service)
        ctx._agent = QueueAgent(ctx.agent_root)
        reg = registry_for(ctx)
        await call(reg, ctx, "music_propose_job", params=from_text())
        [job] = rig.store.list_jobs()
        assert len(person.asked) == 1

        out = await call(reg, ctx, "schedule_task", task_type="check_back",
                         task_data=json.dumps({"text": "see if the song is done", "music_job_id": job["id"]}),
                         delay_seconds=music_tools.check_back_delay_s(job))
        assert out.startswith("✓ Scheduled task"), out
        assert len(person.asked) == 1, "a return to its own running job asked the person"

        # A job that is not this agent's is asked about as before.
        ctx2 = make_ctx(home, service)
        ctx2.conversation_id = "agent_a_other_chat"
        ctx2.current_task_id = "agent_a_other_chat"
        ctx2._agent = ctx._agent
        ui_job = rig.store.create_job("generate_from_text", from_text(style="from the ui"))
        out = await call(reg, ctx2, "schedule_task", task_type="check_back",
                         task_data=json.dumps({"text": "x", "music_job_id": ui_job["id"]}), delay_seconds=60)
        assert out.startswith("✓ Scheduled task"), out
        assert len(person.asked) == 2 and person.asked[-1]["task_type"] == "check_back"


# -- the rules beside the tools ---------------------------------------------------------


def test_schedule_task_comes_with_any_music_tool_and_none_stays_no_gate():
    assert music_tools.with_check_back({"music_wait", "read_file"}) == {"music_wait", "read_file", "schedule_task"}
    assert music_tools.with_check_back({"read_file"}) == {"read_file"}
    assert music_tools.with_check_back(None) is None


def test_the_music_poll_tools_are_judged_by_their_output_and_their_timeouts_are_declared():
    assert {"music_wait", "music_check"} <= guards._OUTPUT_KEYED_TOOLS
    entries = {e.name: e for e in music_tools.get_tools()}
    assert set(entries) == music_tools.MUSIC_TOOL_NAMES
    assert all(e.owner_only and not e.default_enabled and not e.is_core for e in entries.values())
    assert entries["music_wait"].timeout_sec == music_tools.WAIT_MAX_S + music_tools.WAIT_MARGIN_S == 915
    assert entries["music_propose_job"].timeout_sec > core_tools._SCHEDULE_APPROVAL_TTL_SECONDS
