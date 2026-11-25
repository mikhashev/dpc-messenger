"""Zcode #211 on follow-up 21 (follow-up 22).

- 1. `generate_from_text` on yue2 (the default engine of a text job) had no context ceiling at either door:
  about 60 KB of text passed the door and failed inside the engine with no named refusal. The door and the run
  now count the exact text sent, the frames the duration cap lets run and the end token against the 24576
  positions, as a song job does, and refuse `context_exceeded` on `lyrics`.
- 2. `qc._check_schema_answer` documented the `confidence` and `t_start` bounds but left them to the later parse.
- 3. "The source is measured once per job" did not survive a re-prepare: a resumed job measured the control again.
- 4. `wait_hashed` did not see a `rescan_all` (or a poll pass) that had started before the call.
"""

import asyncio
import json

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import qc
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.jobs import MusicJobError
from dpc_client_core.music.library import MusicLibrary
from tests._yue2_helpers import DualRig, install_both, yue2_env, yue2_params


def _text_for(tokens):
    """A text whose sent size takes about `tokens` prompt tokens."""
    return "a" * int((tokens - js.YUE2_PROMPT_FIXED_TOKENS) / js.YUE2_TEXT_TOKENS_PER_BYTE)


# -- 1: the text job's context ----------------------------------------------------------------------


def test_a_yue2_text_job_whose_text_and_cap_overflow_the_context_is_refused_at_the_door():
    fits = js.YUE2_CONTEXT - 1 - js.yue2_semantic_budget({"duration": 360})
    too_long = _text_for(fits) + "a" * 200
    with pytest.raises(js.SongParamsError) as err:
        js.normalize_params_from_text({"lyrics": too_long, "style": "x", "duration_s": 360})
    assert err.value.code == "context_exceeded" and err.value.field == "lyrics"
    assert "360 s cap" in err.value.message and "acestep" in err.value.message
    # the default cap, 60 KB of text: the common path that used to reach the engine
    with pytest.raises(js.SongParamsError):
        js.normalize_params_from_text({"lyrics": "a" * 60_000, "style": "x"})
    # a shorter cap or a shorter text leaves the room
    assert js.normalize_params_from_text({"lyrics": too_long, "style": "x", "duration_s": 300})["duration_s"] == 300
    assert js.normalize_params_from_text({"lyrics": too_long[:-400], "style": "x", "duration_s": 360})
    # acestep has no such shared context
    assert js.normalize_params_from_text({"lyrics": "a" * 60_000, "style": "x", "engine": "acestep"})["engine"] == "acestep"


def test_the_door_counts_the_style_too_and_the_text_as_sent():
    fits = js.YUE2_CONTEXT - 1 - js.yue2_semantic_budget({"duration": 180})
    lyrics = _text_for(fits)[:-100]
    assert js.normalize_params_from_text({"lyrics": lyrics, "style": "x"})        # the lyrics alone fit
    with pytest.raises(js.SongParamsError):
        js.normalize_params_from_text({"lyrics": lyrics, "style": "s" * 300})     # the style tips it over
    sent = js.song_text_sent("s" * 300, lyrics)
    assert js.yue2_text_overflow("s" * 300, lyrics, 180) is not None
    assert js.yue2_song_context(len(sent.encode("utf-8")), 180)["left"] < 0


def test_the_store_refuses_it_with_the_named_code(tmp_path):
    from dpc_client_core.music.capabilities import MusicCapabilities
    from dpc_client_core.music.jobs import MusicJobStore
    from tests._music_jobs_helpers import installed_probe
    store = MusicJobStore(tmp_path, capabilities=MusicCapabilities(installed_probe))
    store.open()
    with pytest.raises(MusicJobError) as err:
        store.create_job("generate_from_text", {"lyrics": "a" * 60_000, "style": "x"})
    assert err.value.code == "context_exceeded" and err.value.field == "lyrics"


