"""A music job of one chat is nothing to another chat of the same agent (backlog P2-5, P2-6).

The same agent identity serves several chats (a 1:1 with the owner, a group). Two lookups
ignored the chat:

- P2-5: `_own_unfinished_job` checked the agent and the state only, so a `check_back` in
  another chat to a job of the owner's 1:1 was Exempt from the owner's card (ADR-053 D-e),
  learned that job's time and woke in the wrong chat. Now the job must be this agent's in this
  chat, and a job that records no chat counts as nobody's (fail closed). A `job_<hex>` named
  in the task's text counts under the same check (its cases, the cross-chat one among them,
  are in test_a_check_back_naming_its_own_music_job_in_the_text_asks_no_approval.py).
- P2-6: `music_propose_job` matched an `idempotency_key` against every job of the agent and
  answered with that job's state, holders and result before the job card; core's pre-card
  check told a turn someone else asked for which tracks exist, their length and whether given
  lyrics match the stored ones. Now the key is looked up in this chat's jobs and any other
  match is a neutral refusal, a repeat core answers with is shown only when it is this chat's,
  and a foreign turn sees core's pre-card refusal of a library request without its reason.

Real code: the music commands, the job store, the registry and the loop's dispatcher, the
schedule_task tool and the approval round-trip (the rig of the music-tools test).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core.dpc_agent import owner_approval
from dpc_client_core.dpc_agent.agent import ASKER_HUMAN, OTHER_NODE_TURN, OWNER_TURN, Asker
from dpc_client_core.dpc_agent.loop import _execute_with_timeout
from dpc_client_core.dpc_agent.owner_approval import Exempt, ToolAsks
from dpc_client_core.dpc_agent.tools import chat_files
from dpc_client_core.dpc_agent.tools import core as core_tools
from dpc_client_core.dpc_agent.tools import music as music_tools
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.music import job_schemas as js
from tests._acestep_helpers import Rig, free_env, from_text
from tests._yue2_helpers import DualRig, install_both
from tests.test_the_owners_agent_makes_music_through_the_music_tools_with_approval_limits_and_the_self_wait_rule import (  # noqa: E501
    AGENT, BOUND, CHAT, MusicService, Person, QueueAgent, _clean, call, home, registry_for,
)
from tests.test_the_owners_agent_proposes_a_region_edit_and_the_card_names_the_region import base_track

__all__ = ["_clean", "home"]   # the fixtures, used by name

GROUP = "group-friends"
FRIEND = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=False, verified=True)


def chat_ctx(home, service, conversation, asker=None):
    """A context of agent AGENT in `conversation`; the owner's turn unless `asker` is given."""
    root = home / "agents" / AGENT
    (root / "logs").mkdir(parents=True, exist_ok=True)
    if asker is None:
        ctx = ToolContext(agent_root=root, turn_class="owner", message_source="ui",
                          conversation_id=conversation, dpc_service=service)
    else:
        ctx = ToolContext(agent_root=root, turn_class="other_node", message_source="group",
                          open_layers=frozenset(), asker=asker, conversation_id=conversation,
                          dpc_service=service)
    ctx.agent_event_loop = asyncio.get_running_loop()
    ctx._agent = QueueAgent(root)
    return ctx


async def one_job_in_the_owners_chat(home, rig, person, key=None):
    """The owner's 1:1 (CHAT) proposes one job, approved; it stays queued (no runner)."""
    ctx = chat_ctx(home, MusicService(home, rig, person), CHAT)
    args = {"params": from_text()}
    if key:
        args["idempotency_key"] = key
    out = await call(registry_for(ctx), ctx, "music_propose_job", **args)
    assert out.startswith("Proposed (approved by the person)."), out
    [job] = rig.store.list_jobs()
    assert job["requested_by"]["conversation_id"] == CHAT
    return ctx, job


def pending(ctx):
    return [t for t in ctx._agent.queue._queue if t.status == "pending"]


async def own_job(ctx, data):
    # Off the loop thread: the lookup calls the service on the loop and waits for it.
    return await asyncio.to_thread(music_tools.own_music_job_to_return_to, ctx, data)


# -- P2-5: check_back and the schedule_task gate --------------------------------------------


@pytest.mark.asyncio
async def test_the_same_agent_in_another_chat_does_not_own_the_job(home):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        ctx_a, job = await one_job_in_the_owners_chat(home, rig, person)
        ctx_b = chat_ctx(home, ctx_a.dpc_service, GROUP, asker=FRIEND)
        data = {"music_job_id": job["id"]}
        assert (await own_job(ctx_a, data))["id"] == job["id"]
        assert await own_job(ctx_b, data) is None
        assert await asyncio.to_thread(music_tools.is_return_to_own_music_job, ctx_b, data) is False

        args = {"task_type": "check_back", "task_data": json.dumps(data)}
        gate_a = await asyncio.to_thread(core_tools._schedule_task_approval, ctx_a, args)
        gate_b = await asyncio.to_thread(core_tools._schedule_task_approval, ctx_b, args)
        assert isinstance(gate_a, Exempt)
        assert isinstance(gate_b, ToolAsks), "another chat's job must not exempt the call from a card"


@pytest.mark.asyncio
async def test_a_check_back_in_a_group_to_the_owners_1to1_job_goes_to_the_owner_and_learns_nothing(home):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        ctx_a, job = await one_job_in_the_owners_chat(home, rig, person)
        person.answer = False
        ctx_b = chat_ctx(home, ctx_a.dpc_service, GROUP, asker=FRIEND)
        out = await call(registry_for(ctx_b), ctx_b, "schedule_task", turn="turn-g",
                         task_type="check_back", task_data=json.dumps({"music_job_id": job["id"]}),
                         delay_seconds=60)
        assert len(person.asked) == 2 and person.asked[-1]["task_type"] == "check_back"
        assert out.startswith("⚠️ check_back was not scheduled"), out
        assert pending(ctx_b) == []
        # Nothing of the 1:1 job: not its time, not core's wake text for it.
        delay = music_tools.check_back_delay_s(job)
        assert f"{delay}s" not in out and "A return to your own music job" not in out


