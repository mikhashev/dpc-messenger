"""Region edits, the pure part (`music/region_edit.py`; SPEC §4 "Length of a region take", §6 "Default
seam", §8 "Seam QA by edit type", decisions 23 and 26-29): the params and the three zones, the engine's
25 Hz window, the splice of a repaint (Forge's option A: the control outside the splice bounds, a
crossfade shaped by coherence at each edge, exactly the control's length), the removal (silence, length
kept) and its cut-with-shift exception, the lyrics re-timing, and the seam numbers, which never carry
inf (a zero denominator is null with a reason).
"""

import json
import math

import numpy as np
import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import region_edit as re_

RATE = 48000


def tone(seconds, freq, amp=0.3, phase=0.0):
    t = np.arange(int(seconds * RATE)) / RATE
    x = (amp * np.sin(2 * math.pi * freq * t + phase)).astype(np.float32)
    return np.stack([x, x], axis=1)


def pcm(data):
    return re_.Pcm(np.asarray(data, dtype=np.float32), RATE)


def joined(result):
    return np.concatenate([p for p in result.pieces if p.shape[0]], axis=0)


# -- params and zones -------------------------------------------------------------------------------

def test_a_regenerate_has_two_takes_the_repaint_engine_and_the_intent_zone_as_its_window():
    p = re_.normalize_params_edit("regenerate_region", {}, {"t_start": 10, "t_end": 14}, duration_s=120,
                                  content_hash="md5:ab")
    assert p["takes"] == 2 == re_.TAKES_PER_JOB and p["engine"] == re_.ENGINE_REPAINT
    assert p["allowed_zone"] is None and p["splice_bounds"] is None   # null = the intent zone (SPEC §4)
    assert p["crossfade_ms"] == re_.CROSSFADE_MS_DEFAULT and p["seed"] is None and p["cut_with_shift"] is False
    assert (p["source_content_hash"], p["source_duration_s"]) == ("md5:ab", 120.0)
    z = re_.zones_of({"t_start": 10, "t_end": 14}, p)
    assert z == {"intent": (10.0, 14.0), "allowed": (10.0, 14.0), "splice": (10.0, 14.0)}


def test_fix_seam_widens_a_short_region_to_a_window_around_the_seam():
    p = re_.normalize_params_edit("fix_seam", {}, {"t_start": 30.0, "t_end": 30.4}, duration_s=60)
    assert p["allowed_zone"] == {"t_start": 29.2, "t_end": 31.2}
    assert re_.zones_of({"t_start": 30.0, "t_end": 30.4}, p)["splice"] == (29.2, 31.2)
    # at the start of the track the window is clamped, never negative
    edge = re_.normalize_params_edit("fix_seam", {}, {"t_start": 0.1, "t_end": 0.3}, duration_s=60)
    assert edge["allowed_zone"]["t_start"] == 0.0


def test_a_removal_makes_one_take_and_takes_no_model_params():
    p = re_.normalize_params_edit("remove_region", {}, {"t_start": 5, "t_end": 9}, duration_s=60)
    assert p["takes"] == 1 and p["engine"] == re_.ENGINE_REMOVE
    for k, v in (("seed", 3), ("caption", "rock"), ("allowed_zone", {"t_start": 4, "t_end": 10})):
        with pytest.raises(re_.EditError) as exc:
            re_.normalize_params_edit("remove_region", {k: v}, {"t_start": 5, "t_end": 9}, duration_s=60)
        assert exc.value.field == k


