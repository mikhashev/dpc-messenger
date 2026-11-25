"""A cover on yue2.cpp by default, Ruach Studio's cover chain, a cover as long as its source, the web-track stub
(Mike 2026-11-26; ADR-051 row B5), and Zcode's review of 16e6549a (group #188).

- `music/cover_score.py`, ported from Ruach Studio: the level a recording is heard at by the transcriber, the
  Vocal voice of a score, the score's parts, Match sections, and the style made to agree with the score.
- The params: yue2 is the default cover engine and `cot` melody its default; `vocal_only` and `lyrics_times`;
  the engines' real floors and ceilings for a cover (`source_too_short`, `source_too_long`) in place of DPC's
  10 s minimum; the reference audio's cap; `lyrics_source`.
- The run: the score chain on the fake yue2 server (levelled source, Vocal voice, agreed style, matched lyrics,
  `duration` and `min_tokens` from the source), QA mode `same` for both engines' covers.
- The web stub: `web_source_pending_integration` at the door and at run time, from `web_sources.py`.
- Zcode #188: the licence follows stored lyrics only (finding 1), the source size cap and an upload timeout that
  scales (2), the start-up sweep of song-job temp files (3), `lyrics_unreadable` (4).
"""

import json
import math
import sys
import types
from pathlib import Path

import pytest

from dpc_client_core.music import cover_score as cs
from dpc_client_core.music import engine_base
from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import qa as qa_module
from dpc_client_core.music import region_edit as re_
from dpc_client_core.music import similar
from dpc_client_core.music import web_sources
from dpc_client_core.music.commands import MusicCommandsMixin
from dpc_client_core.music.jobs import MusicJobError
from tests._acestep_helpers import fake_qa, free_env, install_fake_engine
from tests._yue2_helpers import DualRig, install_fake_yue2
from tests.test_a_song_from_a_library_track_is_a_cover_a_same_style_song_or_a_song_from_its_lyrics import (
    DONE, LOCAL, TRACK, VERSION, SongRig, fake_decoder, sidecar_of, store_at, write_base)

sys.path.insert(0, str(Path(__file__).parent))     # the fake server imports its sibling as a top-level module
from _fake_yue2_server import NATIVE_ABC  # noqa: E402

RATE = 48000
# The lyrics of NATIVE_ABC's song, untagged, with the times its lines are sung at in the source (a bar is 2 s:
# the intro is 0-2 s, the verse 2-6 s, the chorus 6-10 s).
UNTAGGED = ["night drive, city lights", "neon in the rain", "we never stop", "we never stop"]
TIMES = [2.1, 4.0, 6.1, 8.0]


# -- cover_score (ported from Ruach Studio) -----------------------------------------------------------


def test_a_recording_too_loud_for_the_transcriber_gets_one_gain_and_one_within_bounds_none():
    assert cs.transcriber_gain_db(-20.0, -6.0) is None                      # within bounds: as it is
    assert cs.transcriber_gain_db(-9.0, -3.0) == -7.0                       # to -16 LUFS
    assert cs.transcriber_gain_db(-15.0, -0.2) == -1.3                      # a hot peak held at -1.5 dBFS
    assert cs.transcriber_gain_db(None, None) is None and cs.transcriber_gain_db(float("-inf"), None) is None
    assert cs.transcriber_gain_db(None, 0.0) == -1.5                        # no LUFS: the peak half still applies


def test_the_vocal_voice_alone_silences_the_ins_voice_and_keeps_bars_chords_and_sections():
    only = cs.keep_vocal_voice(NATIVE_ABC)
    lines = only.split("\n")
    assert lines[:8] == NATIVE_ABC.split("\n")[:8]                           # the header as it was
    ins = [lines[i + 1] for i, x in enumerate(lines) if x == "V: Ins"]
    assert ins == ["z4z4z4z4|", "Z2|", "z4z4z4z4|Z|"]                         # same lengths, no notes
    assert '"C"C4D4E4F4|"G"G4A4B4c4|' in lines and "% chorus" in lines
    assert cs.keep_vocal_voice("no score") == "no score"


