"""An `edit` job in the store and at the command door (ADR-051 row B4; SPEC §6 refusals, §4 takes): a built
action is checked against what the library knows of the track (a web track is `listen_only_source`, an
unhashed one `track_not_ready`), a repaint follows acestep.cpp's availability while a removal does not,
its takes are `edit` takes of the job's track and region (at most `params.takes`), and the door makes
the region comment for a proposal that has none, before nothing else can refuse it.
"""

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.capabilities import EngineStatus, MusicCapabilities
from dpc_client_core.music.jobs import MusicJobError, MusicJobStore
from tests._music_job_commands_helpers import library_tracks, service_with_engine, without_tools
from tests._music_jobs_helpers import installed_probe

TRACK, VERSION, COMMENT = "trk_" + "1" * 16, "ver_" + "0" * 16, "cmt_" + "2" * 16
SOURCE = {"kind": "local", "content_hash": "md5:" + "a" * 32, "duration_s": 120.0}


def store(tmp_path, probe=installed_probe, ffmpeg=True):
    s = MusicJobStore(tmp_path, capabilities=MusicCapabilities(probe, ffmpeg_probe=lambda: ffmpeg))
    s.open()
    return s


def edit(s, action="regenerate_region", params=None, region=None, source=SOURCE, **kw):
    return s.create_job("edit", {"caption": "synthwave"} if params is None else params, track_id=TRACK, base_version_id=VERSION,
                        comment_id=COMMENT, action=action, region=region or {"t_start": 10, "t_end": 14},
                        prompt="the vocal cracks", source=source, **kw)


def test_a_built_edit_queues_with_its_params_completed_and_the_source_recorded(tmp_path):
    job = edit(store(tmp_path))
    assert job["state"] == js.QUEUED and job["type"] == "edit" and job["action"] == "regenerate_region"
    p = job["params"]
    assert (p["takes"], p["engine"], p["source_content_hash"], p["source_duration_s"]) == \
        (2, "acestep:repaint", SOURCE["content_hash"], 120.0)
    assert job["prompt"] == "the vocal cracks" and job["region"] == {"t_start": 10, "t_end": 14}
    js.validate_job(job)


def test_a_web_track_cannot_be_edited_and_an_unhashed_one_not_yet(tmp_path):
    s = store(tmp_path)
    with pytest.raises(MusicJobError) as web:
        edit(s, source={**SOURCE, "kind": "web"})
    assert web.value.code == "listen_only_source"
    with pytest.raises(MusicJobError) as pending:
        edit(s, source={**SOURCE, "content_hash": None})
    assert pending.value.code == "track_not_ready"
    with pytest.raises(MusicJobError) as unknown_length:
        edit(s, source={**SOURCE, "duration_s": None})
    assert unknown_length.value.code == "track_not_ready"
    with pytest.raises(MusicJobError) as no_source:
        edit(s, source=None)
    assert no_source.value.code == "invalid_source"
    with pytest.raises(MusicJobError) as bad:
        edit(s, region={"t_start": 0, "t_end": 45})
    assert bad.value.code == "invalid_params" and bad.value.field == "region"


def test_a_repaint_follows_acestep_while_a_removal_runs_without_an_engine(tmp_path):
    def not_installed():
        return EngineStatus(state="not_installed", reason="model file(s) missing")
    s = store(tmp_path, probe=not_installed)
    repaint = edit(s)
    assert repaint["state"] == js.UNSUPPORTED and "acestep.cpp" in repaint["state_reason"]
    assert "model file(s) missing" in repaint["state_reason"]
    removal = edit(s, "remove_region", params={})
    assert removal["state"] == js.QUEUED
    # without ffmpeg nothing can be decoded, so no edit runs
    none = edit(store(tmp_path / "b", ffmpeg=False), "remove_region", params={})
    assert none["state"] == js.UNSUPPORTED and "ffmpeg" in none["state_reason"]


def test_a_note_makes_no_job_and_extend_ending_is_still_planned(tmp_path):
    s = store(tmp_path)
    note = edit(s, "note", params={})
    assert note["state"] == js.UNSUPPORTED and "makes no job" in note["state_reason"]
    ext = edit(s, "extend_ending", params={})
    assert ext["state"] == js.UNSUPPORTED and ext["state_reason"].startswith("planned, not built")


def test_an_edit_job_takes_at_most_its_takes_as_edit_takes_of_its_track_and_region(tmp_path):
    s = store(tmp_path)
    job = edit(s)
    s.write_payload(job["id"], "edit.request.json", b"{}")
    s.write_payload(job["id"], "synth.request.json", b"{}", endpoint="/synth")
    s.start_job(job["id"])
    with pytest.raises(MusicJobError, match="no take"):
        s.finish_job(job["id"])          # an edit with nothing to review fails instead

    def add(v):
        return s.add_take(job["id"], variant=v, file_uri=f"file:///g/{job['id']}/v{v}.flac", content_hash=f"md5:{v}",
                          title=f"v{v}", seed=7 + v - 1, synth_batch_size=2, synth_batch_index=v - 1,
                          payload="synth.request.json", duration_s=120.0, duration_requested_s=120.0)
    takes = [add(1), add(2)]
    for t in takes:
        assert (t["kind"], t["track_id"], t["base_version_id"], t["source_track_id"]) == ("edit", TRACK, VERSION, TRACK)
        assert t["region"] == {"t_start": 10, "t_end": 14} and t["duration_requested_s"] == 120.0
        js.validate_take(t)
    with pytest.raises(MusicJobError) as third:
        add(3)
    assert third.value.field == "variant"
    s.finish_job(job["id"])
    s.keep_take(takes[1]["id"])
    assert s.get_job(job["id"])["state"] == js.ACCEPTED