@pytest.mark.parametrize("params,region,field", [
    ({}, {"t_start": 0, "t_end": 31}, "region"),                                       # over 30 s
    ({}, {"t_start": 50, "t_end": 61}, "region"),                                      # past the end
    ({}, {"t_start": 5, "t_end": 5}, "region"),                                        # empty
    ({}, {"t_start": 5, "t_end": 5.00002}, "region"),                                  # about one sample
    ({"allowed_zone": {"t_start": 11, "t_end": 20}}, {"t_start": 10, "t_end": 14}, "allowed_zone"),
    ({"splice_bounds": {"t_start": 9, "t_end": 14}}, {"t_start": 10, "t_end": 14}, "splice_bounds"),
    ({"crossfade_ms": 5}, {"t_start": 10, "t_end": 14}, "crossfade_ms"),
    ({"crossfade_ms": 400}, {"t_start": 10, "t_end": 10.5}, "crossfade_ms"),          # two do not fit
    ({"takes": 3}, {"t_start": 10, "t_end": 14}, "takes"),
    ({"seed": -1}, {"t_start": 10, "t_end": 14}, "seed"),
    ({"caption": "  "}, {"t_start": 10, "t_end": 14}, "caption"),
    ({"cut_with_shift": True}, {"t_start": 50, "t_end": 60}, "cut_with_shift"),       # removal only
    ({"style": "x"}, {"t_start": 10, "t_end": 14}, "style"),                           # undeclared
])
def test_a_repaint_request_that_breaks_a_rule_is_refused_naming_the_field(params, region, field):
    with pytest.raises(re_.EditError) as exc:
        re_.normalize_params_edit("regenerate_region", params, region, duration_s=60)
    assert exc.value.field == field


def test_cut_with_shift_is_the_persons_word_and_only_near_the_end():
    near = {"t_start": 52, "t_end": 58}
    ok = re_.normalize_params_edit("remove_region", {"cut_with_shift": True}, near, duration_s=60)
    assert ok["cut_with_shift"] is True
    with pytest.raises(re_.EditError, match="only the person"):
        re_.normalize_params_edit("remove_region", {"cut_with_shift": True}, near, duration_s=60, by_person=False)
    with pytest.raises(re_.EditError, match="near the end"):
        re_.normalize_params_edit("remove_region", {"cut_with_shift": True}, {"t_start": 10, "t_end": 20},
                                  duration_s=60)


@pytest.mark.parametrize("action", ["regenerate_region", "fix_seam", "remove_region"])
def test_a_region_shorter_than_the_minimum_is_refused_at_proposal_as_region_too_short(action):
    """Review #180 1.4: a zone of about one frame made remove duplicate a frame and fail as a bug."""
    short = {"t_start": 5.0, "t_end": 5.0 + re_.MIN_REGION_S / 2}
    with pytest.raises(re_.EditError) as exc:
        re_.normalize_params_edit(action, {}, short, duration_s=60)
    assert exc.value.field == "region" and exc.value.message.startswith("region_too_short")
    # the minimum itself passes (a regenerate also needs room for its two default crossfades)
    at_least = re_.MIN_REGION_S if action != "regenerate_region" else 2 * re_.CROSSFADE_MS_DEFAULT / 1000.0
    ok = re_.normalize_params_edit(action, {}, {"t_start": 5.0, "t_end": 5.0 + at_least}, duration_s=60)
    assert ok["takes"] == re_.takes_for(action)
    # the removal itself refuses a one-sample region by name instead of a length_mismatch
    with pytest.raises(re_.EditError, match="region_too_short"):
        re_.remove(pcm(tone(1.0, 220)), (0.5, 0.5 + 1.0 / RATE), 75.0)


def test_a_track_whose_length_is_not_known_cannot_be_edited_yet():
    with pytest.raises(re_.EditError) as exc:
        re_.normalize_params_edit("regenerate_region", {}, {"t_start": 1, "t_end": 2}, duration_s=None)
    assert exc.value.field == "track_id"


# -- the engine window --------------------------------------------------------------------------------

def test_the_engine_window_is_the_allowed_zone_widened_to_the_25_hz_grid_and_survives_the_engines_int():
    w = re_.engine_window((10.013, 14.021), control_frames=60 * RATE)
    assert (w.frame_start, w.frame_end) == (250, 351)
    assert w.start_s <= 10.013 and w.end_s >= 14.021
    # acestep.cpp computes (int)(s * 48000 / 1920): the values sent land on the same frames
    assert int(w.sent_start_s * RATE / 1920) == 250 and int(w.sent_end_s * RATE / 1920) == 351
    # never beyond the control's last whole latent frame
    tail = re_.engine_window((59.0, 60.0), control_frames=int(59.99 * RATE))
    assert tail.frame_end == int(59.99 * RATE) // 1920