@pytest.mark.asyncio
async def test_a_return_only_schedule_task_in_another_chat_refuses_the_other_chats_job(home):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        ctx_a, job = await one_job_in_the_owners_chat(home, rig, person)
        ctx_b = chat_ctx(home, ctx_a.dpc_service, GROUP)
        ctx_b.schedule_task_music_return_only = True
        out = await call(registry_for(ctx_b), ctx_b, "schedule_task", turn="turn-g",
                         task_type="check_back", task_data=json.dumps({"music_job_id": job["id"]}))
        assert out.startswith(music_tools.SCHEDULE_RETURN_ONLY_REFUSAL), out
        assert "in this chat" in out and pending(ctx_b) == [] and len(person.asked) == 1


@pytest.mark.asyncio
async def test_the_owners_own_chat_still_returns_to_its_job_with_no_card(home):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        ctx_a, job = await one_job_in_the_owners_chat(home, rig, person)
        out = await call(registry_for(ctx_a), ctx_a, "schedule_task", turn="turn-2",
                         task_type="check_back", task_data=json.dumps({"music_job_id": job["id"]}))
        assert out.startswith("✓ Scheduled task"), out
        assert len(person.asked) == 1, "a return to its own job in its own chat asks nothing"
        [task] = pending(ctx_a)
        assert task.data["music_job_id"] == job["id"] and task.data["_reply_conversation_id"] == CHAT


@pytest.mark.asyncio
async def test_a_job_that_records_no_chat_is_nobodys_to_return_to(home):
    async with Rig(home, start_runner=False) as rig:
        ctx_a = chat_ctx(home, MusicService(home, rig, Person(True)), CHAT)
        legacy = rig.store.create_job("generate_from_text", from_text(),
                                      requested_by={"kind": js.REQUESTER_AGENT, "agent_id": AGENT})
        assert legacy["state"] == js.QUEUED
        assert await own_job(ctx_a, {"music_job_id": legacy["id"]}) is None


# -- P2-6: music_propose_job's key, repeat and pre-card answers ----------------------------


@pytest.mark.asyncio
async def test_a_key_used_in_another_chat_is_refused_without_a_word_of_that_job(home):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        ctx_a, job = await one_job_in_the_owners_chat(home, rig, person, key="song-1")
        for asker in (None, FRIEND):
            ctx_b = chat_ctx(home, ctx_a.dpc_service, GROUP, asker=asker)
            expected = (music_tools.KEY_NOT_YOURS_REFUSAL if asker is None
                        else music_tools.PRE_CARD_REFUSAL_FOR_OTHERS)
            for params in (from_text(), from_text(style="other")):
                out = await call(registry_for(ctx_b), ctx_b, "music_propose_job", turn="turn-g",
                                 params=params, idempotency_key="song-1")
                assert out == expected, out
                assert job["id"] not in out
        assert len(person.asked) == 1 and len(rig.store.list_jobs()) == 1

        # In its own chat the key still names its job.
        out = await call(registry_for(ctx_a), ctx_a, "music_propose_job", turn="turn-2",
                         params=from_text(), idempotency_key="song-1")
        assert out.startswith("This idempotency_key already names a job") and job["id"] in out, out


@pytest.mark.asyncio
async def test_a_foreign_turn_gets_the_same_pre_card_refusal_for_a_taken_key_and_an_invalid_request(home):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        ctx_a, _job = await one_job_in_the_owners_chat(home, rig, person, key="song-1")
        foreign = chat_ctx(home, ctx_a.dpc_service, GROUP, asker=FRIEND)
        reg = registry_for(foreign)
        bad_edit = {"track_id": "trk_" + "f" * 16, "action": "remove_region",
                    "region": {"t_start": 0.0, "t_end": 1.0}, "text": "probe"}
        probes = [
            ("edit", bad_edit, "song-1"),
            ("edit", bad_edit, "unused-key"),
            (js.GENERATE_FROM_TEXT, from_text(variants=99), "song-1"),
            (js.GENERATE_FROM_TEXT, from_text(variants=99), "unused-key"),
        ]
        for i, (job_type, params, key) in enumerate(probes):
            out = await call(reg, foreign, "music_propose_job", turn=f"turn-p{i}",
                             type=job_type, params=params, idempotency_key=key)
            assert out == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS, out
        assert len(person.asked) == 1 and len(rig.store.list_jobs()) == 1


@pytest.mark.asyncio
async def test_a_foreign_turn_sees_no_queue_count_when_the_agents_queue_is_full(home, monkeypatch):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        service = MusicService(home, rig, person)
        owner = chat_ctx(home, service, CHAT)
        reg_owner = registry_for(owner)
        for i in (1, 2):
            out = await call(reg_owner, owner, "music_propose_job", turn=f"turn-o{i}",
                             params=from_text(style=f"song {i}"))
            assert out.startswith("Proposed (approved by the person)."), out
        assert len(person.asked) == 2

        foreign = chat_ctx(home, service, GROUP, asker=FRIEND)
        reg_foreign = registry_for(foreign)
        for i, (job_type, params) in enumerate((
            (js.GENERATE_FROM_TEXT, from_text(style="foreign")),
            ("edit", {"track_id": "trk_" + "f" * 16, "action": "remove_region",
                      "region": {"t_start": 0.0, "t_end": 1.0}, "text": "x"}),
        )):
            out = await call(reg_foreign, foreign, "music_propose_job", turn=f"turn-f{i}",
                             type=job_type, params=params)
            assert out == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS, out
            assert "queued or running" not in out and "requester_queue_full" not in out and "2 of" not in out
        assert len(person.asked) == 2

        # The owner still gets the queue count.
        out = await call(reg_owner, owner, "music_propose_job", turn="turn-o3", params=from_text(style="three"))
        assert out.startswith("⚠️ music_propose_job refused: requester_queue_full: 2 of this agent's music"), out

    # Fallback if core returns requester_queue_full (e.g. service without _music_agent_queue_max).
    _fake(monkeypatch, {"status": "error", "code": "requester_queue_full",
                        "message": "2 music job(s) of this agent are queued or running; at most 2 at once"})
    foreign = chat_ctx(home, None, GROUP, asker=FRIEND)
    out = music_tools.music_propose_job.__wrapped__(foreign, params=from_text())
    assert out == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS


@pytest.mark.asyncio
async def test_a_foreign_turn_cannot_probe_stored_lyrics_when_lyrics_mode_is_omitted(home):
    async with Rig(home, start_runner=False) as rig:
        entry = await base_track(rig, home, seconds=12.0)
        person = Person(True)
        service = MusicService(home, rig, person)
        await (await service._music_lyrics()).save_manual(entry["id"], "[00:01.00]secret verse\n[00:05.00]secret chorus")
        owner = chat_ctx(home, service, CHAT)
        out = await call(registry_for(owner), owner, "music_propose_job", turn="turn-o1",
                         params=from_text(), idempotency_key="taken-key")
        assert out.startswith("Proposed (approved by the person)."), out
        asked_before = len(person.asked)

        foreign = chat_ctx(home, service, GROUP, asker=FRIEND)
        reg = registry_for(foreign)
        for i, (job_type, params, key) in enumerate((
            (js.COVER, {"track_id": entry["id"], "caption": "rock", "lyrics": "wrong guess"}, None),
            (js.COVER, {"track_id": entry["id"], "caption": "rock", "lyrics": "secret verse\nsecret chorus"}, None),
            (js.GENERATE_FROM_LYRICS, {"track_id": entry["id"], "caption": "rock", "lyrics": "wrong guess"}, None),
            (js.GENERATE_FROM_LYRICS, {"track_id": entry["id"], "caption": "rock",
                                       "lyrics": "secret verse\nsecret chorus"}, None),
            (js.COVER, {"track_id": entry["id"], "caption": "rock", "lyrics_mode": None,
                        "lyrics": "secret verse\nsecret chorus"}, None),
            (js.COVER, {"track_id": entry["id"], "caption": "rock", "lyrics_mode": "",
                        "lyrics": "secret verse\nsecret chorus"}, None),
            (js.COVER, {"track_id": entry["id"], "caption": "rock", "lyrics": "wrong guess"}, "taken-key"),
            (js.COVER, {"track_id": entry["id"], "caption": "rock", "lyrics": "wrong guess"}, "free-key"),
        )):
            out = await call(reg, foreign, "music_propose_job", turn=f"turn-l{i}",
                             type=job_type, params=params, idempotency_key=key)
            assert out.startswith("⚠️ music_propose_job: with lyrics_mode reuse") and "leave lyrics out" in out, out
            assert "reuse_lyrics_differ" not in out
        assert len(person.asked) == asked_before
        assert len(rig.store.list_jobs()) == 1

        # Explicit non-reuse modes are not caught by the reuse+lyrics guard.
        new_out = await call(reg, foreign, "music_propose_job", turn="turn-new", type=js.GENERATE_SIMILAR,
                             params={"track_id": entry["id"], "caption": "synthwave", "engine": "acestep",
                                     "lyrics_mode": "new", "lyrics": "fresh line"})
        assert new_out.startswith("Proposed (approved by the person)."), new_out
        assert len(person.asked) == asked_before + 1

        rewrite_out = await call(reg, foreign, "music_propose_job", turn="turn-rewrite", type=js.GENERATE_SIMILAR,
                                 params={"track_id": entry["id"], "caption": "synthwave", "engine": "acestep",
                                         "lyrics_mode": "rewrite", "lyrics": "fresh line"})
        assert rewrite_out == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS, rewrite_out


@pytest.mark.asyncio
async def test_a_foreign_turn_sees_no_paths_or_adapter_presence_from_generate_from_text(tmp_path, monkeypatch):
    home_both = tmp_path / "dual"
    install_both(home_both)
    free_env(monkeypatch)
    monkeypatch.setattr(music_tools, "POLL_INTERVAL_S", 0.05)

    async with DualRig(home_both, start_runner=False) as rig:
        person = Person(True)
        service = MusicService(home_both, rig, person)
        foreign = chat_ctx(home_both, service, GROUP, asker=FRIEND)
        reg = registry_for(foreign)
        bad_requests = [
            {"engine": "yue2", "style": "rock", "lyrics": "la", "adapters": [{"name": "no-such-lora"}]},
            {"engine": "yue2", "style": "rock", "lyrics": "la", "backbone": "cot"},
            from_text(variants=99),
        ]
        for i, params in enumerate(bad_requests):
            out = await call(reg, foreign, "music_propose_job", turn=f"turn-g{i}",
                             type=js.GENERATE_FROM_TEXT, params=params)
            assert out == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS, out
            assert str(home_both) not in out
            assert "loras" not in out and "does not exist" not in out and "no-such-lora" not in out
        assert person.asked == [] and rig.store.list_jobs() == []

        # The owner still gets the full validation error.
        owner = chat_ctx(home_both, service, CHAT)
        out = await call(registry_for(owner), owner, "music_propose_job", turn="turn-o1",
                         type=js.GENERATE_FROM_TEXT, params=bad_requests[0])
        assert "invalid_params" in out and "no-such-lora" in out, out

    # Unsupported engine/model errors on generate_from_text are also masked for foreign turns.
    _fake(monkeypatch, {"status": "error", "code": "unsupported", "message": "yue2.cpp binary is missing"})
    foreign_mock = chat_ctx(home_both, None, GROUP, asker=FRIEND)
    assert music_tools.music_propose_job.__wrapped__(foreign_mock, params=from_text()) == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS


@pytest.mark.asyncio
async def test_a_context_without_a_conversation_id_matches_only_its_own_turn_key_and_fails_closed_across_turns(home):
    async def call_chatless(reg, ctx, turn, **args):
        tc = {"id": "c1", "type": "function",
              "function": {"name": "music_propose_job", "arguments": json.dumps(args)}}
        out = await asyncio.wait_for(_execute_with_timeout(
            reg, tc, ctx.agent_root / "logs", reg.get_timeout("music_propose_job"), ctx=ctx,
            agent_id=ctx.agent_root.name, session_id="", turn_id=turn), BOUND)
        return out["result"]

    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        service = MusicService(home, rig, person)
        ctx_chat = chat_ctx(home, service, CHAT)
        out_chat = await call(registry_for(ctx_chat), ctx_chat, "music_propose_job", turn="turn-c",
                              params=from_text(), idempotency_key="chat-key")
        assert out_chat.startswith("Proposed (approved by the person)."), out_chat

        chatless = chat_ctx(home, service, None)
        reg = registry_for(chatless)
        out1 = await call_chatless(reg, chatless, "turn-1", params=from_text(), idempotency_key="chatless-1")
        assert out1.startswith("Proposed (approved by the person)."), out1
        chatless_job = [j for j in rig.store.list_jobs() if j.get("idempotency_key") == "chatless-1"][0]
        assert "conversation_id" not in chatless_job["requested_by"]

        # Same chatless turn repeating its own idempotency_key gets its own job back.
        out2 = await call_chatless(reg, chatless, "turn-1", params=from_text(), idempotency_key="chatless-1")
        assert out2.startswith("This idempotency_key already names a job") and chatless_job["id"] in out2, out2
        assert len(person.asked) == 2

        # Different turn without conversation_id fails closed on both chatless and chatted keys.
        out3 = await call_chatless(reg, chatless, "turn-2", params=from_text(), idempotency_key="chatless-1")
        assert out3 == music_tools.KEY_NOT_YOURS_REFUSAL and chatless_job["id"] not in out3, out3

        out4 = await call_chatless(reg, chatless, "turn-2", params=from_text(), idempotency_key="chat-key")
        assert out4 == music_tools.KEY_NOT_YOURS_REFUSAL, out4


@pytest.mark.asyncio
async def test_a_legacy_job_without_conversation_id_fails_closed_on_key_and_repeat_from_a_chat(home, monkeypatch):
    async with Rig(home, start_runner=False) as rig:
        person = Person(True)
        service = MusicService(home, rig, person)
        legacy = rig.store.create_job(
            "generate_from_text", from_text(style="legacy secret"), idempotency_key="legacy-1",
            requested_by={"kind": js.REQUESTER_AGENT, "agent_id": AGENT, "turn_id": "turn-0"})
        assert "conversation_id" not in legacy["requested_by"]

        owner = chat_ctx(home, service, CHAT)
        out_owner = await call(registry_for(owner), owner, "music_propose_job", turn="turn-0",
                               params=from_text(style="legacy secret"), idempotency_key="legacy-1")
        assert out_owner == music_tools.KEY_NOT_YOURS_REFUSAL and legacy["id"] not in out_owner, out_owner

        foreign = chat_ctx(home, service, GROUP, asker=FRIEND)
        out_foreign = await call(registry_for(foreign), foreign, "music_propose_job", turn="turn-0",
                                 params=from_text(style="legacy secret"), idempotency_key="legacy-1")
        assert out_foreign == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS and legacy["id"] not in out_foreign, out_foreign

        # Dropping the key creates a new conversation-scoped job in this chat.
        fresh = await call(registry_for(owner), owner, "music_propose_job", turn="turn-1",
                           params=from_text(style="fresh"))
        assert fresh.startswith("Proposed (approved by the person)."), fresh
        assert len(rig.store.list_jobs()) == 2

    # Core returning a legacy repeat (no conversation_id) also fails closed even if turn_id matches.
    legacy_job = {"id": "job_legacy0123456789", "state": js.RUNNING, "result_text": "legacy secret",
                  "requested_by": {"kind": js.REQUESTER_AGENT, "agent_id": AGENT, "turn_id": "turn-1"}}
    _fake(monkeypatch, {"status": "ok", "repeat": legacy_job, "params": None})
    owner_ctx = chat_ctx(home, None, CHAT)
    out_rep_owner = music_tools.music_propose_job.__wrapped__(owner_ctx, params=from_text(), idempotency_key="k")
    assert out_rep_owner == music_tools.REPEAT_NOT_YOURS_REFUSAL and "legacy" not in out_rep_owner

    foreign_ctx = chat_ctx(home, None, GROUP, asker=FRIEND)
    out_rep_foreign = music_tools.music_propose_job.__wrapped__(foreign_ctx, params=from_text(), idempotency_key="k")
    assert out_rep_foreign == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS and "legacy" not in out_rep_foreign


@pytest.mark.asyncio
async def test_a_foreign_turn_sees_no_reason_for_a_library_refusal_and_the_owner_does(home):
    async with Rig(home, start_runner=False) as rig:
        entry = await base_track(rig, home, seconds=6.0)
        person = Person(True)
        service = MusicService(home, rig, person)
        beyond = {"track_id": entry["id"], "action": "remove_region",
                  "region": {"t_start": 10.0, "t_end": 12.0}, "text": "the tail"}
        unknown = dict(beyond, track_id="trk_" + "f" * 16)

        owner = chat_ctx(home, service, CHAT)
        out = await call(registry_for(owner), owner, "music_propose_job", type="edit", params=beyond)
        assert out.startswith("⚠️ music_propose_job failed:") and out != music_tools.PRE_CARD_REFUSAL_FOR_OTHERS

        foreign = chat_ctx(home, service, GROUP, asker=FRIEND)
        for i, params in enumerate((beyond, unknown)):
            out = await call(registry_for(foreign), foreign, "music_propose_job", turn=f"turn-g{i}",
                             type="edit", params=params)
            assert out == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS, out
        assert person.asked == [] and rig.store.list_jobs() == []


@pytest.mark.asyncio
async def test_a_foreign_turn_cannot_test_given_lines_against_the_stored_lyrics(home):
    async with Rig(home, start_runner=False) as rig:
        entry = await base_track(rig, home)
        person = Person(True)
        foreign = chat_ctx(home, MusicService(home, rig, person), GROUP, asker=FRIEND)
        out = await call(registry_for(foreign), foreign, "music_propose_job", type=js.GENERATE_SIMILAR,
                         params={"track_id": entry["id"], "lyrics_mode": "reuse", "lyrics": "a guess\nat the words",
                                 "caption": "synthwave"})
        assert out.startswith("⚠️ music_propose_job: with lyrics_mode reuse") and "leave lyrics out" in out, out
        assert "reuse_lyrics_differ" not in out and person.asked == [] and rig.store.list_jobs() == []


def _fake(monkeypatch, check_answer, propose_answer=None):
    calls = []

    def service_call(ctx, method, **kwargs):
        calls.append(method)
        if method == "list_music_jobs":
            return {"status": "ok", "jobs": []}
        if method == "check_music_job":
            return check_answer
        if method == "propose_music_job":
            return propose_answer
        return {"status": "ok"}

    monkeypatch.setattr(music_tools, "_service_call", service_call)
    monkeypatch.setattr(music_tools, "_turn_id", lambda: "turn-1")
    return calls


