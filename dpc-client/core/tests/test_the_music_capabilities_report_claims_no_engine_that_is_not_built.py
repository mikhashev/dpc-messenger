"""The `mp.capabilities/1` report (SPEC §7) says what the music service can do now. Its
engine part comes from a pluggable probe. Today's probe answers "engine adapter not
built", so `generate_from_text` is unsupported and a proposed job comes back
`unsupported` with that reason. Once an engine is installed (stopped is enough: DPC
starts it), the job type is supported and a job queues.
"""

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.capabilities import EngineStatus, MusicCapabilities, adapter_not_built_probe
from dpc_client_core.music.jobs import MusicJobStore
from tests._music_jobs_helpers import from_text_params, installed_probe, store_at


def test_today_the_engine_is_reported_as_not_built_and_nothing_claims_it():
    report = MusicCapabilities(ffmpeg_probe=lambda: False).report()
    assert report["schema"] == "mp.capabilities/1"
    assert report["engine"]["state"] == "not_built"
    assert report["engine"]["available"] is False
    assert report["engine"]["reason"] == "engine adapter not built"
    assert report["job_types"]["generate_from_text"] == {"status": "unsupported", "reason": "engine adapter not built"}
    assert report["features"]["text2music"]["status"] == "unsupported"
    assert all(v["status"] != "supported" for v in report["job_types"].values())


def test_without_an_engine_only_the_removal_runs_because_it_needs_no_model():
    report = MusicCapabilities(ffmpeg_probe=lambda: True).report()
    actions = report["actions"]
    assert actions["remove_region"]["status"] == "supported" and "no model" in actions["remove_region"]["reason"]
    for repaint in ("regenerate_region", "fix_seam"):
        assert actions[repaint]["status"] == "unsupported"
        assert "acestep.cpp" in actions[repaint]["reason"] and "engine adapter not built" in actions[repaint]["reason"]
    assert report["job_types"]["edit"]["status"] == "supported"
    assert {k for k, v in report["job_types"].items() if v["status"] == "supported"} == {"edit"}


def test_every_job_type_and_edit_action_is_listed_with_a_status_and_a_reason():
    report = MusicCapabilities(installed_probe, ffmpeg_probe=lambda: True).report()
    assert set(report["job_types"]) == set(js.JOB_TYPES)
    assert set(report["actions"]) == set(js.EDIT_ACTIONS)
    for block in (report["job_types"], report["actions"], report["features"]):
        for name, st in block.items():
            assert st["status"] in ("supported", "planned", "unsupported"), name
            assert st["reason"].strip(), name
    assert report["job_types"]["generate_from_text"]["status"] == "supported"
    assert report["job_types"]["edit"]["status"] == "supported"
    for planned in ("transcribe", "qc", "generate_from_melody"):
        assert report["job_types"][planned]["status"] == "planned"
    # the song types follow their engines (similar.py): a single installed probe answers for acestep
    for song in ("generate_similar", "generate_from_lyrics", "cover"):
        assert report["job_types"][song]["status"] == "supported", song
    assert report["job_types"]["voice_clone"]["status"] == "unsupported"
    # QC is not a child job: it is folded into the take (mp.qc/1), so the job type stays planned and the
    # feature is supported, each with its own reason
    assert "no qc child job" in report["job_types"]["qc"]["reason"]
    qc = report["features"]["qc"]
    assert qc["status"] == "supported" and "mp.qc/1" in qc["reason"] and "judge_music_take" in qc["reason"]
    assert "off by default" in qc["reason"]


def test_the_report_says_which_region_edits_are_built_and_extend_ending_is_not():
    from dpc_client_core.music.comments import SUPPORTED_ACTIONS
    actions = MusicCapabilities(installed_probe, ffmpeg_probe=lambda: True).report()["actions"]
    assert set(SUPPORTED_ACTIONS) == {"note", "regenerate_region", "fix_seam", "remove_region"}
    assert actions["note"]["status"] == "supported" and "not built" not in actions["note"]["reason"]
    for built in ("regenerate_region", "fix_seam", "remove_region"):
        assert actions[built]["status"] == "supported", built
    assert actions["extend_ending"]["status"] == "planned"
    # every edit decodes the track with ffmpeg: without it none runs, and the reason says so
    no_ffmpeg = MusicCapabilities(installed_probe, ffmpeg_probe=lambda: False).report()["actions"]
    for built in ("regenerate_region", "fix_seam", "remove_region"):
        assert no_ffmpeg[built]["status"] == "unsupported" and "ffmpeg" in no_ffmpeg[built]["reason"]


def test_the_limits_are_the_slice_one_decisions_per_engine():
    report = MusicCapabilities().report()
    limits = report["limits"]
    assert limits["max_track_s"] == 600
    g = limits["generate_from_text"]
    assert g["variants"] == {"min": 1, "max": 4, "default": 2}
    # The top-level duration is the default engine's: yue2 (decision 40), a cap.
    assert g["engine"] == "yue2" == report["default_engine"] and g["engine_choices"] == ["yue2", "acestep"]
    assert g["duration_s"] == {"min": 30, "max": 360, "default": 180}
    assert (g["seed"]["min"], g["seed"]["max"]) == (0, 2 ** 32 - 1)
    ace = g["engines"]["acestep"]
    assert ace["duration_s"] == {"min": 10, "max": 600, "default": 120}
    assert (ace["dit_model"], ace["dit_quant"], ace["inference_steps"]) == ("turbo", "Q8", 8)
    yue = g["engines"]["yue2"]
    assert yue["duration_is_cap"] is True and yue["backbones"] == ["stock", "rs-tw"] and yue["cot_default"] == "full"
    assert yue["adapter_scale"]["music_default"] == 0.45 and yue["adapter_scale"]["sound_default"] == 0.35
    assert (g["engine_output"], g["library_format"]) == ("wav24", "flac")


