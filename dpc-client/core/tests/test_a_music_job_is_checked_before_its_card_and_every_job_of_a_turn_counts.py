"""Music tools follow-up 12, items 2, 5, 6 and (b), (c) (Zcode #68 P2, P3 and the surviving mutants).

- Everything core would refuse (variants out of range, an empty style, an unknown engine) is
  refused before the approval card, and so is a job core would make `unsupported`: the person is
  asked nothing and no job is made. An `unsupported` job that does exist (the runner refused a
  queued job) counts in the turn cap, in the tool and in core.
- The tool's own queue cap answers before the card; core's queue cap counts `running` jobs.
- Stop while the card is up withdraws it: a "yes" after the Stop makes no job.
- An agent cancels a job only from the chat that proposed it.
- The self-wait rule needs a live hold; a finished job is never "behind this turn".
- The card carries duration_s, has_lyrics and, only with lyrics, lyrics_excerpt.
- The approval log lines name what is asked (music_job), not "check_back".

Live path as in the part 2 tests: the loop's dispatcher, the real registry, the real music
command mixin over a real job store and library; the person is a stub UI surface.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.tools import core as core_tools
from dpc_client_core.dpc_agent.tools import music as music_tools
from dpc_client_core.managers.llama_server_supervisor import card_held_for
from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.jobs import MusicJobError
from dpc_client_core.music.runner import LM_PAYLOAD
from tests._acestep_helpers import Rig, free_env, from_text, install_fake_engine
from tests.test_the_owners_agent_makes_music_through_the_music_tools_with_approval_limits_and_the_self_wait_rule import (
    AGENT, CHAT, MusicService, Person, call, make_ctx, registry_for,
)

LOCAL = "strata-27b"


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


@pytest.fixture
def bare_home(tmp_path, monkeypatch):
    """acestep.cpp's models are there, its server binary is not: a job on it is unsupported."""
    h = tmp_path / "bare"
    install_fake_engine(h, binary=False)
    free_env(monkeypatch)
    return h


def _agent_job(rig, turn="turn-0", agent=AGENT, conversation=CHAT, **params):
    return rig.store.create_job("generate_from_text", from_text(**params), requested_by={
        "kind": "agent", "agent_id": agent, "conversation_id": conversation, "turn_id": turn})


# -- item 2: checked before the card ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad,field", [
    ({"variants": 99}, "variants"),
    ({"style": "   "}, "style"),
    ({"engine": "no-such-engine"}, "engine"),
])
async def test_params_core_would_refuse_are_refused_before_the_person_is_asked(home, bad, field):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        ctx = make_ctx(home, MusicService(home, rig, person))
        out = await call(registry_for(ctx), ctx, "music_propose_job", params=from_text(**bad))
        assert "invalid_params" in out and "Nothing was asked or proposed" in out, out
        assert person.asked == [], "the person was asked to approve a job core then refused"
        assert rig.store.list_jobs() == []


@pytest.mark.asyncio
async def test_a_job_core_would_make_unsupported_asks_nothing_and_makes_no_job(bare_home):
    async with Rig(bare_home, start_runner=False) as rig:
        person = Person(True)
        ctx = make_ctx(bare_home, MusicService(bare_home, rig, person))
        reg = registry_for(ctx)
        for turn in ("turn-1", "turn-1", "turn-2"):
            out = await call(reg, ctx, "music_propose_job", turn=turn, params=from_text())
            assert "unsupported" in out and "binary is missing" in out and "Nothing was asked" in out, out
        assert person.asked == []
        assert rig.store.list_jobs() == []


@pytest.mark.asyncio
async def test_an_unsupported_job_of_the_turn_counts_in_its_turn_cap_in_the_tool_and_in_core(home):
    async with Rig(home, start_runner=False) as rig:
        job = _agent_job(rig, turn="turn-1")
        rig.store.refuse_job(job["id"], "the adapter it names went missing")
        assert rig.store.get_job(job["id"])["state"] == js.UNSUPPORTED

        person = Person(True)
        ctx = make_ctx(home, MusicService(home, rig, person))
        out = await call(registry_for(ctx), ctx, "music_propose_job", params=from_text(style="another"))
        assert "already proposed 1 music job(s), at most 1 per turn" in out, out
        assert person.asked == [] and len(rig.store.list_jobs()) == 1

        with pytest.raises(MusicJobError) as err:
            _agent_job(rig, turn="turn-1", style="past the tool")
        assert err.value.code == "turn_job_limit"


