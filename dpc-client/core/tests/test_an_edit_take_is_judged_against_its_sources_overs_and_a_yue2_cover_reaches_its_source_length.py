"""Follow-up 21, from the first live GPU probe (ai-studio experiments/music-player/measurements/
2026-11-26-live-gpu-probe.md):

- B. The clipping and true-peak gates failed every take. An edit take copies its control bit for bit outside
  the zone, so the control's own overs come along; the gates now fail an edit only for overs it adds (more
  than 0.1 dB over the control's, or more clipped samples), and say "inherited from the source" otherwise.
  A cover keeps its own verdict and carries the source's figures beside it. Stage 1 still only flags.
- C. `qa_report.thresholds.length_same_tolerance_s` said 0.001 beside a cover gate that judged with 0.05 s.
- D. A YuE2 cover came out 1.04 s short: the end token arrived exactly at Ruach's floor, a second under the
  source. The floor is now the source's own frame count.
"""

import math
import shutil
import wave

import numpy as np
import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import qa
from dpc_client_core.music import similar
from dpc_client_core.music.runner import MusicJobRunner
from tests._acestep_helpers import fake_loudness, free_env, install_fake_engine
from tests.test_a_region_edit_runs_through_the_runner_into_sibling_takes_with_a_seam_report import (
    DONE, EditRig, fake_decoder)


def _record(clipped=4, peak_dbfs=0.12, tp=0.2, length=None):
    gates = {
        "length": length or qa.length_gate(160.0, 160.0, qa.LENGTH_SAME),
        "clipping": qa.clipping_gate({"peak_db": peak_dbfs, "peak_count": clipped})[0],
        "true_peak": qa.true_peak_gate(tp),
        "silence": qa.silence_gate({"whole": False, "leading_s": 0.0, "tail_s": 1.0, "longest_internal_s": 0.0,
                                    "runs": []}),
    }
    return qa.build_record(gates=gates, measurements={"duration_s": 160.0}, integrated_lufs=-10.9,
                           true_peak_dbtp=tp, clipped_samples=clipped, measured_by="test")


def _source(clipped=5, peak_dbfs=0.12, tp=0.2):
    return {"measured": True, "true_peak_dbtp": tp, "sample_peak_dbfs": peak_dbfs, "clipped_samples": clipped,
            "integrated_lufs": -10.9, "measured_by": "test"}


# -- B: the comparison ---------------------------------------------------------------------------


def test_an_edit_take_with_only_the_controls_overs_passes_and_says_they_are_inherited():
    """The probe's remove_region take: 4 clipped samples against the control's 5, +0.2 dBTP against +0.2."""
    before = _record()
    assert before["qa_report"]["verdict"] == js.QA_FAILED
    out = qa.compare_with_source(before, _source(), qa.EDIT)
    report = out["qa_report"]
    js.check_qa_report(report)
    assert report["verdict"] == js.QA_PASSED
    for name in ("clipping", "true_peak"):
        gate = report["gates"][name]
        assert gate["status"] == js.GATE_PASS and gate["inherited_from_source"] is True
        assert gate["detail"].startswith("advisory: inherited from the source")
    assert report["gates"]["clipping"]["source_clipped_samples"] == 5
    assert report["gates"]["true_peak"]["source_true_peak_dbtp"] == 0.2
    assert report["measurements"]["source"]["relation"] == "edit"
    assert out["qa"] == before["qa"]                               # mp.qa/1 keeps the take's own numbers
    assert before["qa_report"]["verdict"] == js.QA_FAILED          # the input is not changed in place


@pytest.mark.parametrize("take, source, failed", [
    (dict(tp=0.31), dict(tp=0.2), ["true_peak"]),                  # 0.11 dB over the control's true peak
    (dict(tp=0.3), dict(tp=0.2), []),                              # within 0.1 dB
    (dict(clipped=6), dict(clipped=5), ["clipping"]),              # one clipped sample more at the same peak
    (dict(peak_dbfs=0.25), dict(peak_dbfs=0.12), ["clipping"]),    # a higher peak
    (dict(), dict(clipped=0, peak_dbfs=-0.5, tp=-1.5), ["clipping", "true_peak"]),   # a clean control
])
def test_an_edit_take_that_adds_overs_still_fails(take, source, failed):
    report = qa.compare_with_source(_record(**take), _source(**source), qa.EDIT)["qa_report"]
    js.check_qa_report(report)
    got = [g for g in ("clipping", "true_peak") if report["gates"][g]["status"] == js.GATE_FAIL]
    assert got == failed
    for g in failed:
        assert report["gates"][g]["inherited_from_source"] is False
        assert "so the take" in report["gates"][g]["detail"]