OTHER_CHATS_JOB = {"id": "job_0123456789abcdef", "state": js.RUNNING, "result_text": "secret",
                   "card_wait": {"holders": [{"alias": "strata-27b"}]},
                   "requested_by": {"kind": "agent", "agent_id": AGENT, "conversation_id": "dm-mike",
                                    "turn_id": "turn-0"}}


@pytest.mark.asyncio
async def test_a_repeat_core_answers_with_from_another_chat_is_a_neutral_refusal(home, monkeypatch):
    calls = _fake(monkeypatch, {"status": "ok", "repeat": OTHER_CHATS_JOB, "params": None})
    ctx = chat_ctx(home, None, GROUP)
    out = music_tools.music_propose_job.__wrapped__(ctx, params=from_text(), idempotency_key="k")
    assert out == music_tools.REPEAT_NOT_YOURS_REFUSAL
    assert "propose_music_job" not in calls

    foreign = chat_ctx(home, None, GROUP, asker=FRIEND)
    out_foreign = music_tools.music_propose_job.__wrapped__(foreign, params=from_text(), idempotency_key="k")
    assert out_foreign == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS
    assert "propose_music_job" not in calls


@pytest.mark.asyncio
async def test_an_idempotency_conflict_does_not_name_the_other_job(home, monkeypatch):
    _fake(monkeypatch, {"status": "error", "code": "idempotency_conflict",
                        "message": "idempotency_key 'k' belongs to job job_0123456789abcdef with other params"})
    ctx = chat_ctx(home, None, GROUP)
    out = music_tools.music_propose_job.__wrapped__(ctx, params=from_text(), idempotency_key="k")
    assert out == music_tools.KEY_NOT_YOURS_REFUSAL

    foreign = chat_ctx(home, None, GROUP, asker=FRIEND)
    out_foreign = music_tools.music_propose_job.__wrapped__(foreign, params=from_text(), idempotency_key="k")
    assert out_foreign == music_tools.PRE_CARD_REFUSAL_FOR_OTHERS


@pytest.mark.asyncio
async def test_a_job_core_answers_with_after_the_card_from_another_chat_is_not_shown(home, monkeypatch):
    calls = _fake(monkeypatch, {"status": "ok", "repeat": None, "params": from_text()},
                  propose_answer={"status": "ok", "job": OTHER_CHATS_JOB})
    monkeypatch.setattr(core_tools, "_await_schedule_approval", lambda ctx, **kw: (True, ""))
    ctx = chat_ctx(home, None, GROUP)
    out = music_tools.music_propose_job.__wrapped__(ctx, params=from_text(), idempotency_key="k")
    assert out == music_tools.REPEAT_NOT_YOURS_REFUSAL and "secret" not in out
    assert calls.count("propose_music_job") == 1


# -- Mike #19: read, take, library, lyrics, playlist and chat_post_file(take_id) gates -----------

THIS_NODE = "dpc-node-this-0000"
FRIEND_NODE = "dpc-node-friend-0002"
ONE_AGENT = "agent_owner_0001"
ONE_TO_ONE = ONE_AGENT
SOLO_GROUP = "group-solo"
FRIENDS_GROUP = GROUP
OWNER_ASKER = Asker(ASKER_HUMAN, "Owner", THIS_NODE, is_owner=True, verified=True)
DONE_STATES = (js.REVIEW, js.ACCEPTED, js.FAILED, js.CANCELLED)


class _MultiChatService(MusicService):
    """MusicService with group_manager, node identity, and group agent file posting."""

    def __init__(self, home_dir, rig, person):
        super().__init__(home_dir, rig, person)
        self.p2p_manager = SimpleNamespace(get_display_name=lambda: "Owner", node_id=THIS_NODE)
        self._groups = {
            FRIENDS_GROUP: SimpleNamespace(
                group_id=FRIENDS_GROUP, name="Friends",
                members=[THIS_NODE, FRIEND_NODE], is_discord_bridge=False,
            ),
            SOLO_GROUP: SimpleNamespace(
                group_id=SOLO_GROUP, name="AI Studio",
                members=[THIS_NODE], is_discord_bridge=False,
            ),
        }
        self.group_manager = SimpleNamespace(
            node_id=THIS_NODE,
            get_group=lambda gid: self._groups.get(gid),
        )
        self.sent = []

    def _get_agent_telegram_bridge(self, agent_id):
        return None

    def _conversation_display_name(self, cid: str) -> str:
        if cid in self._groups:
            return self._groups[cid].name
        if cid == ONE_TO_ONE:
            return "OwnerAgent (1:1)"
        return cid

    async def send_group_agent_file(self, group_id, agent_name, file_path, text="", music=None,
                                    expected_sha256=None, expected_size=None):
        copy = Path(file_path)
        self.sent.append({
            "group_id": group_id, "agent_name": agent_name, "name": copy.name,
            "bytes": copy.read_bytes(), "text": text, "music": music,
        })
        return {
            "status": "success", "saved_locally": True, "file_path": f"/group/files/{copy.name}",
            "message_id": f"msg-{len(self.sent)}", "msg_index": len(self.sent),
            "size_bytes": copy.stat().st_size, "mime_type": "audio/flac",
            "sender_name": "OwnerAgent", "transfer_ids": ["t1"], "offline_members": [],
        }


def _multi_ctx(dpc_home, service, conversation, *, asker=OWNER_ASKER, turn_class=OWNER_TURN,
               source=None):
    root = dpc_home / "agents" / ONE_AGENT
    (root / "logs").mkdir(parents=True, exist_ok=True)
    if source is None:
        source = "group" if conversation.startswith("group-") else "ui"
    ctx = ToolContext(
        agent_root=root,
        turn_class=turn_class,
        open_layers=frozenset({"L5", "L6", "EXT"}) if turn_class == OWNER_TURN else frozenset(),
        message_source=source,
        asker=asker,
        conversation_id=conversation,
        conversation_title=service._conversation_display_name(conversation) if service else None,
        dpc_service=service,
    )
    ctx.agent_event_loop = asyncio.get_running_loop()
    ctx._event_loop = asyncio.get_running_loop()
    ctx._agent = QueueAgent(root)
    return ctx


