"""ADR-051 row B4 with D45-D47: the owner's agent proposes a region edit through `music_propose_job` (type
`edit`: `track_id`, `action`, `region`, `text` in params). Core's own checks run before the person is
asked; the approval card names the action and the region; the job's region comment is the agent's. An
agent never gets a cut-with-shift removal (decision 27: the person's word only). Live path as in the
music tools test: the real registry and dispatcher, the real command mixin over a real store and library.
"""

from __future__ import annotations

import wave

import numpy as np
import pytest

from dpc_client_core.dpc_agent.tools import music as music_tools
from dpc_client_core.music import job_schemas as js
from tests._acestep_helpers import Rig
from tests.test_the_owners_agent_makes_music_through_the_music_tools_with_approval_limits_and_the_self_wait_rule import (  # noqa: E501
    MusicService, Person, _clean, call, home, make_ctx, registry_for,
)

__all__ = ["_clean", "home"]   # the fixtures, used by name


async def base_track(rig, home, seconds=6.0):
    path = home / "songs" / "Night Drive.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(seconds * 48000)) / 48000
    pcm = (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(pcm.tobytes())
    entry = await rig.library.register_generated(str(path), job_id="job_" + "1" * 16)
    rig.library._entries[entry["id"]]["duration_s"] = seconds   # no ffprobe here
    rig.store.capabilities._ffmpeg_probe = lambda: True          # what a machine with ffmpeg answers
    return entry


@pytest.mark.asyncio
async def test_an_approved_removal_is_one_job_whose_card_named_the_region_and_whose_comment_is_the_agents(home):
    async with Rig(home, start_runner=False) as rig:
        entry = await base_track(rig, home)
        person = Person(True)
        ctx = make_ctx(home, MusicService(home, rig, person))
        out = await call(registry_for(ctx), ctx, "music_propose_job", type="edit",
                         params={"track_id": entry["id"], "action": "remove_region",
                                 "region": {"t_start": 1.0, "t_end": 2.5}, "text": "a cough"})
        assert out.startswith("Proposed (approved by the person)."), out
        [asked] = person.asked
        assert asked["task_type"] == "music_job" and "remove 0:01.0-0:02.5" in asked["about"]
        assert "no GPU" in asked["about"] and "cost" not in asked              # a removal costs no card time
        music = asked["music"]
        assert (music["type"], music["action"], music["region_text"], music["variants"], music["uses_gpu"]) == \
            ("edit", "remove_region", "remove 0:01.0-0:02.5", 1, False)
        [job] = rig.store.list_jobs()
        assert (job["type"], job["action"], job["track_id"]) == ("edit", "remove_region", entry["id"])
        assert job["requested_by"]["kind"] == "agent" and job["prompt"] == "a cough"
        comments = (await ctx.dpc_service.list_music_comments(track_id=entry["id"]))["comments"]
        assert [(c["id"], c["author"]["kind"], c["action"]) for c in comments] == [(job["comment_id"], "agent", "remove_region")]


@pytest.mark.asyncio
async def test_a_repaint_card_states_the_gpu_cost_and_a_cut_with_shift_is_refused_before_anyone_is_asked(home):
    async with Rig(home, start_runner=False) as rig:
        entry = await base_track(rig, home)
        person = Person(False)
        ctx = make_ctx(home, MusicService(home, rig, person))
        reg = registry_for(ctx)
        cut = await call(reg, ctx, "music_propose_job", type="edit",
                         params={"track_id": entry["id"], "action": "remove_region", "cut_with_shift": True,
                                 "region": {"t_start": 4.0, "t_end": 5.5}, "text": "cut the tail"})
        assert cut.startswith("⚠️") and "only the person" in cut and person.asked == []
        out = await call(reg, ctx, "music_propose_job", type="edit", turn="turn-2",
                         params={"track_id": entry["id"], "action": "regenerate_region", "caption": "synthwave",
                                 "region": {"t_start": 1.0, "t_end": 3.0}, "text": "the snare is off"})
        assert "not proposed" in out and rig.store.list_jobs() == []
        [asked] = person.asked
        assert "regenerate 0:01.0-0:03.0" in asked["about"] and "on the GPU" in asked["about"]
        assert asked["cost"]                                             # the repaint holds the GPU card


@pytest.mark.asyncio
async def test_an_edit_without_its_track_action_or_region_is_refused_in_words(home):
    async with Rig(home, start_runner=False) as rig:
        ctx = make_ctx(home, MusicService(home, rig, Person(True)))
        reg = registry_for(ctx)
        for params, words in (({"action": "remove_region", "region": {"t_start": 1, "t_end": 2}}, "track_id"),
                              ({"track_id": "trk_" + "0" * 16, "action": "extend_ending",
                                "region": {"t_start": 1, "t_end": 2}}, "params.action"),
                              ({"track_id": "trk_" + "0" * 16, "action": "fix_seam"}, "params.region")):
            out = await call(reg, ctx, "music_propose_job", type="edit", params=params)
            assert out.startswith("⚠️ music_propose_job:") and words in out, out
        assert rig.store.list_jobs() == []


def test_the_tool_declares_both_job_types():
    entry = next(e for e in music_tools.get_tools() if e.name == "music_propose_job")
    schema = entry.schema if hasattr(entry, "schema") else entry.parameters
    assert js.GENERATE_FROM_TEXT in str(schema) and "edit" in str(schema) and "regenerate_region" in str(schema)