def test_the_report_names_the_discard_classes_and_that_a_reason_is_needed():
    decisions = MusicCapabilities().report()["take_decisions"]
    assert decisions["discard_needs_reason"] is True
    assert [c["id"] for c in decisions["failure_classes"]] == list(js.FAILURE_CLASSES)
    assert all(c["meaning"] for c in decisions["failure_classes"])


def test_the_qa_block_lists_the_spec_fields_and_says_which_ones_stage_1_measures():
    qa = MusicCapabilities().report()["qa"]
    assert qa["fields"] == ["integrated_lufs", "true_peak_dbtp", "loudness_delta_lu", "seam_start_jump_db",
                            "seam_end_jump_db", "seam_click_score", "clipped_samples", "lyrics_wer"]
    assert set(qa["advisory"]) <= set(qa["fields"])
    assert qa["status"] == "supported" and "never rejects" in qa["reason"]
    # every field says whether anything produces it; stage 1 measures loudness, true peak and clipping
    assert set(qa["field_status"]) == set(qa["fields"])
    measured = {k for k, v in qa["field_status"].items() if v["status"] == "measured"}
    # lyrics_wer is measured by qc.py inside mp.qc/1 only; take.qa keeps it null, and the qa reason says so
    assert measured == {"integrated_lufs", "true_peak_dbtp", "clipped_samples", "seam_start_jump_db",
                        "seam_end_jump_db", "seam_click_score", "lyrics_wer"}
    wer = qa["field_status"]["lyrics_wer"]["reason"]
    assert "qc.py" in wer and "mp.qc/1" in wer and "never a gate" in wer and "take.qa keeps null" in wer
    assert "lyrics WER is not in take.qa" in qa["reason"] and "measured in mp.qc/1" in qa["reason"]
    for k in ("seam_start_jump_db", "seam_end_jump_db", "seam_click_score"):
        assert "region-edit" in qa["field_status"][k]["reason"] and "never a gate" in qa["field_status"][k]["reason"]
    assert qa["field_status"]["loudness_delta_lu"]["status"] == "planned"
    assert qa["field_status"]["loudness_delta_lu"]["reason"]
    # the gates and the number that judges each; seams and WER never gate
    assert qa["gates"]["names"] == ["length", "clipping", "true_peak", "silence"]
    assert qa["gates"]["thresholds"]["tail_silence_max_s"] == 5.0 and qa["gates"]["thresholds"]["true_peak_max_dbtp"] == -1.0
    assert qa["gates"]["advisory_only"] == ["seams", "lyrics_wer"]


def test_a_job_proposed_while_the_engine_is_not_built_comes_back_unsupported(tmp_path):
    store = MusicJobStore(tmp_path)
    store.open()
    job = store.create_job("generate_from_text", from_text_params())
    assert job["state"] == "unsupported"
    assert job["state_reason"] == "engine adapter not built"
    assert [h["state"] for h in job["history"]] == ["queued", "unsupported"]
    js.validate_job(job)


def test_a_planned_job_type_comes_back_unsupported_with_planned_in_the_reason(tmp_path):
    job = store_at(tmp_path).create_job("generate_from_melody", {"caption": "punk"}, track_id="trk_0123456789abcdef",
                                        base_version_id="ver_0123456789abcdef")
    assert job["state"] == "unsupported" and job["state_reason"].startswith("planned, not built:")


def test_an_installed_engine_lets_a_job_queue(tmp_path):
    job = store_at(tmp_path).create_job("generate_from_text", from_text_params())
    assert job["state"] == "queued" and job["state_reason"] is None


def test_the_probe_is_read_again_on_every_report():
    answers = iter([adapter_not_built_probe(), installed_probe()])
    caps = MusicCapabilities(lambda: next(answers))
    assert caps.report()["job_types"]["generate_from_text"]["status"] == "unsupported"
    assert caps.report()["job_types"]["generate_from_text"]["status"] == "supported"


def test_a_probe_that_raises_or_answers_nonsense_reports_an_error_engine():
    def broken():
        raise RuntimeError("port closed")

    for probe in (broken, lambda: {"state": "running"}):
        report = MusicCapabilities(probe).report()
        assert report["engine"]["state"] == "error" and report["engine"]["available"] is False
        assert report["job_types"]["generate_from_text"]["status"] == "unsupported"


def test_only_stopped_starting_and_running_engines_are_available():
    for state in ("not_built", "not_installed", "error"):
        assert EngineStatus(state=state, reason="x").available is False
    for state in ("stopped", "starting", "running"):
        assert EngineStatus(state=state, reason="x").available is True
    try:
        EngineStatus(state="sleeping", reason="x")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown engine state was accepted")