async def _exec(reg, ctx, name, turn="turn-1", **args):
    tc = {"id": f"c-{name}", "type": "function",
          "function": {"name": name, "arguments": json.dumps(args)}}
    out = await asyncio.wait_for(_execute_with_timeout(
        reg, tc, ctx.agent_root / "logs", reg.get_timeout(name), ctx=ctx,
        agent_id=ctx.agent_root.name, session_id=ctx.conversation_id or "", turn_id=turn), BOUND)
    return out["result"]


async def _seed_world(tmp_path, monkeypatch, *, start_runner=True):
    """Build one Rig + _MultiChatService with:
    - a 1:1 generated job/take/track/lyrics (`job_dm`, `take_dm`, `trk_dm`)
    - a personal folder track (`trk_folder`) with lyrics
    - a user playlist (`pl_private`) containing both private tracks
    - a multi-node group job/take/track/lyrics (`job_group`, `take_group`, `trk_group`)
    """
    user_home = tmp_path / "u"
    dpc_home = user_home / ".dpc"
    from tests._acestep_helpers import install_fake_engine
    install_fake_engine(dpc_home)
    free_env(monkeypatch)
    monkeypatch.setattr(music_tools, "POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setenv("DPC_HOME", str(dpc_home))
    monkeypatch.setattr(service_mod, "DPC_HOME_DIR", dpc_home)
    monkeypatch.setattr(core_tools, "_SCHEDULE_APPROVAL_TTL_SECONDS", 0.3)
    owner_approval._reset_for_tests()
    return user_home, dpc_home


async def _populate_world(rig, dpc_home, service):
    # 1. Propose and finish a 1:1 job in ONE_TO_ONE (seed=1)
    ctx_dm = _multi_ctx(dpc_home, service, ONE_TO_ONE, asker=OWNER_ASKER, turn_class=OWNER_TURN)
    out_dm = await _exec(registry_for(ctx_dm), ctx_dm, "music_propose_job", turn="turn-dm",
                         params=from_text(style="private 1to1 ballad", variants=1, seed=1))
    assert out_dm.startswith("Proposed (approved by the person)."), out_dm
    job_dm = rig.store.list_jobs()[-1]
    job_dm = await rig.wait_for(job_dm["id"], DONE_STATES)
    assert job_dm["state"] == js.REVIEW, job_dm.get("state_reason")
    [take_dm] = rig.store.list_takes(job_dm["id"])
    trk_dm = take_dm["library_track_id"]
    await (await service._music_lyrics()).save_manual(trk_dm, "[00:01.00]SECRET 1TO1 VERSE")

    # 2. Add a personal folder track + lyrics
    folder = dpc_home.parent / "MyMusic"
    folder.mkdir(parents=True, exist_ok=True)
    personal_wav = folder / "Personal Song.wav"
    import wave as _wave
    import numpy as _np
    pcm = (0.25 * _np.sin(2 * _np.pi * 330 * _np.arange(48000) / 48000) * 32767).astype("<i2")
    with _wave.open(str(personal_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(pcm.tobytes())
    added_folder = await rig.library.add_folder(str(folder), watch=False)
    await rig.library.rescan_folder(added_folder["id"])
    entries = rig.library.list_entries()
    folder_entry = next(e for e in entries if e.get("origin") == "folder")
    trk_folder = folder_entry["id"]
    await (await service._music_lyrics()).save_manual(trk_folder, "[00:02.00]SECRET FOLDER LYRICS")

    # 3. Create a private playlist
    pl_private = await rig.library.create_playlist("Secret Private Mix", [trk_dm, trk_folder])

    # 4. Propose and finish a group job inside FRIENDS_GROUP (seed=2)
    ctx_grp = _multi_ctx(dpc_home, service, FRIENDS_GROUP, asker=OWNER_ASKER, turn_class=OTHER_NODE_TURN)
    out_grp = await _exec(registry_for(ctx_grp), ctx_grp, "music_propose_job", turn="turn-grp",
                          params=from_text(style="public group anthem", variants=1, seed=2))
    assert out_grp.startswith("Proposed (approved by the person)."), out_grp
    job_group = next(j for j in rig.store.list_jobs() if j["id"] != job_dm["id"])
    job_group = await rig.wait_for(job_group["id"], DONE_STATES)
    assert job_group["state"] == js.REVIEW, job_group.get("state_reason")
    [take_group] = rig.store.list_takes(job_group["id"])
    trk_group = take_group["library_track_id"]
    await (await service._music_lyrics()).save_manual(trk_group, "[00:01.00]PUBLIC GROUP CHORUS")

    service.person.asked.clear()
    return {
        "job_dm": job_dm, "take_dm": take_dm, "trk_dm": trk_dm,
        "trk_folder": trk_folder, "pl_private": pl_private,
        "job_group": job_group, "take_group": take_group, "trk_group": trk_group,
    }


@pytest.mark.asyncio
async def test_multi_node_group_blocks_1to1_and_private_library_even_when_owner_asked(tmp_path, monkeypatch):
    _user_home, dpc_home = await _seed_world(tmp_path, monkeypatch)
    async with Rig(dpc_home) as rig:
        person = Person(True)
        service = _MultiChatService(dpc_home, rig, person)
        data = await _populate_world(rig, dpc_home, service)

        ctx = _multi_ctx(dpc_home, service, FRIENDS_GROUP, asker=OWNER_ASKER, turn_class=OTHER_NODE_TURN)
        reg = registry_for(ctx)
        job_dm, take_dm, trk_dm = data["job_dm"], data["take_dm"], data["trk_dm"]
        trk_folder, pl = data["trk_folder"], data["pl_private"]
        job_group, take_group, trk_group = data["job_group"], data["take_group"], data["trk_group"]

        # 1. Job-reading tools refuse 1:1 job neutrally (no status, no .flac path, no take id)
        for tool_name, args in (
            ("music_check", {"job_id": job_dm["id"]}),
            ("music_wait", {"job_id": job_dm["id"], "timeout": 1}),
            ("music_list_takes", {"job_id": job_dm["id"]}),
        ):
            out = await _exec(reg, ctx, tool_name, **args)
            assert "not_your_job" in out, (tool_name, out)
            assert "review" not in out and ".flac" not in out and take_dm["id"] not in out

        # Non-existent job id gives the exact same neutral refusal (no existence oracle)
        fake_check = await _exec(reg, ctx, "music_check", job_id="job_0000000000000000")
        assert "not_your_job" in fake_check and "not_found" not in fake_check

        # 2. Lyrics refuse 1:1 track, personal folder track, and unknown track identically
        for bad_trk in (trk_dm, trk_folder, "trk_0000000000000000"):
            out = await _exec(reg, ctx, "music_get_lyrics", track_id=bad_trk)
            assert "not_your_job" in out, (bad_trk, out)
            assert "SECRET" not in out and "not_found" not in out

        # 3. Library lists ONLY tracks created in this group, and NO private playlists
        lib = json.loads(await _exec(reg, ctx, "music_list_library"))
        assert [t["id"] for t in lib["tracks"]] == [trk_group]
        assert lib["tracks_total"] == 1
        assert lib["playlists"] == []

        # 4. Playlist add refuses 1:1 track, folder track, and 1:1 take before touching playlist
        before_pl = [p for p in rig.library.list_playlists() if p.get("kind") == "user"]
        for kwargs in (
            {"playlist_id": pl["id"], "track_ids": [trk_dm]},
            {"playlist_id": pl["id"], "track_ids": [trk_folder]},
            {"playlist_id": pl["id"], "track_ids": [trk_group, trk_dm]},
            {"playlist_id": pl["id"], "take_id": take_dm["id"]},
        ):
            out = await _exec(reg, ctx, "music_playlist_add", **kwargs)
            assert "not_your_job" in out, (kwargs, out)
            assert "Secret Private Mix" not in out and "already has" not in out
        assert [p for p in rig.library.list_playlists() if p.get("kind") == "user"] == before_pl

        # 5. chat_post_file(take_id=...) refuses 1:1 take without sending or leaking state
        post_out = await _exec(reg, ctx, "chat_post_file", take_id=take_dm["id"])
        assert "not_your_job" in post_out, post_out
        assert "review" not in post_out and ".flac" not in post_out
        assert service.sent == []

        # Zero approval cards for any owner-asked read/post
        assert person.asked == []

        # 6. Legitimate multi-node group job/take/track STILL works!
        assert "review" in await _exec(reg, ctx, "music_check", job_id=job_group["id"])
        assert take_group["id"] in await _exec(reg, ctx, "music_wait", job_id=job_group["id"], timeout=1)
        assert take_group["id"] in await _exec(reg, ctx, "music_list_takes", job_id=job_group["id"])
        assert "PUBLIC GROUP CHORUS" in await _exec(reg, ctx, "music_get_lyrics", track_id=trk_group)
        posted_grp = await _exec(reg, ctx, "chat_post_file", take_id=take_group["id"])
        assert "Posted " in posted_grp and len(service.sent) == 1
        assert service.sent[0]["music"]["take_id"] == take_group["id"]

        pl2 = await rig.library.create_playlist("Group Mix", [])
        added_out = await _exec(reg, ctx, "music_playlist_add", playlist_id=pl2["id"],
                                take_id=take_group["id"])
        assert "added 1 track(s)" in added_out


@pytest.mark.asyncio
async def test_multi_node_group_on_foreign_asker_refuses_1to1_without_card_and_cards_name_chat_for_group_job(
        tmp_path, monkeypatch):
    _user_home, dpc_home = await _seed_world(tmp_path, monkeypatch)
    async with Rig(dpc_home) as rig:
        person = Person(True)
        service = _MultiChatService(dpc_home, rig, person)
        data = await _populate_world(rig, dpc_home, service)

        ctx = _multi_ctx(dpc_home, service, FRIENDS_GROUP, asker=FRIEND, turn_class=OTHER_NODE_TURN)
        reg = registry_for(ctx)
        job_dm, take_dm, trk_dm = data["job_dm"], data["take_dm"], data["trk_dm"]
        trk_folder, pl = data["trk_folder"], data["pl_private"]
        job_group, take_group, trk_group = data["job_group"], data["take_group"], data["trk_group"]

        # 1:1 / folder items are refused BEFORE any approval card is raised
        assert "not_your_job" in await _exec(reg, ctx, "music_check", job_id=job_dm["id"])
        assert "not_your_job" in await _exec(reg, ctx, "music_wait", job_id=job_dm["id"], timeout=1)
        assert "not_your_job" in await _exec(reg, ctx, "music_list_takes", job_id=job_dm["id"])
        assert "not_your_job" in await _exec(reg, ctx, "music_get_lyrics", track_id=trk_dm)
        assert "not_your_job" in await _exec(reg, ctx, "music_get_lyrics", track_id=trk_folder)
        assert "not_your_job" in await _exec(reg, ctx, "music_playlist_add", playlist_id=pl["id"], track_ids=[trk_dm])
        assert "not_your_job" in await _exec(reg, ctx, "music_playlist_add", playlist_id=pl["id"], take_id=take_dm["id"])
        assert "not_your_job" in await _exec(reg, ctx, "chat_post_file", take_id=take_dm["id"])
        assert person.asked == [], "refused cross-chat requests must not raise an approval card"
        assert service.sent == []

        # music_list_library asks on a card naming the chat, and when approved shows only group tracks
        lib = json.loads(await _exec(reg, ctx, "music_list_library"))
        assert len(person.asked) == 1
        assert FRIENDS_GROUP in person.asked[-1]["details"]
        assert [t["id"] for t in lib["tracks"]] == [trk_group]
        assert lib["playlists"] == []

        # Group's own job/take/track raises cards naming the chat and succeeds when approved
        assert "review" in await _exec(reg, ctx, "music_check", job_id=job_group["id"])
        assert FRIENDS_GROUP in person.asked[-1]["details"]
        assert take_group["id"] in await _exec(reg, ctx, "music_wait", job_id=job_group["id"], timeout=1)
        assert FRIENDS_GROUP in person.asked[-1]["details"]
        assert take_group["id"] in await _exec(reg, ctx, "music_list_takes", job_id=job_group["id"])
        assert FRIENDS_GROUP in person.asked[-1]["details"]
        assert "PUBLIC GROUP CHORUS" in await _exec(reg, ctx, "music_get_lyrics", track_id=trk_group)
        assert FRIENDS_GROUP in person.asked[-1]["details"]
        assert "Posted " in await _exec(reg, ctx, "chat_post_file", take_id=take_group["id"])
        assert FRIENDS_GROUP in person.asked[-1]["details"]
        assert len(service.sent) == 1


@pytest.mark.asyncio
async def test_one_node_group_and_1to1_allow_owner_1to1_reads_but_block_foreign_asker(tmp_path, monkeypatch):
    _user_home, dpc_home = await _seed_world(tmp_path, monkeypatch)
    async with Rig(dpc_home) as rig:
        person = Person(True)
        service = _MultiChatService(dpc_home, rig, person)
        data = await _populate_world(rig, dpc_home, service)

        job_dm, take_dm, trk_dm = data["job_dm"], data["take_dm"], data["trk_dm"]
        trk_folder, pl = data["trk_folder"], data["pl_private"]

        # One-node group (AI Studio) with Owner asking -> MAY read Owner's 1:1 jobs, library, lyrics, takes
        ctx_solo = _multi_ctx(dpc_home, service, SOLO_GROUP, asker=OWNER_ASKER, turn_class=OWNER_TURN)
        reg_solo = registry_for(ctx_solo)
        assert "review" in await _exec(reg_solo, ctx_solo, "music_check", job_id=job_dm["id"])
        assert take_dm["id"] in await _exec(reg_solo, ctx_solo, "music_wait", job_id=job_dm["id"], timeout=1)
        assert take_dm["id"] in await _exec(reg_solo, ctx_solo, "music_list_takes", job_id=job_dm["id"])
        assert "SECRET 1TO1 VERSE" in await _exec(reg_solo, ctx_solo, "music_get_lyrics", track_id=trk_dm)
        assert "SECRET FOLDER LYRICS" in await _exec(reg_solo, ctx_solo, "music_get_lyrics", track_id=trk_folder)

        lib_solo = json.loads(await _exec(reg_solo, ctx_solo, "music_list_library"))
        assert {trk_dm, trk_folder, data["trk_group"]} <= {t["id"] for t in lib_solo["tracks"]}
        assert any(p["id"] == pl["id"] for p in lib_solo["playlists"])

        pl_solo = await rig.library.create_playlist("Solo Mix", [])
        assert "added 1 track(s)" in await _exec(
            reg_solo, ctx_solo, "music_playlist_add", playlist_id=pl_solo["id"], take_id=take_dm["id"])
        assert "Posted " in await _exec(reg_solo, ctx_solo, "chat_post_file", take_id=take_dm["id"])
        assert len(service.sent) == 1 and service.sent[0]["group_id"] == SOLO_GROUP
        assert person.asked == []

        # One-node group with non-owner asker -> per-chat opacity still blocks 1:1 items
        ctx_solo_foreign = _multi_ctx(dpc_home, service, SOLO_GROUP, asker=FRIEND, turn_class=OWNER_TURN)
        assert "not_your_job" in await _exec(registry_for(ctx_solo_foreign), ctx_solo_foreign,
                                             "music_check", job_id=job_dm["id"])
        assert person.asked == []

        # 1:1 owner chat still works as before
        ctx_dm = _multi_ctx(dpc_home, service, ONE_TO_ONE, asker=OWNER_ASKER, turn_class=OWNER_TURN)
        reg_dm = registry_for(ctx_dm)
        assert "review" in await _exec(reg_dm, ctx_dm, "music_check", job_id=job_dm["id"])
        assert "SECRET 1TO1 VERSE" in await _exec(reg_dm, ctx_dm, "music_get_lyrics", track_id=trk_dm)
        assert "Posted " in await _exec(reg_dm, ctx_dm, "chat_post_file", take_id=take_dm["id"])
        assert person.asked == []


@pytest.mark.asyncio
async def test_multi_node_detection_checks_group_roster_and_discord_bridge_and_fails_closed(tmp_path, monkeypatch):
    _user_home, dpc_home = await _seed_world(tmp_path, monkeypatch)
    async with Rig(dpc_home) as rig:
        person = Person(True)
        service = _MultiChatService(dpc_home, rig, person)
        data = await _populate_world(rig, dpc_home, service)
        job_dm = data["job_dm"]

        # Unclassified turn_class=None on multi-node group -> detected from group_manager, blocked!
        ctx_unclassified_multi = _multi_ctx(
            dpc_home, service, FRIENDS_GROUP, asker=OWNER_ASKER, turn_class=None)
        assert "not_your_job" in await _exec(
            registry_for(ctx_unclassified_multi), ctx_unclassified_multi,
            "music_check", job_id=job_dm["id"])

        # Mis-set turn_class=OWNER_TURN on multi-node group -> group_manager roster catches it!
        ctx_wrong_cls = _multi_ctx(
            dpc_home, service, FRIENDS_GROUP, asker=OWNER_ASKER, turn_class=OWNER_TURN)
        assert "not_your_job" in await _exec(
            registry_for(ctx_wrong_cls), ctx_wrong_cls, "music_check", job_id=job_dm["id"])

        # Discord bridge with 1 member -> treated as multi-node (blocked)!
        service._groups["group-bridge"] = SimpleNamespace(
            group_id="group-bridge", name="Discord", members=[THIS_NODE], is_discord_bridge=True)
        ctx_bridge = _multi_ctx(dpc_home, service, "group-bridge", asker=OWNER_ASKER, turn_class=None)
        assert "not_your_job" in await _exec(
            registry_for(ctx_bridge), ctx_bridge, "music_check", job_id=job_dm["id"])

        # Unknown group (not in group_manager) -> fails closed!
        ctx_unknown = _multi_ctx(dpc_home, service, "group-missing", asker=OWNER_ASKER, turn_class=None)
        assert "not_your_job" in await _exec(
            registry_for(ctx_unknown), ctx_unknown, "music_check", job_id=job_dm["id"])

        # Unclassified turn_class=None on verified 1-node group -> recognized as 1-node, allowed!
        ctx_unclassified_solo = _multi_ctx(
            dpc_home, service, SOLO_GROUP, asker=OWNER_ASKER, turn_class=None)
        assert "review" in await _exec(
            registry_for(ctx_unclassified_solo), ctx_unclassified_solo,
            "music_check", job_id=job_dm["id"])