def test_the_scores_parts_have_their_seconds_and_whether_the_voice_sings_in_them():
    parts = cs.score_parts(NATIVE_ABC)
    assert parts["bpm"] == 120.0 and cs.bar_seconds(NATIVE_ABC) == 2.0
    assert [(p["label"], p["start_s"], p["end_s"], p["sung"]) for p in parts["parts"]] == [
        ("intro", 0.0, 2.0, False), ("verse", 2.0, 6.0, True), ("chorus", 6.0, 10.0, True)]


def test_match_sections_retags_blocks_by_where_they_are_sung_and_adds_the_instrumental_intro():
    lines = ["[Chorus]", "night drive, city lights", "neon in the rain", "", "[Verse]", "we never stop"]
    rows = [{"text": t, "start": s} for t, s in zip(["night drive, city lights", "neon in the rain", "we never stop"],
                                                    [2.1, 4.0, 6.1])]
    m = cs.match_sections(NATIVE_ABC, lines, rows)
    assert m["lyrics"] == "[Intro]\n\n[Verse]\nnight drive, city lights\nneon in the rain\n\n[Chorus]\nwe never stop"
    assert [b["status"] for b in m["blocks"]] == ["renamed", "renamed"] and not m["unchanged"]
    assert m["filled"] == [{"section": 1, "tag": "Intro", "copied_from": None}]
    with pytest.raises(ValueError):
        cs.match_sections("X:1\nT:\nQ:1/4=100\nK:C\nV: Vocal\nC4|", lines, rows)   # no named sections


def test_untagged_lyrics_are_cut_at_the_section_boundaries_by_their_line_times_first():
    tagged = cs.pre_tag(UNTAGGED, TIMES, NATIVE_ABC)
    assert tagged == ["[Verse]", "night drive, city lights", "neon in the rain", "", "[Chorus]", "we never stop",
                      "we never stop"]
    assert cs.pre_tag(["[verse]"] + UNTAGGED, [None] + TIMES, NATIVE_ABC) is None      # tagged already
    assert cs.pre_tag(UNTAGGED, [2.1, None, 6.1, 8.0], NATIVE_ABC) is None              # a line without a time


def test_the_style_takes_the_scores_tempo_and_drops_a_key_that_disagrees():
    assert cs.agree_style("synthwave, warm pads", NATIVE_ABC) == ("synthwave, warm pads, 120 BPM",
                                                                  ["added the score's tempo 120 BPM"])
    style, changes = cs.agree_style("rock, 96 BPM, A minor, male vocal", NATIVE_ABC)
    assert style == "rock, 120 BPM, male vocal" and len(changes) == 2
    assert cs.agree_style("jazz, C major, 120 bpm", NATIVE_ABC) == ("jazz, C major, 120 bpm", [])
    assert cs.agree_style("a major label sound", NATIVE_ABC)[0] == "a major label sound, 120 BPM"


# -- the params ---------------------------------------------------------------------------------------


