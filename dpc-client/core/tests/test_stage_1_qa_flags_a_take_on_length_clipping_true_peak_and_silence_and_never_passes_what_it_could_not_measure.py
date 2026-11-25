"""Stage-1 QA of a music take (`music/qa.py`): `{qa, qa_report}` from one ffmpeg pass.

A quiet tail of 1-5 s passes, seams never gate, a measurement that could not be made is
`not_measured` and never a pass, and a QA failure never fails the job. The WAVs are synthetic
(numpy and the standard library) and measured with the real ffmpeg; those tests skip without it.
"""

import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import qa
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.loudness import MEASURE_TIMEOUT_S, MEASURED_BY
from dpc_client_core.music.qa import QaError, measure_take_qa
from dpc_client_core.music.runner import ffmpeg_converter
from dpc_client_core.music.schemas import SchemaError
from tests._acestep_helpers import Rig, free_env, from_text, install_fake_engine
from tests._music_jobs_helpers import running_job, store_at

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
RATE = 44100
DONE = (js.REVIEW, js.FAILED, js.CANCELLED)


def tone(seconds, amplitude=0.25, freq=440.0):
    t = np.arange(int(seconds * RATE)) / RATE
    return amplitude * np.sin(2 * np.pi * freq * t)


def silence(seconds):
    return np.zeros(int(seconds * RATE))


