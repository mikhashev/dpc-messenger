"""Follow-up 13, items 1, 4 and 5 (Zcode's review of follow-up 12, P3): the music tools.

- A Stop or a Kill that comes after the person's "yes" makes no job: when it lands as the answer
  arrives, when only the process registry knows the Kill, and when it lands during the call that
  makes the job (the job is taken back at once, and the turn is registered first, so the loop's own
  Stop finds it too).
- YuE2's time on the card is an upper bound: its estimate is cap-based, so the card line, the card's
  parts and the line after the proposal say "up to N s". A job with no estimate (acestep.cpp) keeps
  its wording.
- The music tools' descriptions say the music-return-only mode (ADR-051 D49): without schedule_task
  the one call open is a check_back to the agent's own queued or running music job.

Live path as in the follow-up 12 tests: the loop's dispatcher, the real registry, the real music
command mixin over a real job store; the person is a stub UI surface.
"""

from __future__ import annotations

import asyncio

import pytest

from dpc_client_core import process_registry
from dpc_client_core.dpc_agent.tools import core as core_tools
from dpc_client_core.dpc_agent.tools import music as music_tools
from dpc_client_core.music import job_schemas as js
from tests._acestep_helpers import Rig, free_env, from_text, install_fake_engine
from tests.test_the_owners_agent_makes_music_through_the_music_tools_with_approval_limits_and_the_self_wait_rule import (
    MusicService, Person, call, make_ctx, registry_for,
)

STOPPED = "⚠️ The music job was not proposed: the turn was stopped. No job was made."


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
    return h


class StopsWithTheYes(MusicService):
    """The person's "yes" and the turn's Stop arrive together."""

    def __init__(self, home, rig, person, stop):
        super().__init__(home, rig, person)
        self.stop = stop

    async def announce_schedule_approval_request(self, request_id, **kw):
        self.person.asked.append(kw)
        self.stop.set()
        core_tools.resolve_schedule_approval(request_id, True)


# -- item 1 ------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["turn_stop_event", "turn_kill_event"])
async def test_a_stop_or_kill_arriving_with_the_yes_makes_no_job(home, which):
    async with Rig(home, start_runner=False) as rig:
        event = asyncio.Event()
        service = StopsWithTheYes(home, rig, Person(True), event)
        ctx = make_ctx(home, service)
        setattr(ctx, which, event)
        out = await call(registry_for(ctx), ctx, "music_propose_job", params=from_text())
        assert out == STOPPED, out
        assert rig.store.list_jobs() == []
        assert getattr(ctx, "_music_turns", {}) == {}


@pytest.mark.asyncio
async def test_a_kill_only_the_process_registry_knows_makes_no_job_after_the_yes(home):
    registry = process_registry.get_registry()

    class KillsWithTheYes(MusicService):
        async def announce_schedule_approval_request(self, request_id, **kw):
            self.person.asked.append(kw)
            registry.note_kill_of_turn("turn-killed")
            core_tools.resolve_schedule_approval(request_id, True)

    async with Rig(home, start_runner=False) as rig:
        ctx = make_ctx(home, KillsWithTheYes(home, rig, Person(True)))
        out = await call(registry_for(ctx), ctx, "music_propose_job", turn="turn-killed", params=from_text())
        assert out == STOPPED, out
        assert rig.store.list_jobs() == []


@pytest.mark.asyncio
async def test_a_stop_during_the_call_that_makes_the_job_takes_the_job_back_and_the_turn_is_registered_first(home):
    async with Rig(home, start_runner=False) as rig:
        stop = asyncio.Event()

        class StopsInTheCall(MusicService):
            async def propose_music_job(self, **kw):
                # The turn was registered for the loop's Stop before the job existed.
                seen["registered"] = dict(getattr(ctx, "_music_turns", {}))
                answer = await super().propose_music_job(**kw)
                stop.set()
                return answer

        seen = {}
        ctx = make_ctx(home, StopsInTheCall(home, rig, Person(True)))
        ctx.turn_stop_event = stop
        out = await call(registry_for(ctx), ctx, "music_propose_job", params=from_text())
        assert seen["registered"] == {"turn-1": "agent-a"}
        assert out.startswith("⚠️ The turn was stopped while the music job was being made; the job was "
                              "cancelled (job_"), out
        [job] = rig.store.list_jobs()
        assert job["state"] == js.CANCELLED, job["state"]