def test_a_cover_runs_on_yue2_by_default_with_the_lead_melody_and_is_as_long_as_its_source():
    p = js.normalize_params_song("cover", {"caption": "x", "lyrics": UNTAGGED}, track_id=TRACK, source=LOCAL)
    assert (p["engine"], p["cot"], p["vocal_only"], p["lyrics_times"]) == ("yue2", "melody", False, None)
    assert p["duration_s"] == 30.5 and p["lyrics_source"] == "given" and js.check_params_song("cover", p) is None
    assert js.SONG_ENGINES["cover"] == ("yue2", "acestep")
    v = js.normalize_params_song("cover", {"caption": "x", "lyrics": UNTAGGED, "vocal_only": True,
                                           "lyrics_times": TIMES, "lyrics_source": "stored"}, track_id=TRACK,
                                 source=LOCAL)
    assert v["vocal_only"] is True and v["lyrics_times"] == TIMES and js.check_params_song("cover", v) is None
    for bad, field in (({"vocal_only": True, "cot": "full"}, "vocal_only"), ({"vocal_only": "yes"}, "vocal_only"),
                       ({"lyrics_times": [1.0]}, "lyrics_times"), ({"lyrics_source": "web"}, "lyrics_source")):
        with pytest.raises(js.ParamsError) as err:
            js.normalize_params_song("cover", {"caption": "x", "lyrics": UNTAGGED, **bad}, track_id=TRACK, source=LOCAL)
        assert err.value.field == field
    with pytest.raises(js.ParamsError, match="on yue2"):
        js.normalize_params_song("cover", {"caption": "x", "engine": "acestep", "lyrics_mode": "none",
                                           "vocal_only": False}, track_id=TRACK,
                                 source=LOCAL)
    with pytest.raises(js.SongParamsError) as err:                         # reuse with nothing stored, on yue2
        js.normalize_params_song("cover", {"caption": "x", "lyrics_mode": "reuse"}, track_id=TRACK, source=LOCAL)
    assert err.value.code == "missing_lyrics"


@pytest.mark.parametrize("engine, short, fine, long_", [
    ("acestep", 0.01, 5.0, 601.0),             # DPC's 10 s minimum is gone; one latent frame is the floor
    ("yue2", 0.03, 5.0, js.YUE2_COVER_MAX_S + 1),
])
def test_a_cover_is_refused_only_below_or_above_what_its_engine_can_do(engine, short, fine, long_):
    params = {"caption": "x", "engine": engine, "lyrics": UNTAGGED}
    ok = js.normalize_params_song("cover", params, track_id=TRACK, source=dict(LOCAL, duration_s=fine))
    assert ok["duration_s"] == fine
    for seconds, code in ((short, "source_too_short"), (long_, "source_too_long")):
        with pytest.raises(js.SongParamsError) as err:
            js.normalize_params_song("cover", params, track_id=TRACK, source=dict(LOCAL, duration_s=seconds))
        assert err.value.code == code and err.value.field == "track_id"
    assert math.isclose(js.YUE2_COVER_MAX_S, (24576 - 1) / 25 - 1)


def test_a_reference_audio_longer_than_the_engines_longest_song_is_refused_at_the_door():
    """Zcode #188 finding 2: the whole source was decoded and uploaded whatever its length."""
    params = {"caption": "x", "reference": "audio"}
    js.normalize_params_song("generate_similar", params, track_id=TRACK,
                             source=dict(LOCAL, duration_s=js.REFERENCE_AUDIO_MAX_S))
    with pytest.raises(js.SongParamsError) as err:
        js.normalize_params_song("generate_similar", params, track_id=TRACK,
                                 source=dict(LOCAL, duration_s=js.REFERENCE_AUDIO_MAX_S + 1))
    assert err.value.code == "source_too_long" and js.REFERENCE_AUDIO_MAX_S == 630.0
    with pytest.raises(js.SongParamsError) as err:
        js.normalize_params_song("generate_similar", params, track_id=TRACK, source=dict(LOCAL, duration_s=None))
    assert err.value.code == "track_not_ready"
    with pytest.raises(MusicJobError) as door:
        store_at(Path(__import__("tempfile").mkdtemp())).create_job(
            "cover", {"caption": "x", "engine": "acestep", "lyrics_mode": "none"}, track_id=TRACK,
            base_version_id=VERSION, source=dict(LOCAL, duration_s=700.0))
    assert door.value.code == "source_too_long"


def test_a_multipart_upload_gets_more_time_the_bigger_it_is():
    assert engine_base.upload_timeout_s(0) == 300.0
    ten_minutes = 10 * 60 * RATE * 2 * 3                                  # a 10 min 48 kHz stereo 24-bit WAV
    assert engine_base.upload_timeout_s(ten_minutes) > 300.0 + 150


# -- the door and the commands ---------------------------------------------------------------------------


class _Lyrics:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error

    async def get(self, track_id):
        if self.error:
            raise self.error
        return self.answer