def test_a_source_that_is_not_a_whole_number_of_latent_frames_never_gets_a_window_past_its_end():
    """Review #180 1.3: `repainting_end` stayed on the next grid line past the source's length."""
    frames = 6 * RATE + 700                       # 6 s plus a partial latent frame (700 of 1920 samples)
    w = re_.engine_window((5.0, frames / RATE), control_frames=frames)
    assert w.frame_end == frames // 1920 == 150 and w.end_s * RATE <= frames
    assert w.sent_end_s <= frames / RATE and int(w.sent_end_s * RATE / 1920) == 150
    assert w.clamped_to_source is True and w.as_dict()["clamped_to_source"] is True
    # a source within the 2 ms nudge of a grid line: the end sent is the source's length, still on the line
    near = 6 * RATE + 40
    n = re_.engine_window((5.0, near / RATE), control_frames=near)
    assert n.sent_end_s <= near / RATE and int(n.sent_end_s * RATE / 1920) == n.frame_end == 150
    # a window inside the source is not marked
    assert "clamped_to_source" not in re_.engine_window((1.0, 2.0), control_frames=frames).as_dict()


def test_the_repaint_request_is_acestep_cpps_repaint_with_every_take_in_one_batch_and_float_output():
    w = re_.engine_window((10, 14), control_frames=60 * RATE)
    body = json.loads(re_.build_repaint_request(caption="indie rock", lyrics="[verse]\nla", window=w, seed=7,
                                                takes=2, inference_steps=8, synth_model="dit.gguf", vae="vae.gguf"))
    assert body["task_type"] == "repaint" and body["synth_batch_size"] == 2 and body["seed"] == 7
    assert body["output_format"] == "wav32"           # ace-server skips its peak normalisation for float
    assert (body["repainting_start"], body["repainting_end"]) == (w.sent_start_s, w.sent_end_s)
    assert body["caption"] == "indie rock" and body["synth_model"] == "dit.gguf" and body["vae"] == "vae.gguf"


# -- the splice -------------------------------------------------------------------------------------

def test_a_repaint_take_is_the_control_outside_the_splice_the_engine_inside_and_exactly_the_controls_length():
    control = pcm(tone(6.0, 220))
    engine = pcm(np.concatenate([tone(6.0, 330), np.zeros((1000, 2), np.float32)]))   # the engine's grid is longer
    result = re_.splice(control, engine, (2.0, 4.0), 75.0)
    take = joined(result)
    assert take.shape == control.data.shape and result.frames == control.frames
    a, b = result.splice_frames
    assert 2.0 * RATE <= a <= 2.0 * RATE + RATE // 100      # moved inward to a quiet sample, at most 10 ms
    assert 4.0 * RATE - RATE // 100 <= b <= 4.0 * RATE
    assert np.array_equal(take[:a], control.data[:a]) and np.array_equal(take[b:], control.data[b:])
    xf = int(round(0.075 * RATE))
    assert np.array_equal(take[a + xf:b - xf], engine.data[a + xf:b - xf])
    assert [s.side for s in result.seams] == ["start", "end"]
    # unrelated material on the two sides: equal-power (SPEC §6)
    assert all(s.shape == "equal_power" and s.coherence is not None and abs(s.coherence) < 0.85 for s in result.seams)


def test_when_the_engine_reproduced_the_boundary_material_the_crossfade_is_equal_gain_and_flat():
    control = pcm(tone(4.0, 220))
    engine = pcm(tone(4.0, 220))                 # the same material, phase-aligned
    result = re_.splice(control, engine, (1.0, 3.0), 75.0)
    assert all(s.shape == "equal_gain" and s.coherence >= re_.COHERENCE_LINEAR_MIN for s in result.seams)
    take = joined(result)
    assert np.max(np.abs(take - control.data)) < 1e-5   # equal-gain on coherent sides adds no bump