def test_a_cover_keeps_its_verdict_and_carries_the_sources_figures():
    """Covers are a new rendering: the gate verdict stays the take's own (a decision left for Mike)."""
    before = _record(clipped=77, peak_dbfs=0.0, tp=0.3)
    report = qa.compare_with_source(before, _source(), qa.COVER)["qa_report"]
    js.check_qa_report(report)
    assert report["verdict"] == js.QA_FAILED
    assert report["gates"]["clipping"]["status"] == report["gates"]["true_peak"]["status"] == js.GATE_FAIL
    assert "inherited_from_source" not in report["gates"]["clipping"]
    assert report["gates"]["true_peak"]["source_true_peak_dbtp"] == 0.2
    assert "the source's true peak is +0.20 dBTP" in report["gates"]["true_peak"]["detail"]
    assert report["measurements"]["source"]["relation"] == "cover"


def test_with_no_source_measurement_the_gates_stay_as_measured_and_the_reason_is_kept():
    before = _record()
    report = qa.compare_with_source(before, {"measured": False, "reason": "ffmpeg is not on PATH"}, qa.EDIT)["qa_report"]
    assert report["gates"] == before["qa_report"]["gates"] and report["verdict"] == js.QA_FAILED
    assert report["measurements"]["source"] == {"measured": False, "reason": "ffmpeg is not on PATH",
                                                "relation": "edit"}
    with pytest.raises(ValueError):
        qa.compare_with_source(before, _source(), "same_style")