@pytest.mark.asyncio
async def test_the_tools_own_queue_cap_answers_before_the_card(home):
    async with Rig(home, start_runner=False) as rig:
        _agent_job(rig, turn="turn-a", style="one")
        _agent_job(rig, turn="turn-b", style="two")
        person = Person(True)
        ctx = make_ctx(home, MusicService(home, rig, person))
        out = await call(registry_for(ctx), ctx, "music_propose_job", params=from_text(style="three"))
        # The tool's wording, not core's: the tool's check ran, before anything else was asked.
        assert out.startswith("⚠️ music_propose_job refused: requester_queue_full: 2 of this agent's music"), out
        assert person.asked == [] and len(rig.store.list_jobs()) == 2


@pytest.mark.asyncio
async def test_cores_queue_cap_counts_a_running_job(home):
    async with Rig(home, start_runner=False) as rig:
        job = _agent_job(rig, turn="turn-a")
        rig.store.write_payload(job["id"], LM_PAYLOAD, b"{}", endpoint="/lm")
        rig.store.start_job(job["id"])
        assert rig.store.get_job(job["id"])["state"] == js.RUNNING
        with pytest.raises(MusicJobError) as err:
            rig.store.create_job("generate_from_text", from_text(style="next"), agent_queue_max=1,
                                 requested_by={"kind": "agent", "agent_id": AGENT, "turn_id": "turn-b"})
        assert err.value.code == "requester_queue_full"
        with pytest.raises(MusicJobError):
            rig.store.check_job("generate_from_text", from_text(style="next"), agent_queue_max=1,
                                requested_by={"kind": "agent", "agent_id": AGENT, "turn_id": "turn-b"})


# -- item 5: Stop while the card is up; cancel from another chat ---------------------------------


class SilentPerson(Person):
    """A UI surface that shows the card and does not answer."""


class CardService(MusicService):
    def __init__(self, home, rig, person):
        super().__init__(home, rig, person)
        self.requests = []
        self.closed = []

    async def announce_schedule_approval_request(self, request_id, **kw):
        self.person.asked.append(kw)
        self.requests.append(request_id)

    async def announce_schedule_approval_closed(self, **kw):
        self.closed.append(kw)


@pytest.mark.asyncio
async def test_stop_while_the_card_is_up_withdraws_it_and_a_later_yes_makes_no_job(home):
    async with Rig(home, start_runner=False) as rig:
        service = CardService(home, rig, SilentPerson(True))
        ctx = make_ctx(home, service)
        ctx._event_loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        ctx.turn_stop_event = stop
        reg = registry_for(ctx)
        pending = asyncio.ensure_future(call(reg, ctx, "music_propose_job", params=from_text()))
        deadline = time.monotonic() + 5
        while not service.requests:
            assert time.monotonic() < deadline, "the card never went up"
            await asyncio.sleep(0.02)
        stop.set()
        out = await pending
        assert out == ("⚠️ The music job was not proposed: the turn was stopped before the person "
                       "answered. No job was made."), out
        assert core_tools.resolve_schedule_approval(service.requests[0], True) is False
        assert rig.store.list_jobs() == []
        [closed] = service.closed
        assert closed["resolution"] == "superseded"
        assert closed["outcome"] == "⛔ The turn was stopped; no music job was made."


@pytest.mark.asyncio
async def test_an_agent_cancels_its_job_only_from_the_chat_that_proposed_it(home):
    async with Rig(home, start_runner=False) as rig:
        service = MusicService(home, rig, Person(True))
        ctx = make_ctx(home, service)
        reg = registry_for(ctx)
        await call(reg, ctx, "music_propose_job", params=from_text())
        [job] = rig.store.list_jobs()
        elsewhere = make_ctx(home, service)
        elsewhere.conversation_id = "agent_a_other_chat"
        out = await call(reg, elsewhere, "music_cancel_job", job_id=job["id"])
        assert "not_your_job" in out and "another chat" in out, out
        assert rig.store.get_job(job["id"])["state"] == js.QUEUED
        out = await call(reg, ctx, "music_cancel_job", job_id=job["id"])
        assert out.startswith(f"job {job['id']}: cancelled"), out