@pytest.mark.asyncio
async def test_a_turn_that_is_not_stopped_proposes_as_before(home):
    async with Rig(home, start_runner=False) as rig:
        ctx = make_ctx(home, MusicService(home, rig, Person(True)))
        ctx.turn_stop_event = asyncio.Event()
        out = await call(registry_for(ctx), ctx, "music_propose_job", params=from_text())
        assert out.startswith("Proposed (approved by the person)."), out
        [job] = rig.store.list_jobs()
        assert job["state"] == js.QUEUED
        assert ctx._music_turns == {"turn-1": "agent-a"}


# -- item 4: "up to" for YuE2 -------------------------------------------------------------------


YUE2 = {"engine": "yue2", "style": "slow piano", "lyrics": "[verse]\nla la la la", "variants": 1, "duration_s": 60}


def test_the_card_sentence_says_up_to_for_yue2_and_about_for_nothing_else():
    about = music_tools._approval_about(js.GENERATE_FROM_TEXT, YUE2)
    assert " up to " in about and " s on the GPU" in about and "about " not in about.split("GPU")[0].split(", ")[-1], about
    eta = int(round(js.music_expected_s(YUE2)))
    assert f"up to {eta} s on the GPU" in about

    acestep = {"engine": "acestep", "style": "drums", "variants": 1}
    assert music_tools._approval_about(js.GENERATE_FROM_TEXT, acestep).endswith("time on the GPU not known")


def test_the_card_parts_flag_yue2s_time_as_an_upper_bound_and_acestep_has_none():
    parts = music_tools._approval_parts(js.GENERATE_FROM_TEXT, YUE2)
    assert parts["eta_seconds"] and parts["eta_is_upper_bound"] is True
    plain = music_tools._approval_parts(js.GENERATE_FROM_TEXT, {"engine": "acestep", "style": "drums"})
    assert plain["eta_seconds"] is None and plain["eta_is_upper_bound"] is False


@pytest.mark.asyncio
async def test_the_line_after_a_yue2_proposal_says_up_to(home, monkeypatch):
    async with Rig(home, start_runner=False) as rig:
        ctx = make_ctx(home, MusicService(home, rig, Person(True)))
        monkeypatch.setattr(js, "music_expected_s", lambda params: 120.0)
        monkeypatch.setattr(music_tools, "_eta_is_upper_bound", lambda params: True)
        out = await call(registry_for(ctx), ctx, "music_propose_job", params=from_text())
        assert "expected time on the card: up to 120 s once it starts" in out, out
        monkeypatch.setattr(music_tools, "_eta_is_upper_bound", lambda params: False)
        out = await call(registry_for(ctx), ctx, "music_propose_job", turn="turn-2", params=from_text(style="b"))
        assert "expected time on the card: about 120 s once it starts" in out or "turn already proposed" in out, out


# -- item 5: the descriptions --------------------------------------------------------------------


def test_the_music_tools_that_point_to_check_back_say_the_music_return_only_rule():
    tools = {t.name: t.schema["description"] for t in music_tools.get_tools()}
    for name in ("music_propose_job", "music_wait", "music_check"):
        text = tools[name]
        assert "If schedule_task is not granted to you" in text, name
        assert "check_back" in text and "music_job_id" in text and "asks no approval" in text, name
        assert "core sets its delay and its text" in text, name
    assert "schedule_task is not granted" not in tools["music_list_library"]