def test_an_equal_power_crossfade_that_would_pass_full_scale_is_gain_normalised_inside_the_fade_only():
    """Review #180 1.2: sin + cos is ~1.41x at the midpoint; loud, partly correlated sides went over full
    scale and the take failed its clipping gate. Outside the two fades the samples stay bit-exact."""
    control = pcm(tone(6.0, 50, amp=0.9))
    engine = pcm(tone(6.0, 50, amp=0.9, phase=math.pi / 3))   # corr = cos 60 deg = 0.5: equal-power
    result = re_.splice(control, engine, (2.0, 4.0), 75.0)
    take = joined(result)
    assert all(s.shape == "equal_power" for s in result.seams)
    for s in result.seams:
        g = s.level_guard
        assert g["applied"] == "gain_normalised" and g["peak_before"] > re_.FULL_SCALE >= g["peak_after"]
        assert s.as_dict(RATE)["level_guard"] == g
    assert float(np.max(np.abs(take))) <= re_.FULL_SCALE
    a, b = result.splice_frames
    xf = int(round(0.075 * RATE))
    assert float(np.max(np.abs(take[:a] - control.data[:a]))) == 0.0
    assert float(np.max(np.abs(take[b:] - control.data[b:]))) == 0.0
    assert float(np.max(np.abs(take[a + xf:b - xf] - engine.data[a + xf:b - xf]))) == 0.0
    entry = re_.seams_entry("regenerate_region", result, RATE, crossfade_ms=75.0)["entry"]
    assert all(m["level_guard"]["applied"] == "gain_normalised" for m in entry["seams"])
    # quiet sides need no guard, and the record says so
    quiet = re_.splice(pcm(tone(6.0, 220)), pcm(tone(6.0, 330)), (2.0, 4.0), 75.0)
    assert all(s.level_guard is None and s.as_dict(RATE)["level_guard"] is None for s in quiet.seams)


def test_a_splice_that_reaches_the_track_edge_has_no_seam_there():
    control = pcm(tone(3.0, 220))
    engine = pcm(tone(3.0, 330))
    result = re_.splice(control, engine, (0.0, 1.5), 50.0)
    assert [s.side for s in result.seams] == ["end"] and "no seam there" in result.notes[0]
    assert result.frames == control.frames


def test_an_engine_answer_that_does_not_cover_the_splice_or_has_another_rate_is_refused():
    control = pcm(tone(4.0, 220))
    with pytest.raises(re_.EditError, match="ends before the splice"):
        re_.splice(control, pcm(tone(2.0, 330)), (1.0, 3.0), 75.0)
    with pytest.raises(re_.EditError, match="Hz"):
        re_.splice(control, re_.Pcm(tone(4.0, 330), 44100), (1.0, 3.0), 75.0)


# -- the removal ------------------------------------------------------------------------------------

def test_a_removal_silences_the_region_with_fades_inside_it_and_keeps_every_other_sample_and_the_length():
    control = pcm(tone(5.0, 220))
    result = re_.remove(control, (2.0, 3.0), 75.0)
    take = joined(result)
    r0, r1, f = 2 * RATE, 3 * RATE, int(round(0.075 * RATE))
    assert take.shape == control.data.shape
    assert np.array_equal(take[:r0], control.data[:r0]) and np.array_equal(take[r1:], control.data[r1:])
    assert not take[r0 + f:r1 - f].any()                        # digital silence between the fades
    assert [s.kind for s in result.seams] == ["fade_out", "fade_in"]