@pytest.mark.asyncio
async def test_the_run_checks_the_text_again_before_any_synth_body_is_written(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home, start_runner=False) as rig:
        job = rig.store.create_job("generate_from_text", yue2_params())
        grown = dict(job, params=dict(job["params"], lyrics="a" * 60_000))   # as if it changed after the door
        with pytest.raises(MusicJobError) as err:
            await rig.runner._yue2_payloads(grown)
        assert err.value.code == "context_exceeded" and err.value.message.startswith("context_exceeded:")
        assert rig.store.get_job(job["id"])["payloads"] == []
        # the same job as it was proposed writes its bodies
        await rig.runner._yue2_payloads(job)
        assert len(rig.store.get_job(job["id"])["payloads"]) == 2


# -- 2: the judge's number bounds sit where they are documented ---------------------------------------------


def _answer(**over):
    good = {"scores": {k: 7 for k in js.QC_SCORE_KEYS},
            "issues": [{"t_start": 1.0, "t_end": 2.0, "kind": "artifact", "note": "n"}],
            "reasons": "r", "confidence": 0.5}
    good.update(over)
    return good


def test_the_schema_check_itself_holds_confidence_and_t_start_to_their_bounds():
    qc._check_schema_answer(_answer())
    for confidence in (1.5, -0.1, "high", None, True):
        with pytest.raises(qc.JudgeParseError, match="confidence"):
            qc._check_schema_answer(_answer(confidence=confidence))
    for t_start in (-1.0, "1", None, True):
        with pytest.raises(qc.JudgeParseError, match="t_start"):
            qc._check_schema_answer(_answer(issues=[{"t_start": t_start, "t_end": 2.0, "kind": "artifact", "note": "n"}]))
    with pytest.raises(qc.JudgeParseError, match="t_end"):
        qc._check_schema_answer(_answer(issues=[{"t_start": 0.0, "t_end": -2.0, "kind": "artifact", "note": "n"}]))
    with pytest.raises(qc.JudgeParseError, match="lacks"):
        qc._check_schema_answer(_answer(issues=[{"t_start": 0.0, "kind": "artifact", "note": "n"}]))


def test_a_constrained_answer_with_a_bad_bound_is_refused_by_the_schema_check_before_the_parse():
    bad = json.dumps(_answer(confidence=2))
    with pytest.raises(qc.JudgeParseError, match="confidence"):
        qc.parse_judge_answer(bad, None, constrained=True)
    assert qc.parse_judge_answer(json.dumps(_answer()), None, constrained=True)["confidence"] == 0.5


# -- 3: the source is measured once per job, also across a re-prepare ----------------------------------------


class _Meter:
    def __init__(self):
        self.calls = []

    def __call__(self, path):
        self.calls.append(path.name)
        return {"measured": True, "true_peak_dbtp": 0.2, "sample_peak_dbfs": 0.1, "clipped_samples": 5,
                "integrated_lufs": -10.9, "measured_by": "test"}