def write_wav(path: Path, *parts) -> Path:
    """Mono 16-bit WAV of the parts joined; values beyond full scale clip in the cast."""
    pcm = np.clip(np.round(np.concatenate(parts) * 32768.0), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return path


def measure(tmp_path, *parts, requested=None, mode="exact", **kw):
    return measure_take_qa(write_wav(tmp_path / "take.wav", *parts), requested_s=requested, length_mode=mode, **kw)


def statuses(record):
    return {k: v["status"] for k, v in record["qa_report"]["gates"].items()}


def assert_valid(record):
    js.check_qa_report(record["qa_report"])
    if record["qa"] is not None:
        js.check_qa(record["qa"])


# -- the real ffmpeg on synthetic WAVs ----------------------------------------------------


@needs_ffmpeg
def test_a_clean_tone_of_the_requested_length_passes_every_gate_and_gives_a_valid_mp_qa_1(tmp_path):
    rec = measure(tmp_path, tone(6.0), requested=6.0)
    assert statuses(rec) == {"length": "pass", "clipping": "pass", "true_peak": "pass", "silence": "pass"}
    assert rec["qa_report"]["verdict"] == "passed"
    assert_valid(rec)
    q = rec["qa"]
    assert set(q) == set(js.QA_KEYS)
    assert q["clipped_samples"] == 0 and -20 < q["integrated_lufs"] < -10 and q["true_peak_dbtp"] < -10
    # a new take has no seam, no region to compare with and no judge: null, not a made-up number
    assert q["loudness_delta_lu"] is None and q["seam_start_jump_db"] is None and q["lyrics_wer"] is None
    m = rec["qa_report"]["measurements"]
    assert m["duration_s"] == pytest.approx(6.0, abs=0.02) and m["clipped_samples"] == 0
    assert m["loudness_source"] == "ebur128 in the same ffmpeg pass"


@needs_ffmpeg
def test_a_clipped_tone_fails_clipping_and_true_peak_with_a_count(tmp_path):
    rec = measure(tmp_path, tone(4.0, amplitude=3.0), requested=4.0)
    s = statuses(rec)
    assert s["clipping"] == "fail" and s["true_peak"] == "fail" and rec["qa_report"]["verdict"] == "failed"
    assert rec["qa"]["clipped_samples"] > 1000, rec["qa"]
    assert "sample(s) at the peak" in rec["qa_report"]["gates"]["clipping"]["detail"]
    assert_valid(rec)


@needs_ffmpeg
def test_a_peak_just_under_the_clip_level_is_not_clipping(tmp_path):
    rec = measure(tmp_path, tone(4.0, amplitude=0.98), requested=4.0)
    assert statuses(rec)["clipping"] == "pass" and rec["qa"]["clipped_samples"] == 0
    assert statuses(rec)["true_peak"] == "fail"  # 0.98 is about -0.18 dBFS, over the -1 dBTP ceiling


@needs_ffmpeg
def test_a_take_that_starts_with_four_seconds_of_silence_fails_silence(tmp_path):
    rec = measure(tmp_path, silence(4.0), tone(4.0), requested=8.0)
    gate = rec["qa_report"]["gates"]["silence"]
    assert gate["status"] == "fail" and gate["leading_s"] == pytest.approx(4.0, abs=0.1)
    assert "at the start" in gate["detail"]
    assert statuses(rec)["length"] == "pass" and statuses(rec)["clipping"] == "pass"
    assert rec["qa_report"]["verdict"] == "failed"


@needs_ffmpeg
def test_a_one_and_a_half_second_dropout_inside_the_take_fails_silence(tmp_path):
    rec = measure(tmp_path, tone(3.0), silence(1.5), tone(3.0), requested=7.5)
    gate = rec["qa_report"]["gates"]["silence"]
    assert gate["status"] == "fail" and gate["longest_internal_s"] == pytest.approx(1.5, abs=0.1)
    assert gate["tail_s"] == 0 and gate["leading_s"] == 0
    assert "dropout" in gate["detail"]


@needs_ffmpeg
def test_a_quiet_tail_of_three_seconds_is_normal_and_passes(tmp_path):
    rec = measure(tmp_path, tone(5.0), silence(3.0), requested=8.0)
    gate = rec["qa_report"]["gates"]["silence"]
    assert gate["status"] == "pass", gate
    assert gate["tail_s"] == pytest.approx(3.0, abs=0.1)
    assert rec["qa_report"]["verdict"] == "passed"
    assert rec["qa_report"]["measurements"]["tail_silence_s"] == pytest.approx(3.0, abs=0.1)


@needs_ffmpeg
def test_a_tail_longer_than_five_seconds_fails(tmp_path):
    rec = measure(tmp_path, tone(4.0), silence(6.0), requested=10.0)
    gate = rec["qa_report"]["gates"]["silence"]
    assert gate["status"] == "fail" and "at the end" in gate["detail"]
    assert "a quiet tail up to 5 s is normal" in gate["detail"]


@needs_ffmpeg
def test_a_take_much_shorter_than_asked_fails_length_when_the_length_is_exact(tmp_path):
    rec = measure(tmp_path, tone(6.0), requested=20.0, mode="exact")
    gate = rec["qa_report"]["gates"]["length"]
    assert gate["status"] == "fail" and gate["delta_s"] == pytest.approx(-14.0, abs=0.1)
    assert gate["tolerance_s"] == 2.0 and gate["mode"] == "exact"
    assert rec["qa_report"]["verdict"] == "failed"


@needs_ffmpeg
def test_a_file_that_is_silent_end_to_end_fails_and_has_no_mp_qa_1_to_fill(tmp_path):
    rec = measure(tmp_path, silence(6.0), requested=6.0)
    s = statuses(rec)
    assert s["silence"] == "fail" and rec["qa_report"]["gates"]["silence"]["whole_take"] is True
    assert s["true_peak"] == "not_measured"
    assert rec["qa"] is None   # no true peak: nothing is invented to make mp.qa/1 valid
    assert rec["qa_report"]["verdict"] == "failed"
    assert_valid(rec)


@needs_ffmpeg
def test_a_loudness_record_from_the_runner_is_used_and_ebur128_is_not_run_again(tmp_path):
    f = write_wav(tmp_path / "take.wav", tone(5.0))
    seen = []
    real = MediaTools.discover()

    def runner(cmd, *, timeout):
        seen.append(cmd[cmd.index("-af") + 1])
        return real.runner(cmd, timeout=timeout)
    loud = {"lufs_i": -15.5, "lra": 1.0, "true_peak_dbtp": -12.3, "measured_by": MEASURED_BY,
            "measured_at": "2026-11-25T08:00:00Z"}
    rec = measure_take_qa(f, requested_s=5.0, loudness=loud, duration_s=5.0,
                          tools=MediaTools(ffmpeg=real.ffmpeg, runner=runner))
    assert len(seen) == 1 and "ebur128" not in seen[0] and "astats" in seen[0] and "silencedetect" in seen[0]
    assert rec["qa"]["integrated_lufs"] == -15.5 and rec["qa"]["true_peak_dbtp"] == -12.3
    assert rec["qa_report"]["measurements"]["loudness_source"] == "the take's ebur128 loudness record"


# -- the length gate -----------------------------------------------------------------------


def test_an_exact_length_has_a_tolerance_of_the_larger_of_two_seconds_and_five_percent():
    assert qa.length_gate(120.0, 117.0, "exact")["status"] == "pass"
    assert qa.length_gate(120.0, 112.0, "exact")["status"] == "fail"
    assert qa.length_gate(20.0, 21.5, "exact")["status"] == "pass"
    assert qa.length_gate(20.0, 22.5, "exact")["status"] == "fail"


def test_a_cap_engine_may_end_the_song_early_but_not_over_the_cap_or_under_ten_seconds():
    # YuE2 ends the song at its end token: the request is a cap.
    assert qa.length_gate(180.0, 95.0, "cap")["status"] == "pass"
    assert "ends the song itself" in qa.length_gate(180.0, 95.0, "cap")["detail"]
    assert qa.length_gate(180.0, 181.5, "cap")["status"] == "pass"
    assert qa.length_gate(180.0, 183.0, "cap")["status"] == "fail"
    assert qa.length_gate(180.0, 6.0, "cap")["status"] == "fail"


def test_an_unknown_length_or_request_is_not_measured_never_a_pass():
    assert qa.length_gate(60.0, None, "exact")["status"] == "not_measured"
    assert qa.length_gate(None, 60.0, "exact")["status"] == "not_measured"
    assert qa.length_gate(0, 60.0, "cap")["status"] == "not_measured"


# -- logs, parsers and failures without ffmpeg ----------------------------------------------

STDERR = """Input #0, wav, from 'take.wav':
  Duration: 00:00:08.00, bitrate: 705 kb/s
[Parsed_silencedetect_2 @ 0000019e217cefc0] silence_start: 5
[Parsed_ebur128_0 @ 0000019e217cfd80] Summary:

  Integrated loudness:
    I:         -22.1 LUFS
    Threshold: -32.3 LUFS

  Loudness range:
    LRA:         9.8 LU

  True peak:
    Peak:      -18.1 dBFS
[Parsed_silencedetect_2 @ 0000019e217cefc0] silence_end: 8 | silence_duration: 3
[Parsed_astats_1 @ 0000019e217cfec0] Overall
[Parsed_astats_1 @ 0000019e217cfec0] Peak level dB: -18.063921
[Parsed_astats_1 @ 0000019e217cfec0] Peak count: 960.000000
"""
NO_ASTATS = "\n".join(line for line in STDERR.splitlines() if "astats" not in line)


class _Result:
    def __init__(self, returncode, stderr):
        self.returncode = returncode
        self.stderr = stderr.encode("utf-8")


def fake_tools(returncode=0, stderr=STDERR, seen=None):
    def runner(cmd, *, timeout):
        if seen is not None:
            seen.append((cmd, timeout))
        return _Result(returncode, stderr)
    return MediaTools(ffmpeg="ffmpeg", runner=runner)


def test_the_log_parsers_read_duration_astats_and_silence():
    assert qa.parse_duration(STDERR) == 8.0
    assert qa.parse_astats(STDERR) == {"peak_db": -18.063921, "peak_count": 960}
    assert qa.parse_silences(STDERR, 8.0) == [(5.0, 8.0)]
    assert qa.classify_silences([(5.0, 8.0)], 8.0) == {
        "whole": False, "leading_s": 0.0, "tail_s": 3.0, "longest_internal_s": 0.0, "runs": [[5.0, 8.0]]}
    # older ffmpeg: a run that never ended is closed by the file's length; unknown length drops it
    assert qa.parse_silences("silence_start: 6.5\n", 8.0) == [(6.5, 8.0)]
    assert qa.parse_silences("silence_start: 6.5\n", None) == []
    assert qa.parse_astats("no stats here") is None
    assert qa.parse_astats("] Overall\n Peak level dB: -inf\n Peak count: 0\n") == {"peak_db": None, "peak_count": 0}


def test_the_canned_log_gives_a_passing_record_with_a_three_second_tail():
    rec = measure_take_qa(Path(__file__), requested_s=8.0, tools=fake_tools())
    assert statuses(rec) == {"length": "pass", "clipping": "pass", "true_peak": "pass", "silence": "pass"}
    assert rec["qa"]["integrated_lufs"] == -22.1 and rec["qa"]["true_peak_dbtp"] == -18.1
    assert rec["qa_report"]["gates"]["silence"]["tail_s"] == 3.0
    assert_valid(rec)


def test_the_command_is_one_pass_with_the_loudness_timeout():
    seen = []
    measure_take_qa(Path(__file__), requested_s=8.0, tools=fake_tools(seen=seen))
    cmd, timeout = seen[0]
    assert len(seen) == 1 and cmd[0] == "ffmpeg" and "-nostdin" in cmd and cmd[-3:] == ["-f", "null", "-"]
    chain = cmd[cmd.index("-af") + 1]
    assert chain.startswith("ebur128=peak=true:framelog=verbose,astats=")
    assert chain.endswith("silencedetect=n=-60dB:d=0.5")
    assert timeout == MEASURE_TIMEOUT_S


def test_an_ffmpeg_without_the_astats_options_is_asked_again_with_the_plain_filters():
    calls = []

    def runner(cmd, *, timeout):
        calls.append(cmd[cmd.index("-af") + 1])
        if "measure_overall" in calls[-1]:
            return _Result(1, "Option 'measure_overall' not found")
        return _Result(0, STDERR)
    rec = measure_take_qa(Path(__file__), requested_s=8.0, tools=MediaTools(ffmpeg="ffmpeg", runner=runner))
    assert len(calls) == 2 and "measure_" not in calls[1] and ",astats," in calls[1]
    assert rec["qa_report"]["verdict"] == "passed"


def test_a_log_with_no_astats_block_leaves_clipping_not_measured_and_the_verdict_not_a_pass():
    rec = measure_take_qa(Path(__file__), requested_s=8.0, tools=fake_tools(stderr=NO_ASTATS))
    s = statuses(rec)
    assert s["clipping"] == "not_measured" and s["length"] == s["true_peak"] == s["silence"] == "pass"
    assert rec["qa_report"]["verdict"] == "not_measured"
    assert rec["qa"] is None    # clipped_samples is a required field with no number behind it
    assert_valid(rec)


def test_a_failed_gate_wins_over_an_unmeasured_one_in_the_verdict():
    rec = measure_take_qa(Path(__file__), requested_s=30.0, tools=fake_tools(stderr=NO_ASTATS))
    assert statuses(rec)["length"] == "fail" and statuses(rec)["clipping"] == "not_measured"
    assert rec["qa_report"]["verdict"] == "failed"


def test_no_ffmpeg_no_file_or_a_failing_ffmpeg_is_a_qa_error(tmp_path):
    with pytest.raises(QaError, match="ffmpeg is not on PATH"):
        measure_take_qa(Path(__file__), requested_s=1.0, tools=MediaTools(ffmpeg=None))
    with pytest.raises(QaError, match="not there"):
        measure_take_qa(tmp_path / "gone.flac", requested_s=1.0, tools=MediaTools(ffmpeg="ffmpeg"))
    with pytest.raises(QaError, match="rc 1"):
        measure_take_qa(Path(__file__), requested_s=1.0, tools=fake_tools(returncode=1, stderr="Invalid data"))
    with pytest.raises(QaError, match="length mode"):
        measure_take_qa(Path(__file__), requested_s=1.0, length_mode="soft", tools=fake_tools())


def test_seams_and_lyrics_wer_are_advisory_and_never_measured_for_a_new_take():
    rec = measure_take_qa(Path(__file__), requested_s=8.0, tools=fake_tools())
    adv = rec["qa_report"]["advisory"]
    assert set(adv) == {"seams", "lyrics_wer"}
    assert all(v["status"] == "not_measured" and v["reason"] for v in adv.values())
    assert "decision 29" in adv["seams"]["reason"]
    # a seam can never gate: an advisory entry that passed or failed is refused by the schema
    with pytest.raises(SchemaError):
        js.check_qa_report({**rec["qa_report"], "advisory": {**adv, "seams": {"status": "fail"}}})


# -- the schemas ----------------------------------------------------------------------------

GOOD_QA = {"integrated_lufs": -14.0, "true_peak_dbtp": -2.0, "loudness_delta_lu": None, "seam_start_jump_db": None,
           "seam_end_jump_db": None, "seam_click_score": None, "clipped_samples": 0, "lyrics_wer": None,
           "measured_by": "ffmpeg"}


def test_mp_qa_1_is_exact_every_field_required_and_no_extras():
    js.check_qa(GOOD_QA)
    js.check_qa({**GOOD_QA, "simulated": False, "seam_click_score": 3.2, "lyrics_wer": 0.4, "loudness_delta_lu": -0.3})
    for k in js.QA_KEYS:
        with pytest.raises(SchemaError):
            js.check_qa({a: b for a, b in GOOD_QA.items() if a != k})
    for bad in ({**GOOD_QA, "extra": 1}, {**GOOD_QA, "integrated_lufs": None},
                {**GOOD_QA, "true_peak_dbtp": float("inf")}, {**GOOD_QA, "clipped_samples": -1},
                {**GOOD_QA, "clipped_samples": 1.5}, {**GOOD_QA, "clipped_samples": True},
                {**GOOD_QA, "seam_start_jump_db": -0.1}, {**GOOD_QA, "lyrics_wer": 1.2},
                {**GOOD_QA, "measured_by": ""}, {**GOOD_QA, "simulated": "no"}):
        with pytest.raises(SchemaError):
            js.check_qa(bad)
    # a loudness-only record (loudness.py) is still not a valid mp.qa/1
    with pytest.raises(SchemaError):
        js.check_qa({"lufs_i": -9.8, "lra": 4.1, "true_peak_dbtp": -0.9, "measured_by": MEASURED_BY,
                     "measured_at": "2026-11-21T20:00:00Z"})


def test_a_report_must_agree_with_its_gates_and_declare_only_its_keys():
    rec = qa.not_measured_record("ffmpeg is not on PATH", requested_s=60.0)
    js.check_qa_report(rec["qa_report"])
    assert rec["qa"] is None and rec["qa_report"]["verdict"] == "not_measured"
    assert all(g["status"] == "not_measured" and "ffmpeg is not on PATH" in g["detail"]
               for g in rec["qa_report"]["gates"].values())
    for bad in ({**rec["qa_report"], "verdict": "passed"}, {**rec["qa_report"], "extra": 1},
                {**rec["qa_report"], "stage": 2}, {k: v for k, v in rec["qa_report"].items() if k != "gates"},
                {**rec["qa_report"], "gates": {"length": rec["qa_report"]["gates"]["length"]}}):
        with pytest.raises(SchemaError):
            js.check_qa_report(bad)
    ok = {n: {"status": "pass"} for n in js.QA_GATES}
    assert js.qa_verdict_of(ok) == "passed"
    assert js.qa_verdict_of({**ok, "silence": {"status": "fail"}}) == "failed"
    assert js.qa_verdict_of({**ok, "silence": {"status": "not_measured"}}) == "not_measured"


def test_the_summary_says_passed_failed_gate_names_or_not_measured():
    ok = measure_take_qa(Path(__file__), requested_s=8.0, tools=fake_tools())
    assert qa.qa_summary(ok)["text"] == "QA passed"
    s = qa.qa_summary(measure_take_qa(Path(__file__), requested_s=30.0, tools=fake_tools()))
    assert s["verdict"] == "failed" and s["failed"] == ["length"] and s["text"] == "QA failed: length"
    none = qa.qa_summary({"qa": None, "qa_report": None})
    assert none["verdict"] == "not_measured" and none["text"].startswith("QA not measured")


# -- the take, the store and the runner ------------------------------------------------------


def test_the_store_keeps_qa_and_its_report_on_a_new_take_and_refuses_an_invalid_one(tmp_path):
    from dpc_client_core.music.jobs import MusicJobError
    home = tmp_path / "dpc"
    store = store_at(home)
    job = running_job(store)
    rec = measure_take_qa(Path(__file__), requested_s=8.0, tools=fake_tools())
    take = store.add_take(
        job["id"], variant=1, file_uri=f"file:///music/generated/{job['id']}/v1.flac", content_hash="md5:" + "0" * 32,
        title="t", seed=42, synth_batch_size=1, synth_batch_index=0, payload="lm.request.json", qa=rec["qa"],
        qa_report=rec["qa_report"])
    assert take["qa"] == rec["qa"] and take["qa_report"] == rec["qa_report"]
    assert store_at(home).get_take(take["id"])["qa_report"]["verdict"] == "passed"   # loaded back through validate_take
    with pytest.raises(MusicJobError) as err:
        store.add_take(job["id"], variant=2, file_uri="file:///x/v2.flac", content_hash=None, title="t", seed=42,
                       synth_batch_size=1, synth_batch_index=0, payload="lm.request.json",
                       qa={"integrated_lufs": -14.0})
    assert err.value.code == "invalid_params"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "dpc"
    install_fake_engine(h)
    return h


@pytest.mark.asyncio
async def test_each_take_a_job_makes_carries_its_qa_and_the_job_reaches_review(home, monkeypatch):
    free_env(monkeypatch)
    seen = []

    def meter(flac, **kw):
        seen.append(kw)
        assert flac.name.endswith(".flac.part") and flac.is_file()   # the staged file, before it is final
        return qa.build_record(
            gates={n: qa._gate("pass", "ok") for n in js.QA_GATES}, measurements={"duration_s": kw["duration_s"]},
            integrated_lufs=kw["loudness"]["lufs_i"], true_peak_dbtp=kw["loudness"]["true_peak_dbtp"],
            clipped_samples=0, measured_by="test meter")
    async with Rig(home, qa_meter=meter) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=2, duration_s=30))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        takes = rig.store.list_takes(job["id"])
    assert len(seen) == 2
    assert seen[0]["requested_s"] == 30.0 and seen[0]["length_mode"] == "exact" and seen[0]["loudness"]["lufs_i"] == -12.5
    for t in takes:
        js.check_qa(t["qa"])
        js.check_qa_report(t["qa_report"])
        assert t["qa_report"]["verdict"] == "passed" and t["qa"]["measured_by"] == "test meter"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [QaError("ffmpeg is not on PATH"), RuntimeError("boom"),
                                     SchemaError("a meter that returns a broken record")])