def _wav_with_overs(path, seconds=1.0, rate=48000, overs=7):
    n = int(seconds * rate)
    t = np.arange(n) / rate
    x = 0.5 * np.sin(2 * np.pi * 440 * t)
    x[1000:1000 + overs] = 1.0
    pcm = np.round(x * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_the_source_is_measured_with_the_same_ffmpeg_pass_as_a_take(tmp_path):
    peaks = qa.measure_source_peaks(_wav_with_overs(tmp_path / "control.wav"))
    assert peaks["measured"] is True and peaks["clipped_samples"] >= 7     # astats: a count at the peak
    assert peaks["sample_peak_dbfs"] is not None and peaks["sample_peak_dbfs"] > qa.CLIP_LEVEL_DB
    assert isinstance(peaks["true_peak_dbtp"], float) and isinstance(peaks["integrated_lufs"], float)


def test_a_source_that_cannot_be_read_is_a_named_error(tmp_path):
    with pytest.raises(qa.QaError):
        qa.measure_source_peaks(tmp_path / "missing.wav")


# -- B through the runner ------------------------------------------------------------------------


def _overs_meter(calls):
    def meter(flac, *, requested_s=None, length_mode="exact", duration_s=None, loudness=None, tool_version=None,
              edit=None, same_tolerance_s=None):
        calls.append(edit)
        rec = _record(length=qa.length_gate(requested_s, duration_s, length_mode))
        rec["qa_report"]["measurements"]["duration_s"] = duration_s
        return rec
    return meter


class SourceRig(EditRig):
    def __init__(self, home, source_peaks):
        super().__init__(home)
        self.source_calls = []
        self.meter_calls = []

        def source_meter(path):
            self.source_calls.append(path.name)
            if isinstance(source_peaks, Exception):
                raise source_peaks
            return source_peaks
        self.runner = MusicJobRunner(
            self.store, self.engine, self._library, dpc_home=home, converter=self.keep,
            on_job_change=self.events.append, card_poll_s=0.05, engine_poll_s=0.02, loudness_meter=fake_loudness,
            qa_meter=_overs_meter(self.meter_calls), source_meter=source_meter, control_decoder=fake_decoder,
            draw_seed=lambda: 4242)


@pytest.mark.asyncio
async def test_a_removal_with_the_controls_overs_passes_stage_one_through_the_runner(tmp_path, monkeypatch):
    free_env(monkeypatch, record=tmp_path / "posts")
    install_fake_engine(tmp_path)
    async with SourceRig(tmp_path, _source()) as rig:
        entry, _base = await rig.base()
        job = rig.propose(entry, "remove_region", {"t_start": 2.0, "t_end": 3.0})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")
        (take,) = rig.store.list_takes(job["id"])
        report = take["qa_report"]
        assert rig.source_calls == ["control.wav.part"]                    # the control, measured once
        assert report["verdict"] == js.QA_PASSED, report["gates"]
        assert report["gates"]["true_peak"]["inherited_from_source"] is True
        assert report["measurements"]["source"]["relation"] == "edit"
        assert take["qc"]["stage1"]["verdict"] == js.QA_PASSED


@pytest.mark.asyncio
async def test_a_control_that_cannot_be_measured_leaves_the_gates_as_measured(tmp_path, monkeypatch):
    free_env(monkeypatch, record=tmp_path / "posts")
    install_fake_engine(tmp_path)
    async with SourceRig(tmp_path, qa.QaError("ffmpeg is not on PATH")) as rig:
        entry, _base = await rig.base()
        job = rig.propose(entry, "remove_region", {"t_start": 2.0, "t_end": 3.0})
        done = await rig.wait_for(job["id"], DONE)
        assert done["state"] == js.REVIEW, done.get("state_reason")      # never a reason to fail the job
        (take,) = rig.store.list_takes(job["id"])
        report = take["qa_report"]
        assert report["verdict"] == js.QA_FAILED
        assert report["measurements"]["source"]["measured"] is False
        assert "ffmpeg is not on PATH" in report["measurements"]["source"]["reason"]


def test_a_runner_with_a_stand_in_take_meter_makes_no_comparison_unless_given_a_source_meter(tmp_path):
    from tests._music_jobs_helpers import store_at
    store = store_at(tmp_path / "dpc")
    plain = MusicJobRunner(store, object(), None, dpc_home=tmp_path / "dpc", qa_meter=lambda *a, **k: {})
    assert plain._source_measure is None
    real = MusicJobRunner(store, object(), None, dpc_home=tmp_path / "dpc")
    assert real._source_measure is not None


# -- C: the tolerance the gate used ----------------------------------------------------------------


@pytest.mark.parametrize("tolerance", [0.05, 2.0])
def test_the_report_names_the_same_length_tolerance_the_gate_used(tolerance):
    gate = qa.length_gate(121.319, 120.279, qa.LENGTH_SAME, same_tolerance_s=tolerance)
    report = _record(length=gate)["qa_report"]
    assert gate["tolerance_s"] == tolerance
    assert report["thresholds"]["length_same_tolerance_s"] == tolerance
    edit = _record(length=qa.length_gate(160.0, 160.0, qa.LENGTH_SAME))["qa_report"]
    assert edit["thresholds"]["length_same_tolerance_s"] == qa.LENGTH_SAME_TOLERANCE_S
    exact = _record(length=qa.length_gate(60.0, 59.6, qa.LENGTH_EXACT))["qa_report"]
    assert exact["thresholds"] == qa.THRESHOLDS


# -- D: a YuE2 cover may not end before its source ----------------------------------------------------


@pytest.mark.parametrize("source_s", [121.319, 6.0, 0.05, 159.999, 400.0])
def test_a_yue2_covers_floor_is_the_sources_own_length_and_stays_under_its_cap(source_s):
    body = similar.yue2_cover_length(source_s)
    floor = body["semantic_sampling"]["min_tokens"]
    assert floor == math.ceil(source_s * js.YUE2_FRAME_RATE)
    assert floor / js.YUE2_FRAME_RATE >= source_s                      # the end token cannot come before the source ends
    assert floor < js.yue2_semantic_budget(body)                      # and the cap is still above it
    assert body["duration"] == float(math.ceil(source_s) + 1)        # the cap is Ruach's, unchanged


def test_the_probes_cover_would_now_reach_its_source():
    """Probe: source 121.319 s, end token at step 3007 = floor((121.319 - 1) x 25), take 120.279 s."""
    body = similar.yue2_cover_length(121.319)
    assert body["semantic_sampling"] == {"min_tokens": 3033}
    assert 3033 / js.YUE2_FRAME_RATE == pytest.approx(121.32)
    # the context check counts the cap, not the floor: its budget is what it was
    assert js.yue2_semantic_budget(body) == (122 + 1) * js.YUE2_FRAME_RATE