def test_cut_with_shift_takes_the_region_out_and_moves_the_tail_earlier():
    control = pcm(np.concatenate([tone(2.0, 220), tone(1.0, 880), tone(1.0, 330)]))
    result = re_.remove(control, (2.0, 3.0), 50.0, cut_with_shift=True)
    take = joined(result)
    assert result.frames == control.frames - RATE == take.shape[0]
    f = int(round(0.05 * RATE))
    assert np.array_equal(take[2 * RATE + f:], control.data[3 * RATE + f:])   # the tail, shifted by 1 s
    assert "shorter" in result.notes[0]


def test_lyrics_after_a_cut_move_earlier_by_the_cut_and_lines_inside_it_go():
    lines = [{"t_start": 1.0, "t_end": 3.0, "text": "before"}, {"t_start": 4.0, "t_end": 5.0, "text": "inside"},
             {"t_start": 3.5, "t_end": 7.0, "text": "runs out of it"}, {"t_start": 8.0, "t_end": 9.0, "text": "after"},
             {"t_start": 2.0, "t_end": 4.0, "text": "runs into it"}, {"t_start": None, "t_end": None, "text": "unsynced"}]
    out = re_.retime_lines(lines, (3.2, 6.2))
    assert [l["text"] for l in out] == ["before", "runs out of it", "after", "runs into it", "unsynced"]
    assert out[0] == lines[0]
    assert (out[1]["t_start"], out[1]["t_end"]) == (pytest.approx(3.2), pytest.approx(4.0))   # starts at the cut
    assert (out[2]["t_start"], out[2]["t_end"]) == (pytest.approx(5.0), pytest.approx(6.0))
    assert (out[3]["t_start"], out[3]["t_end"]) == (2.0, pytest.approx(3.2))                   # clipped at the cut


# -- the seam numbers ----------------------------------------------------------------------------------

def test_a_repaint_takes_seam_numbers_fill_mp_qa_1_and_the_advisory_entry_never_gates():
    control = pcm(tone(6.0, 220))
    engine = pcm(tone(6.0, 330, amp=0.6))
    result = re_.splice(control, engine, (2.0, 4.0), 75.0)
    w = re_.engine_window((2.0, 4.0), control.frames)
    out = re_.seams_entry("regenerate_region", result, RATE, crossfade_ms=75.0, window=w)
    fields = out["qa_fields"]
    for k in ("seam_start_jump_db", "seam_end_jump_db", "seam_click_score"):
        assert isinstance(fields[k], float) and math.isfinite(fields[k]) and fields[k] >= 0
    assert fields["seam_start_jump_db"] == pytest.approx(20 * math.log10(2), abs=0.5)   # 0.3 -> 0.6
    entry = out["entry"]
    assert entry["status"] == "measured" and "never a gate" in entry["reason"]
    assert entry["engine_window"]["grid_hz"] == 25 and len(entry["seams"]) == 2
    assert all(s["step_vs_p99"] is not None for s in entry["seams"])
    json.dumps(out, allow_nan=False)


def test_a_seam_against_digital_silence_is_null_with_a_reason_never_inf():
    control = pcm(np.zeros((4 * RATE, 2), np.float32))
    engine = pcm(tone(4.0, 330))
    result = re_.splice(control, engine, (1.0, 3.0), 75.0)
    out = re_.seams_entry("regenerate_region", result, RATE, crossfade_ms=75.0)
    assert out["qa_fields"]["seam_start_jump_db"] is None
    assert "denominator_zero" in out["entry"]["seams"][0]["null_reasons"]["jump_db"]
    json.dumps(out, allow_nan=False)   # nothing a webview's JSON.parse would refuse


def test_a_removed_regions_seam_fields_are_null_and_its_fades_are_measured_instead():
    control = pcm(tone(5.0, 220))
    result = re_.remove(control, (2.0, 3.0), 75.0)
    out = re_.seams_entry("remove_region", result, RATE, crossfade_ms=75.0)
    assert out["qa_fields"] == {"seam_start_jump_db": None, "seam_end_jump_db": None, "seam_click_score": None}
    rr = out["entry"]["removed_region"]
    assert rr["fade_ms"] == 75.0 and [e["kind"] for e in rr["edges"]] == ["fade_out", "fade_in"]
    assert all(e["fade_ms"] == pytest.approx(75.0, abs=0.1) for e in rr["edges"])
    assert rr["edges"][0]["t_s"] == pytest.approx(2.0, abs=0.001)