def _commands(lyrics):
    fake = types.SimpleNamespace()

    async def music_lyrics():
        return lyrics
    fake._music_lyrics = music_lyrics
    fake._music_song_lyrics = lambda tid: MusicCommandsMixin._music_song_lyrics(fake, tid)
    return lambda job_type, params: MusicCommandsMixin._music_song_params(fake, job_type, params, TRACK)


@pytest.mark.asyncio
async def test_reused_lyrics_are_the_stored_ones_with_their_times_and_other_lines_are_refused():
    """Zcode #188 finding 1: `reuse` with arbitrary text passed and the take inherited the source's licence."""
    stored = {"lyrics": {"lines": [{"t_start": t, "text": x} for x, t in zip(UNTAGGED, TIMES)] + [
        {"t_start": None, "text": ""}]}}
    fill = _commands(_Lyrics(stored))
    out = await fill("cover", {"caption": "x", "lyrics_source": "stored", "lyrics_times": [0]})
    assert out["lyrics"] == UNTAGGED and out["lyrics_source"] == "stored" and out["lyrics_times"] == TIMES
    same = await fill("cover", {"caption": "x", "lyrics": "\n".join(UNTAGGED) + "\n"})
    assert same["lyrics_source"] == "stored"                               # the same lines, given again
    ace = await fill("cover", {"caption": "x", "engine": "acestep"})
    assert "lyrics_times" not in ace and ace["lyrics_source"] == "stored"   # line times are a yue2 cover's
    with pytest.raises(MusicJobError) as err:
        await fill("generate_from_lyrics", {"caption": "x", "lyrics": ["my own words"]})
    assert err.value.code == "reuse_lyrics_differ" and err.value.field == "lyrics"
    given = await fill("generate_similar", {"caption": "x", "lyrics_mode": "new", "lyrics_source": "stored"})
    assert "lyrics_source" not in given                                    # only this door says "stored"


@pytest.mark.asyncio
async def test_lyrics_that_cannot_be_read_are_lyrics_unreadable_never_none_stored():
    """Zcode #188 finding 4: a read error was swallowed and a cover went without lyrics."""
    with pytest.raises(MusicJobError) as err:
        await _commands(_Lyrics(error=OSError("disk gone")))("cover", {"caption": "x"})
    assert err.value.code == "lyrics_unreadable" and "disk gone" in err.value.message
    none = await _commands(_Lyrics({"lyrics": None}))("cover", {"caption": "x"})
    assert none["lyrics_mode"] == "none" and "lyrics" not in none          # a real absence: no lyrics


def test_the_web_stub_is_one_interface_planned_in_the_capability_report():
    with pytest.raises(web_sources.WebSourceNotAvailable) as err:
        web_sources.fetch_audio({"id": TRACK, "source": {"kind": "web"}}, Path("."))
    assert err.value.code == "web_source_pending_integration"
    assert "Yandex Music / SoundCloud integration" in err.value.message
    assert web_sources.integration_status()["status"] == "planned" and not web_sources.can_fetch({})


# -- the runs ---------------------------------------------------------------------------------------


def _qa_recorder(calls):
    def meter(flac, **kw):
        calls.append(kw)
        return fake_qa(flac, **kw)
    return meter


def _yue2_rig(tmp_path, monkeypatch, modes, record):
    monkeypatch.setenv("FAKE_YUE2_MODE", modes)
    monkeypatch.setenv("FAKE_YUE2_RECORD", str(record))
    free_env(monkeypatch)
    models = install_fake_yue2(tmp_path, voices=())
    install_fake_engine(tmp_path)
    for rel in ("serveurperso/SheetSage2-Q8_0.gguf", "serveurperso/MERT-v2-FullSong-Q8_0.gguf"):
        (models / rel).write_bytes(b"gguf")
    rig = DualRig(tmp_path)
    rig.store.capabilities._ffmpeg_probe = lambda: True
    rig.runner._decode_control = fake_decoder
    return rig