@pytest.mark.asyncio
async def test_a_resumed_job_reuses_the_kept_source_measurement_and_a_changed_hash_measures_again(tmp_path, monkeypatch):
    from tests._acestep_helpers import fake_loudness, free_env, install_fake_engine
    from tests.test_a_region_edit_runs_through_the_runner_into_sibling_takes_with_a_seam_report import (
        EditRig, fake_decoder)
    from dpc_client_core.music.runner import MusicJobRunner, SOURCE_PEAKS_PAYLOAD
    free_env(monkeypatch, record=tmp_path / "posts")
    install_fake_engine(tmp_path)
    meter = _Meter()
    rig = EditRig(tmp_path)
    rig.runner = MusicJobRunner(
        rig.store, rig.engine, rig._library, dpc_home=tmp_path, converter=rig.keep, on_job_change=rig.events.append,
        loudness_meter=fake_loudness, qa_meter=lambda *a, **k: {}, source_meter=meter, control_decoder=fake_decoder,
        draw_seed=lambda: 4242)
    await rig.library.open()
    try:
        entry, _base = await rig.base()
        job = rig.propose(entry, "remove_region", {"t_start": 2.0, "t_end": 3.0})   # the runner is not started
        out_dir, control = tmp_path / "gen", tmp_path / "gen" / "control.wav.part"
        first = await rig.runner._edit_prepare(rig.store.get_job(job["id"]), out_dir, control)
        assert meter.calls == ["control.wav.part"]
        kept = rig.store.get_job(job["id"])
        assert SOURCE_PEAKS_PAYLOAD in [p["name"] for p in kept["payloads"]]
        # the job is prepared again (a restart with the payload written): no second pass over the control
        again = await rig.runner._edit_prepare(kept, out_dir, control)
        assert meter.calls == ["control.wav.part"]
        assert again["source_peaks"] == first["source_peaks"]
        # audio of another hash is measured, and the kept record is not overwritten
        other = await rig.runner._measure_source(control, job["id"], "the control", "f" * 64)
        assert other["measured"] is True and len(meter.calls) == 2
        assert json.loads(rig.store.read_payload(job["id"], SOURCE_PEAKS_PAYLOAD))["sha256"] != "f" * 64
    finally:
        await rig.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_a_failed_measurement_is_not_kept(tmp_path, monkeypatch):
    from tests._acestep_helpers import free_env, install_fake_engine
    from tests.test_a_region_edit_runs_through_the_runner_into_sibling_takes_with_a_seam_report import EditRig
    from dpc_client_core.music.runner import SOURCE_PEAKS_PAYLOAD
    free_env(monkeypatch, record=tmp_path / "posts")
    install_fake_engine(tmp_path)
    rig = EditRig(tmp_path)
    calls = []

    def broken(path):
        calls.append(1)
        raise OSError("no ffmpeg")
    rig.runner._source_measure = broken
    await rig.library.open()
    try:
        entry, _base = await rig.base()
        job = rig.propose(entry, "remove_region", {"t_start": 2.0, "t_end": 3.0})
        for _ in range(2):
            said = await rig.runner._measure_source(tmp_path / "x.wav", job["id"], "the control", "a" * 64)
            assert said["measured"] is False
        assert len(calls) == 2
        assert SOURCE_PEAKS_PAYLOAD not in [p["name"] for p in rig.store.get_job(job["id"])["payloads"]]
    finally:
        await rig.__aexit__(None, None, None)


# -- 4: wait_hashed sees every scan in flight ---------------------------------------------------------------------


def _library(home):
    return MusicLibrary(home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)


@pytest.mark.asyncio
async def test_wait_hashed_waits_for_a_rescan_all_started_before_the_call(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a" * 64)
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        await lib.add_folder(str(music), scan=False)
        await lib.wait_hashed()
        assert lib.list_entries() == []
        gate = asyncio.Event()
        real = lib._rescan_folder_locked

        async def slow(folder_id):
            await gate.wait()
            return await real(folder_id)

        monkeypatch.setattr(lib, "_rescan_folder_locked", slow)
        scan = asyncio.ensure_future(lib.rescan_all())
        await asyncio.sleep(0.05)                       # the rescan is under way, nothing walked yet
        with pytest.raises(asyncio.TimeoutError):
            await lib.wait_hashed(timeout=0.05)
        gate.set()
        await lib.wait_hashed()
        assert scan.done()
        entries = lib.list_entries()
        assert [e["source"]["name"] for e in entries] == ["a.mp3"] and entries[0]["hash_state"] == "done"
        assert not lib._scans_running
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_wait_hashed_waits_for_a_poll_pass_and_a_cancelled_scan_does_not_hold_it(tmp_path, monkeypatch):
    music = tmp_path / "Music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"a" * 64)
    lib = _library(tmp_path / "dpc")
    await lib.open()
    try:
        await lib.add_folder(str(music), scan=False)
        gate = asyncio.Event()
        real = lib._rescan_folder_locked

        async def slow(folder_id):
            await gate.wait()
            return await real(folder_id)

        monkeypatch.setattr(lib, "_rescan_folder_locked", slow)
        poll = asyncio.ensure_future(lib._poll_pass())
        await asyncio.sleep(0.05)
        with pytest.raises(asyncio.TimeoutError):
            await lib.wait_hashed(timeout=0.05)
        poll.cancel()
        with pytest.raises(asyncio.CancelledError):
            await poll
        assert not lib._scans_running
        await lib.wait_hashed(timeout=1.0)
    finally:
        await lib.stop()