def test_a_removal_take_carries_no_seed_and_the_restart_rule_counts_takes(tmp_path):
    s = store(tmp_path)
    job = edit(s, "remove_region", params={})
    s.write_payload(job["id"], "edit.request.json", b"{}")
    s.start_job(job["id"])
    take = s.add_take(job["id"], variant=1, file_uri="file:///g/x.flac", content_hash="md5:1", title="cut",
                      seed=None, synth_batch_size=None, synth_batch_index=None, payload="edit.request.json")
    assert "seed" not in take and "synth_batch_size" not in take and take["duration_requested_s"] == 120.0
    again = MusicJobStore(tmp_path, capabilities=MusicCapabilities(installed_probe, ffmpeg_probe=lambda: True))
    again.open()
    after = again.get_job(job["id"])
    assert after["state"] == js.REVIEW and "1 of 1" in after["state_reason"]


def test_the_take_of_a_library_track_is_found_for_an_edits_parent(tmp_path):
    s = store(tmp_path)
    job = edit(s, "remove_region", params={})
    s.write_payload(job["id"], "edit.request.json", b"{}")
    s.start_job(job["id"])
    take = s.add_take(job["id"], variant=1, file_uri="file:///g/x.flac", content_hash="md5:1", title="cut",
                      seed=None, synth_batch_size=None, synth_batch_index=None, payload="edit.request.json",
                      library_track_id="trk_" + "9" * 16)
    assert s.find_take_by_library_track("trk_" + "9" * 16)["id"] == take["id"]
    assert s.find_take_by_library_track("trk_" + "8" * 16) is None


# -- the command door ----------------------------------------------------------------------------

async def _service_with_a_track(tmp_path, monkeypatch):
    without_tools(monkeypatch)
    home = tmp_path / "dpc"          # the music folder must lie outside the DPC home
    service = service_with_engine(home)
    service._music_jobs_instance = store(home)
    ids = await library_tracks(service, tmp_path / "songs", names=("a.mp3",))
    lib = await service._music()
    tid = ids["a.mp3"]
    lib._entries[tid]["duration_s"] = 60.0          # no ffprobe here: the length a probe would give
    return service, tid


@pytest.mark.asyncio
async def test_the_door_makes_the_region_comment_for_an_edit_proposed_without_one(tmp_path, monkeypatch):
    service, tid = await _service_with_a_track(tmp_path, monkeypatch)
    try:
        answer = await service.propose_music_job(type="edit", params={}, track_id=tid, action="remove_region",
                                                 region={"t_start": 5, "t_end": 8}, prompt="a cough here")
        assert answer["status"] == "ok", answer
        job = answer["job"]
        assert job["state"] == js.QUEUED and job["base_version_id"] == "ver_" + "0" * 16
        comments = (await service.list_music_comments(track_id=tid))["comments"]
        assert [(c["id"], c["action"], c["t_start"], c["t_end"], c["text"]) for c in comments] == \
            [(job["comment_id"], "remove_region", 5.0, 8.0, "a cough here")]
        assert comments[0]["author"] == {"kind": "human", "name": "Me"}

        # an agent's edit: its comment names the agent; a refused one leaves no comment behind
        agent = {"kind": "agent", "agent_id": "agent_ark", "conversation_id": "c1", "turn_id": "t1"}
        refused = await service.propose_music_job(type="edit", params={"cut_with_shift": True}, track_id=tid,
                                                  action="remove_region", region={"t_start": 52, "t_end": 58},
                                                  prompt="cut the tail", requested_by=agent)
        assert refused["status"] == "error" and refused["field"] == "cut_with_shift"
        assert len((await service.list_music_comments(track_id=tid))["comments"]) == 1
        ok = await service.propose_music_job(type="edit", params={}, track_id=tid, action="remove_region",
                                             region={"t_start": 52, "t_end": 58}, prompt="cut the tail",
                                             requested_by=agent)
        assert ok["status"] == "ok", ok
        again = await service.propose_music_job(type="edit", params={}, track_id=tid, action="remove_region",
                                                region={"t_start": 52, "t_end": 58}, prompt="cut the tail",
                                                requested_by=agent)
        assert again["job"]["id"] == ok["job"]["id"]                      # the turn's repeat, no second comment
        authors = [c["author"] for c in (await service.list_music_comments(track_id=tid))["comments"]]
        assert authors == [{"kind": "human", "name": "Me"}, {"kind": "agent", "name": "agent_ark"}]
    finally:
        await service.stop_music_library()


@pytest.mark.asyncio
async def test_check_music_job_answers_an_edit_without_making_a_comment_or_a_job(tmp_path, monkeypatch):
    service, tid = await _service_with_a_track(tmp_path, monkeypatch)
    try:
        agent = {"kind": "agent", "agent_id": "agent_ark", "conversation_id": "c1", "turn_id": "t9"}
        answer = await service.check_music_job(type="edit", params={"caption": "lo-fi"}, track_id=tid,
                                               action="regenerate_region", region={"t_start": 1, "t_end": 3},
                                               prompt="x", requested_by=agent)
        assert answer["status"] == "ok" and answer["params"]["takes"] == 2 and answer["repeat"] is None
        assert (await service.list_music_comments(track_id=tid))["comments"] == []
        assert (await service.list_music_jobs())["jobs"] == []
        missing = await service.check_music_job(type="edit", params={}, track_id="trk_" + "0" * 16,
                                                action="remove_region", region={"t_start": 1, "t_end": 3})
        assert missing["status"] == "error" and missing["code"] == "invalid_source"
    finally:
        await service.stop_music_library()