@pytest.mark.asyncio
async def test_a_yue2_cover_hears_a_loud_source_levelled_sings_the_vocal_line_with_matched_lyrics_and_asks_for_its_length(
        tmp_path, monkeypatch):
    record = tmp_path / "posts"
    rig = _yue2_rig(tmp_path, monkeypatch, "native_abc,follow_length", record)
    calls = []
    rig.runner._qa_measure = _qa_recorder(calls)
    loud = {"lufs_i": -9.0, "true_peak_dbtp": -0.1, "lra": 3.0}
    real_measure = rig.runner._measure
    rig.runner._measure = lambda p: loud if p.name == similar.LEVELLED_PART else real_measure(p)   # the mono mix
    async with rig:
        path = write_base(tmp_path / "music" / "Night Drive.wav", seconds=6.0)
        entry = await rig.library.register_generated(str(path), job_id="job_" + "1" * 16, lineage={"variant": 1})
        source = {"kind": "local", "content_hash": entry["content_hash"], "duration_s": 6.0, "title": "Night Drive"}
        job = rig.store.create_job("cover", {"caption": "russian rock, 90 BPM, D minor", "lyrics": UNTAGGED,
                                             "lyrics_times": TIMES, "lyrics_source": "stored", "vocal_only": True,
                                             "variants": 1, "seed": 5},
                                   track_id=entry["id"], base_version_id=VERSION, source=source)
        assert job["params"]["engine"] == "yue2" and job["params"]["duration_s"] == 6.0
        rig.runner.wake()
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        asked = json.loads(sorted(record.glob("*-transcribe.json"))[-1].read_bytes())
        assert asked["melody_only"] is True                                 # cot melody, the default
        assert asked["audio_bytes"] < 6 * RATE * 6 // 2 + 1000               # the mono levelled file, not stereo
        body = json.loads(rig.store.read_payload(job["id"], "synth.v1.request.json"))
        assert body["abc"] == cs.keep_vocal_voice(NATIVE_ABC) and body["cot"] == "melody"
        assert body["style"] == "russian rock, 120 BPM"                      # tempo agreed, a wrong key dropped
        assert body["lyrics"] == ("[Intro]\n\n[Verse]\nnight drive, city lights\nneon in the rain\n\n"
                                  "[Chorus]\nwe never stop\nwe never stop")
        assert body["duration"] == 7.0 and body["semantic_sampling"] == {"min_tokens": 150}   # the source's 6 s
        song = sidecar_of(rig.store.list_takes(job["id"])[0])["song"]
        assert song["transcribe_level"]["gain_db"] == -7.0 and song["vocal_only"] is True
        assert song["section_match"]["status"] == "applied" and song["section_match"]["pre_tagged"] is True
        assert len(song["style_changes"]) == 2 and song["score_sent_sha256"] != song["score_abc_sha256"]
        assert song["length"]["source_s"] == 6.0 and song["lyrics_source"] == "stored"
        assert calls[-1]["length_mode"] == "same" and calls[-1]["requested_s"] == 6.0
        assert calls[-1]["same_tolerance_s"] == 2.0
        assert not list((rig.runner.generated_root / job["id"]).glob("*.wav.part"))


@pytest.mark.asyncio
async def test_an_acestep_cover_of_a_short_source_runs_and_qa_measures_it_against_the_sources_length(
        tmp_path, monkeypatch):
    free_env(monkeypatch)
    install_fake_engine(tmp_path)
    async with SongRig(tmp_path) as rig:
        calls = []
        rig.runner._qa_measure = _qa_recorder(calls)
        entry, _ = await rig.source(seconds=4.0)
        job = rig.propose("cover", entry, {"caption": "punk", "engine": "acestep", "lyrics_mode": "none",
                                           "variants": 1}, seconds=4.0)
        assert job["state"] == js.QUEUED and job["params"]["duration_s"] == 4.0   # under the old 10 s minimum
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        assert calls and all(c["length_mode"] == "same" and c["requested_s"] == 4.0
                             and c["same_tolerance_s"] == 0.05 for c in calls)


