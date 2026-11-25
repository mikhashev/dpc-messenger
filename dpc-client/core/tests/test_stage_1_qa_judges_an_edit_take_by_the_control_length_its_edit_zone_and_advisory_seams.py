"""Stage-1 QA of a region-edit take (`music/qa.py` with `edit=`; SPEC §8 "What gates a take", decisions 23,
27 and 29): the length must be the control's (`same`, `length_mismatch` otherwise), silence is judged
outside the edit zone, a removal's zone is meant to be silent, a repaint's zone with a long silence is a
hole, and the seam numbers fill `mp.qa/1` and an advisory entry that never passes or fails. The
ffmpeg-backed cases skip without ffmpeg.
"""

import math
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import qa
from dpc_client_core.music import region_edit as re_

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
RATE = 48000


def write_wav(path: Path, samples) -> Path:
    pcm = np.clip(np.round(np.asarray(samples) * 32768.0), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return path


def tone(seconds, freq=440.0, amp=0.25):
    t = np.arange(int(seconds * RATE)) / RATE
    return amp * np.sin(2 * np.pi * freq * t)


def test_an_edit_take_must_be_the_controls_length_to_the_millisecond():
    ok = qa.length_gate(183.250021, 183.25, qa.LENGTH_SAME)
    assert ok["status"] == "pass" and ok["mode"] == "same" and "decision 23" in ok["detail"]
    off = qa.length_gate(183.30, 183.25, qa.LENGTH_SAME)
    assert off["status"] == "fail" and off["detail"].startswith("length_mismatch")
    assert off["delta_s"] == pytest.approx(-0.05)   # received 183.25 against the control's 183.30
    assert qa.THRESHOLDS["length_same_tolerance_s"] == qa.LENGTH_SAME_TOLERANCE_S


def test_silence_is_cut_at_the_edit_zone_and_the_longest_stretch_inside_is_reported():
    runs = [(1.0, 1.4), (4.5, 6.0), (6.2, 9.5), (20.0, 21.0)]
    outside, inside = qa.split_runs_by_zone(runs, (5.0, 9.0))
    assert outside == [(1.0, 1.4), (4.5, 5.0), (9.0, 9.5), (20.0, 21.0)]
    assert inside == pytest.approx(2.8)
    assert qa.split_runs_by_zone(runs, None) == (runs, 0.0)


def test_a_removal_expects_its_zone_silent_and_a_repaint_with_silence_there_has_a_hole():
    found = qa.classify_silences([], 60.0)
    removed = qa.silence_gate(found, zone_silence_s=4.0, zone_expects_silence=True)
    assert removed["status"] == "pass" and "removed region" in removed["detail"]
    assert removed["edit_zone_silence_s"] == 4.0 and removed["edit_zone_expects_silence"] is True
    hole = qa.silence_gate(found, zone_silence_s=1.5, zone_expects_silence=False)
    assert hole["status"] == "fail" and "a hole" in hole["detail"]
    fine = qa.silence_gate(found, zone_silence_s=0.6, zone_expects_silence=False)
    assert fine["status"] == "pass"


def test_seam_numbers_fill_mp_qa_1_never_inf_and_the_advisory_entry_cannot_pass_or_fail():
    gates = {g: qa._gate(js.GATE_PASS, "ok") for g in js.QA_GATES}
    rec = qa.build_record(gates=gates, measurements={}, integrated_lufs=-14.0, true_peak_dbtp=-1.5, clipped_samples=0,
                          measured_by="test", seam_fields={"seam_start_jump_db": 0.8, "seam_end_jump_db": math.inf,
                                                           "seam_click_score": float("nan")},
                          seams_advisory={"status": "pass", "reason": "a seam that tried to gate"})
    q = rec["qa"]
    assert q["seam_start_jump_db"] == 0.8 and q["seam_end_jump_db"] is None and q["seam_click_score"] is None
    js.check_qa(q)
    assert rec["qa_report"]["advisory"]["seams"]["status"] == "not_measured"   # pass/fail is not an advisory status
    js.check_qa_report(rec["qa_report"])
    assert rec["qa_report"]["verdict"] == "passed"


@needs_ffmpeg
def test_a_removed_region_is_silent_on_purpose_and_its_take_passes_with_null_seam_fields(tmp_path):
    x = tone(10.0)
    control = re_.Pcm(np.stack([x, x], axis=1).astype(np.float32), RATE)
    result = re_.remove(control, (4.0, 7.0), 75.0)
    take = np.concatenate(result.pieces)[:, 0]
    path = write_wav(tmp_path / "take.wav", take)
    seams = re_.seams_entry("remove_region", result, RATE, crossfade_ms=75.0)
    rec = qa.measure_take_qa(path, requested_s=10.0, length_mode=qa.LENGTH_SAME,
                             edit={"action": "remove_region", "zone": (4.0, 7.0), "expects_silence": True,
                                   "seams": seams})
    gates = rec["qa_report"]["gates"]
    assert gates["silence"]["status"] == "pass" and gates["silence"]["edit_zone_silence_s"] > 2.5
    assert gates["length"]["status"] == "pass"
    assert rec["qa"]["seam_start_jump_db"] is None and rec["qa"]["seam_click_score"] is None
    assert rec["qa_report"]["advisory"]["seams"]["kind"] == "removed_region"
    assert rec["qa_report"]["measurements"]["edit_zone_s"] == [4.0, 7.0]
    js.check_qa_report(rec["qa_report"])
    # without the zone the same file fails the silence gate: the 3 s dropout is what the zone explains
    plain = qa.measure_take_qa(path, requested_s=10.0, length_mode=qa.LENGTH_SAME)
    assert plain["qa_report"]["gates"]["silence"]["status"] == "fail"


@needs_ffmpeg
def test_a_repaint_that_left_silence_where_the_edit_should_be_fails_as_a_hole_and_keeps_its_seam_numbers(tmp_path):
    x = np.concatenate([tone(4.0), np.zeros(2 * RATE), tone(4.0)])
    path = write_wav(tmp_path / "take.wav", x)
    seams = {"entry": {"status": "measured", "reason": "advisory"},
             "qa_fields": {"seam_start_jump_db": 0.4, "seam_end_jump_db": 0.6, "seam_click_score": 5.1}}
    rec = qa.measure_take_qa(path, requested_s=10.0, length_mode=qa.LENGTH_SAME,
                             edit={"action": "regenerate_region", "zone": (3.5, 6.5), "expects_silence": False,
                                   "seams": seams})
    assert rec["qa_report"]["gates"]["silence"]["status"] == "fail"
    assert "a hole" in rec["qa_report"]["gates"]["silence"]["detail"]
    assert (rec["qa"]["seam_start_jump_db"], rec["qa"]["seam_end_jump_db"], rec["qa"]["seam_click_score"]) == (0.4, 0.6, 5.1)
    assert rec["qa_report"]["verdict"] == "failed"   # a flag; QA never rejects the take by itself