async def test_a_qa_error_leaves_the_take_not_measured_and_the_job_still_done(home, monkeypatch, failure):
    free_env(monkeypatch)

    def broken(flac, **kw):
        raise failure
    async with Rig(home, qa_meter=broken) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        take = rig.store.list_takes(job["id"])[0]
    assert take["qa"] is None
    report = take["qa_report"]
    js.check_qa_report(report)
    assert report["verdict"] == "not_measured" and report["note"]
    assert {g["status"] for g in report["gates"].values()} == {"not_measured"}
    assert take["decision"] == "pending"   # QA flags; it never rejects


@pytest.mark.asyncio
async def test_a_record_from_the_meter_that_breaks_the_schema_is_not_stored(home, monkeypatch):
    free_env(monkeypatch)

    def liar(flac, **kw):
        rec = qa.not_measured_record("x")
        rec["qa_report"]["verdict"] = "passed"      # does not follow from the gates
        return rec
    async with Rig(home, qa_meter=liar) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        take = rig.store.list_takes(job["id"])[0]
    assert job["state"] == js.REVIEW and take["qa_report"]["verdict"] == "not_measured"


@pytest.mark.asyncio
async def test_a_yue2_take_is_judged_against_its_duration_as_a_cap(home):
    seen = {}

    def meter(path, **kw):
        seen.update(kw)
        return qa.not_measured_record("t")
    async with Rig(home, qa_meter=meter) as rig:
        await rig.runner._measure_qa(Path("x.flac.part"), "job_" + "0" * 16, 1, {"duration_s": 180},
                                     {"engine": "yue2.cpp"}, 95.0, None)
        assert seen["length_mode"] == "cap" and seen["requested_s"] == 180.0 and seen["duration_s"] == 95.0
        await rig.runner._measure_qa(Path("x.flac.part"), "job_" + "0" * 16, 1, {"duration_s": 60},
                                     {"engine": "acestep.cpp"}, 60.0, None)
        assert seen["length_mode"] == "exact" and seen["requested_s"] == 60.0