# -- item 6: the self-wait rule's two gates -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hold_that_is_not_live_makes_no_self_wait(home, monkeypatch):
    async with Rig(home, start_runner=False) as rig:
        ctx = make_ctx(home, MusicService(home, rig, Person(True)))
        reg = registry_for(ctx)
        await call(reg, ctx, "music_propose_job", params=from_text())
        [job] = rig.store.list_jobs()
        monkeypatch.setattr(music_tools, "current_card_hold",
                            lambda: SimpleNamespace(live=False, alias=LOCAL))
        out = await call(reg, ctx, "music_wait", job_id=job["id"], timeout=1)
        assert "starts after this turn" not in out, out
        assert "Still not finished at the end of this wait" in out
        monkeypatch.setattr(music_tools, "current_card_hold",
                            lambda: SimpleNamespace(live=True, alias=LOCAL))
        out = await call(reg, ctx, "music_wait", job_id=job["id"], timeout=1)
        assert "starts after this turn" in out, out


def test_a_finished_job_whose_card_wait_names_this_alias_is_not_behind_this_turn():
    stale = {"id": "job_x", "state": js.REVIEW, "card_wait": {"holders": [{"alias": LOCAL}]}}
    assert music_tools._waits_behind_this_turn(stale, LOCAL) is False
    assert music_tools._self_wait_answer(stale, LOCAL) is None
    assert music_tools._waits_behind_this_turn({**stale, "state": js.RUNNING}, LOCAL) is True


# -- (b) the card's parts; (c) the log names what is asked ---------------------------------------


@pytest.mark.asyncio
async def test_the_card_carries_duration_has_lyrics_and_an_excerpt_only_with_lyrics(home):
    async with Rig(home, start_runner=False) as rig:
        person = Person(False)
        ctx = make_ctx(home, MusicService(home, rig, person))
        reg = registry_for(ctx)
        long_lyrics = "[verse]\n" + "la " * 100
        await call(reg, ctx, "music_propose_job", params=from_text(lyrics=long_lyrics, duration_s=45))
        await call(reg, ctx, "music_propose_job", turn="turn-2", params=from_text(lyrics=None, duration_s=20))
        sung, instrumental = (asked["music"] for asked in person.asked)
        assert sung["duration_s"] == 45.0 and sung["has_lyrics"] is True and sung["instrumental"] is False
        excerpt = sung["lyrics_excerpt"]
        assert excerpt.startswith("[verse] la la") and excerpt.endswith("…")
        assert music_tools.APPROVAL_LYRICS_CHARS - 2 <= len(excerpt) <= music_tools.APPROVAL_LYRICS_CHARS
        assert instrumental["duration_s"] == 20.0 and instrumental["has_lyrics"] is False
        assert "lyrics_excerpt" not in instrumental
        assert "45 s long" in person.asked[0]["about"]


@pytest.mark.asyncio
async def test_the_approval_log_names_a_music_job_not_a_check_back(home, monkeypatch, caplog):
    monkeypatch.setattr(core_tools, "_SCHEDULE_APPROVAL_TTL_SECONDS", 0.3)
    async with Rig(home, start_runner=False) as rig:
        service = CardService(home, rig, SilentPerson(True))
        ctx = make_ctx(home, service)
        ctx._event_loop = asyncio.get_running_loop()
        with caplog.at_level(logging.INFO, logger=core_tools.log.name):
            out = await call(registry_for(ctx), ctx, "music_propose_job", params=from_text())
        assert "nobody answered" in out, out
        text = "\n".join(r.getMessage() for r in caplog.records if r.name == core_tools.log.name)
        assert "music_job approval requested" in text and "music_job approval timed out" in text, text
        assert "check_back approval" not in text, text