# -- WAV in and out -----------------------------------------------------------------------------------

def test_a_24_bit_wav_written_from_pieces_reads_back_sample_for_sample(tmp_path):
    a, b = tone(0.5, 220), tone(0.25, 440)
    dest = tmp_path / "take.wav"
    frames = re_.write_wav_s24(dest, [a, b], rate=RATE, channels=2, chunk=1000)
    back = re_.read_pcm(dest)
    assert frames == back.frames == a.shape[0] + b.shape[0] and back.rate == RATE and back.channels == 2
    want = np.round(np.concatenate([a, b]).astype(np.float64) * 8388608.0) / 8388608.0
    assert np.max(np.abs(back.data - want)) < 1e-7
    # samples past full scale are clipped (QA counts them), never wrapped
    loud = tmp_path / "loud.wav"
    re_.write_wav_s24(loud, [np.full((10, 2), 1.5, np.float32)], rate=RATE, channels=2)
    assert re_.read_pcm(loud).data.max() == pytest.approx(8388607 / 8388608.0)


def test_a_float_wav_inside_a_bigger_file_is_read_from_its_range(tmp_path):
    x = tone(0.1, 330)
    pcm32 = x.reshape(-1).astype("<f4").tobytes()
    import struct
    fmt = struct.pack("<HHIIHH", 3, 2, RATE, RATE * 8, 8, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(pcm32)) + pcm32
    wav = b"RIFF" + struct.pack("<I", len(body)) + body
    blob = tmp_path / "multipart.part"
    blob.write_bytes(b"--b\r\nheaders\r\n\r\n" + wav + b"\r\n--b--\r\n")
    start = len(b"--b\r\nheaders\r\n\r\n")
    back = re_.read_pcm(blob, start, start + len(wav))
    assert back.frames == x.shape[0] and np.array_equal(back.data, x)


def test_the_takes_per_job_follow_the_spec_limits():
    assert re_.takes_for("regenerate_region") == re_.takes_for("fix_seam") == 2
    assert re_.takes_for("remove_region") == 1
    assert set(re_.BUILT_ACTIONS) < set(js.EDIT_ACTIONS)