@pytest.mark.asyncio
@needs_ffmpeg
async def test_the_real_ffmpeg_flags_the_fake_engines_fifty_milliseconds_as_too_short(home, monkeypatch):
    from dpc_client_core.music.loudness import ffmpeg_loudness_meter
    from dpc_client_core.music.qa import ffmpeg_qa_meter
    free_env(monkeypatch)
    async with Rig(home, converter=ffmpeg_converter(), loudness_meter=ffmpeg_loudness_meter(),
                   qa_meter=ffmpeg_qa_meter()) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1, duration_s=30))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        take = rig.store.list_takes(job["id"])[0]
    report = take["qa_report"]
    js.check_qa_report(report)
    assert report["gates"]["length"]["status"] == "fail" and report["verdict"] == "failed"
    assert report["gates"]["length"]["requested_s"] == 30.0
    assert take["decision"] == "pending"


# -- follow-up 14 (Zcode #176) -----------------------------------------------------------------

def test_a_loudness_record_without_a_true_peak_is_measured_again_so_true_peak_is_judged():
    seen = []
    loud = {"lufs_i": -15.5, "lra": 1.0, "true_peak_dbtp": None}
    rec = measure_take_qa(Path(__file__), requested_s=8.0, loudness=loud, tools=fake_tools(seen=seen))
    (cmd, _timeout), = seen
    assert "ebur128" in cmd[cmd.index("-af") + 1]
    assert statuses(rec)["true_peak"] == "pass"
    assert rec["qa"]["true_peak_dbtp"] == -18.1
    assert rec["qa_report"]["measurements"]["loudness_source"] == "ebur128 in the same ffmpeg pass"