def test_qa_flags_a_cover_far_from_its_sources_length_and_passes_one_within_its_engines_tolerance():
    near = qa_module.length_gate(180.0, 181.2, "same", same_tolerance_s=2.0)
    far = qa_module.length_gate(180.0, 150.0, "same", same_tolerance_s=2.0)
    assert near["status"] == js.GATE_PASS and "as long as its source" in near["detail"]
    assert far["status"] == js.GATE_FAIL and far["detail"].startswith("length_mismatch")
    edit = qa_module.length_gate(10.0, 10.01, "same")                      # an edit keeps its millisecond rule
    assert edit["status"] == js.GATE_FAIL and "decision 23" in edit["detail"]


@pytest.mark.asyncio
async def test_a_generate_from_lyrics_take_inherits_the_licence_only_for_the_tracks_stored_lyrics(
        tmp_path, monkeypatch):
    free_env(monkeypatch)
    install_fake_engine(tmp_path)
    async with SongRig(tmp_path) as rig:
        entry, _ = await rig.source()
        parents = {}
        for basis in ("given", "stored"):
            job = rig.propose("generate_from_lyrics", entry, {"caption": "folk", "lyrics": UNTAGGED, "engine": "acestep",
                                                              "variants": 1, "lyrics_source": basis})
            done = await rig.wait_for(job["id"], DONE)
            assert done["state"] == js.REVIEW, done.get("state_reason")
            side = sidecar_of(rig.store.list_takes(job["id"])[0])
            parents[basis] = [c for c in side["licence"]["components"] if c.get("role") == "parent"]
        assert parents["given"] == [] and len(parents["stored"]) == 1


@pytest.mark.asyncio
async def test_a_web_tracks_audio_comes_through_the_one_interface_and_says_it_waits_for_the_integration(
        tmp_path, monkeypatch):
    free_env(monkeypatch)
    install_fake_engine(tmp_path)
    monkeypatch.setattr(web_sources, "can_fetch", lambda entry: True)      # as if the door let it through
    async with SongRig(tmp_path) as rig:
        entry, _ = await rig.source()
        rig.library._entries[entry["id"]]["source"]["kind"] = "web"
        job = rig.propose("cover", entry, {"caption": "punk", "engine": "acestep", "lyrics_mode": "none",
                                           "variants": 1}, kind="web")
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.UNSUPPORTED
        assert done["state_reason"].startswith("web_source_pending_integration")


def test_stale_song_job_temp_files_are_swept_by_name_and_a_running_jobs_are_kept(tmp_path):
    """Zcode #188 finding 3: a crash mid-job left the decoded source behind."""
    for job in ("job_a", "job_b"):
        d = tmp_path / job
        d.mkdir()
        for name in (similar.SOURCE_PART, similar.MULTIPART_PART, similar.LEVELLED_PART, "take_v1.flac", "x.part"):
            (d / name).write_bytes(b"x")
    gone = similar.sweep_temp_parts(tmp_path, active={"job_b"})
    assert sorted(p.name for p in gone) == sorted(similar.TEMP_PARTS)
    assert sorted(p.name for p in (tmp_path / "job_a").iterdir()) == ["take_v1.flac", "x.part"]
    assert len(list((tmp_path / "job_b").iterdir())) == 5
    assert similar.sweep_temp_parts(tmp_path / "missing") == []


@pytest.mark.asyncio
async def test_the_runner_sweeps_stale_song_job_temp_files_when_it_starts(tmp_path, monkeypatch):
    free_env(monkeypatch)
    install_fake_engine(tmp_path)
    stale = tmp_path / "music" / "generated" / "job_crashed"
    rig = SongRig(tmp_path)
    stale = rig.runner.generated_root / "job_crashed"
    stale.mkdir(parents=True)
    (stale / similar.SOURCE_PART).write_bytes(b"\0" * 1024)
    async with rig:
        for _ in range(100):
            if not (stale / similar.SOURCE_PART).exists():
                break
            await __import__("asyncio").sleep(0.02)
        assert not (stale / similar.SOURCE_PART).exists()