def test_tail_edit_on_40ms_multiple_plus_17ms_track_agrees_with_engine_window():
    """Tracks of N * 40 ms + 17 ms (250 * 40 ms + 17 ms = 10.017 s): splice must agree with
    engine_window's whole-frame end so whole-frame engine outputs are not rejected and a padded
    unrepainted 17 ms tail still gets its crossfade."""
    frames_whole = 250 * 1920                       # 10.000 s (480000 samples)
    rem = int(round(0.017 * RATE))                  # 17 ms (816 samples)
    control_frames = frames_whole + rem             # 10.017 s (480816 samples)
    t_ctrl = np.arange(control_frames) / RATE
    ctrl_wave = (0.3 * np.sin(2 * math.pi * 220 * t_ctrl + 0.7)).astype(np.float32)
    control = pcm(np.stack([ctrl_wave, ctrl_wave], axis=1))
    win = re_.engine_window((8.0, control_frames / RATE), control_frames=control.frames)
    assert (win.frame_start, win.frame_end, win.clamped_to_source) == (200, 250, True)

    # 1. Real acestep.cpp whole-frame answer (480000 samples): must NOT reject variants.
    t_eng = np.arange(frames_whole) / RATE
    eng_wave = (0.25 * np.sin(2 * math.pi * 440 * t_eng + 1.2)).astype(np.float32)
    engine_whole = pcm(np.stack([eng_wave, eng_wave], axis=1))
    res_whole = re_.splice(control, engine_whole, (8.0, control_frames / RATE), 75.0, window=win)
    take_whole = joined(res_whole)
    assert res_whole.frames == control.frames == control_frames
    assert [s.side for s in res_whole.seams] == ["start", "end"]
    assert res_whole.seams[1].end <= frames_whole
    assert np.array_equal(take_whole[frames_whole:], control.data[frames_whole:])
    # Smooth transition across the 10.000 s boundary (no uncrossfaded jump).
    step_at_boundary = float(np.max(np.abs(take_whole[frames_whole] - take_whole[frames_whole - 1])))
    assert step_at_boundary < 0.05

    # 2. Padded/ceil engine answer (251 frames = 481920 samples) where [480000, 480816) was NOT
    #    repainted (holds control samples): must NOT drop the end crossfade at 480000.
    padded = np.zeros((251 * 1920, 2), dtype=np.float32)
    padded[:frames_whole] = engine_whole.data
    padded[frames_whole:control_frames] = control.data[frames_whole:control_frames]
    res_padded = re_.splice(control, pcm(padded), (8.0, control_frames / RATE), 75.0, window=win)
    take_padded = joined(res_padded)
    assert res_padded.frames == control.frames == control_frames
    assert [s.side for s in res_padded.seams] == ["start", "end"]
    assert res_padded.seams[1].end <= frames_whole
    assert float(np.max(np.abs(take_padded[frames_whole] - take_padded[frames_whole - 1]))) < 0.05

    # 3. Start and middle edits on the same 10.017 s track with a whole-frame engine output.
    win_start = re_.engine_window((0.0, 2.0), control_frames=control.frames)
    res_start = re_.splice(control, engine_whole, (0.0, 2.0), 75.0, window=win_start)
    assert res_start.frames == control_frames and [s.side for s in res_start.seams] == ["end"]

    win_mid = re_.engine_window((2.0, 4.0), control_frames=control.frames)
    res_mid = re_.splice(control, engine_whole, (2.0, 4.0), 75.0, window=win_mid)
    assert res_mid.frames == control_frames and [s.side for s in res_mid.seams] == ["start", "end"]

    # 4. Tight tail region (9.86..10.017 s = 157 ms >= 2 * 75 ms) clamped to 10.000 s (140 ms).
    win_tight = re_.engine_window((9.86, control_frames / RATE), control_frames=control.frames)
    res_tight = re_.splice(control, engine_whole, (9.86, control_frames / RATE), 75.0, window=win_tight)
    assert res_tight.frames == control_frames and [s.side for s in res_tight.seams] == ["start", "end"]


def test_duration_rounding_near_frame_aligned_end_adds_no_spurious_crossfade():
    """If the engine window reaches the track's exact end and splice_s[1] fell short only from
    duration rounding (e.g. 9.9999 s or probed 9.995 s on a 10.000 s track), no spurious 75 ms
    crossfade is added before the rounding remainder."""
    control = pcm(tone(10.0, 220))                    # 250 * 40 ms exact (480000 samples)
    engine = pcm(tone(10.0, 330))
    win = re_.engine_window((8.0, 9.9999), control_frames=control.frames)
    assert win.frame_end == 250 and not win.clamped_to_source
    res_round = re_.splice(control, engine, (8.0, 9.9999), 75.0, window=win)
    assert [s.side for s in res_round.seams] == ["start"]
    assert "reaches the track's end" in res_round.notes[0]

    win_probed = re_.engine_window((8.0, 9.995), control_frames=control.frames)
    res_probed = re_.splice(control, engine, (8.0, 9.995), 75.0, window=win_probed, source_duration_s=9.995)
    assert [s.side for s in res_probed.seams] == ["start"]
    assert re_.effective_splice((8.0, 10.017), 480816, RATE, window=re_.engine_window((8.0, 10.017), 480816)) == (8.0, 10.0)
    assert re_.EngineWindow.from_dict(re_.engine_window((8.0, 10.017), 480816).as_dict()).clamped_to_source is True