def test_a_loudness_record_with_both_numbers_is_still_not_measured_again():
    seen = []
    loud = {"lufs_i": -15.5, "lra": 1.0, "true_peak_dbtp": -12.3}
    rec = measure_take_qa(Path(__file__), requested_s=8.0, loudness=loud, tools=fake_tools(seen=seen))
    (cmd, _timeout), = seen
    assert "ebur128" not in cmd[cmd.index("-af") + 1]
    assert rec["qa"]["true_peak_dbtp"] == -12.3


def test_a_short_cap_has_a_floor_of_half_the_cap_not_ten_seconds():
    # A 6 s YuE2 request: a 5 s take is a song that ended early, not a failure.
    assert qa.length_gate(6.0, 5.0, "cap")["status"] == "pass"
    assert qa.length_gate(6.0, 3.5, "cap")["status"] == "pass"
    short = qa.length_gate(6.0, 2.5, "cap")
    assert short["status"] == "fail" and short["floor_s"] == 3.0
    # A long cap keeps the 10 s floor.
    assert qa.length_gate(180.0, 9.0, "cap")["status"] == "fail"
    assert qa.length_gate(180.0, 11.0, "cap")["status"] == "pass"
    assert qa.cap_floor_s(30.0) == 10.0 and qa.cap_floor_s(12.0) == 6.0
    assert "50% of the cap" in qa.THRESHOLDS["length_cap_min"]


def test_a_take_under_the_cap_floor_is_a_valid_record_that_names_the_floor():
    rec = measure_take_qa(Path(__file__), requested_s=30.0, length_mode="cap", tools=fake_tools())
    assert statuses(rec)["length"] == "fail"
    assert rec["qa_report"]["gates"]["length"]["floor_s"] == 10.0
    assert_valid(rec)
    short_cap = measure_take_qa(Path(__file__), requested_s=12.0, length_mode="cap", tools=fake_tools())
    assert statuses(short_cap)["length"] == "pass", "8 s is over half of a 12 s cap"
    assert_valid(short_cap)
