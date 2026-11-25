"""Backlog: takes made before stage-1 QA landed show "not measured". `backfill_music_qa` measures
them from their files, stores `qa` / `qa_report` through the job store and assembles `mp.qc/1`
again, keeping a judge result already stored. A missing file is `no_file` for that take only."""

import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import qa, qc
from dpc_client_core.music.commands import MusicCommandsMixin
from dpc_client_core.music.qc_commands import QC_COMMANDS, MusicQcCommandsMixin
from tests._music_jobs_helpers import running_job, store_at


def _sine(path: Path, seconds=0.2, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(int(seconds * rate))))
    return path


class _Settings:
    def __init__(self, home):
        self.dpc_home_dir = home

    def get(self, section, key, fallback=None):
        return fallback


def _fake_record(path, **kwargs):
    gates = {
        "length": qa.length_gate(kwargs["requested_s"], kwargs["duration_s"], kwargs["length_mode"]),
        "clipping": qa.clipping_gate({"peak_db": -3.0, "peak_count": 1})[0],
        "true_peak": qa.true_peak_gate(-2.0),
        "silence": qa.silence_gate({"whole": False, "leading_s": 0.0, "tail_s": 0.0,
                                    "longest_internal_s": 0.0, "runs": []}),
    }
    return qa.build_record(gates=gates, measurements={"duration_s": kwargs["duration_s"]},
                           integrated_lufs=-14.0, true_peak_dbtp=-2.0, clipped_samples=0,
                           measured_by="test ffmpeg")


class _Service(MusicCommandsMixin, MusicQcCommandsMixin):
    def __init__(self, home, meter=None, real=False):
        self.settings = _Settings(home)
        self._music_jobs_instance = store_at(home)
        self.broadcasts = []
        self.calls = []
        if not real:
            self._music_qa_meter = meter or self._meter

    def _music_job_changed(self, job):
        self.broadcasts.append(job["id"])

    def _meter(self, path, **kwargs):
        self.calls.append((Path(path).name, kwargs))
        return _fake_record(path, **kwargs)


def _old_job(store, tmp_path, files=(True, True), **params):
    params.setdefault("duration_s", 30)
    """A reviewed job whose takes have no qa, qa_report or qc, as before stage-1 QA."""
    job = running_job(store, variants=len(files), **params)
    takes = []
    for v, exists in enumerate(files, 1):
        path = tmp_path / f"{job['id']}_v{v}.wav"
        if exists:
            _sine(path)
        takes.append(store.add_take(
            job["id"], variant=v, file_uri=path.as_uri(), content_hash=f"sha256:{v:064x}", title=f"v{v}",
            seed=42, synth_batch_size=len(files), synth_batch_index=v - 1, payload="lm.request.json",
            sample_rate=16000, channels=1, duration_s=30.0))
    return store.finish_job(job["id"]), takes


def test_the_command_is_on_the_door_and_slow():
    from dpc_client_core.local_api import ALLOWED_COMMANDS
    assert "backfill_music_qa" in QC_COMMANDS and "backfill_music_qa" in ALLOWED_COMMANDS
    assert getattr(MusicQcCommandsMixin.backfill_music_qa, "dpc_slow", False)


@pytest.mark.asyncio
async def test_old_takes_get_qa_and_qc_stored_and_the_job_is_broadcast(tmp_path):
    service = _Service(tmp_path / "dpc")
    store = service._music_jobs_instance
    job, takes = _old_job(store, tmp_path)
    assert all(t.get("qa_report") is None and "qc" not in t for t in takes)
    out = await service.backfill_music_qa(job_id=job["id"])
    assert out["status"] == "ok" and out["counts"] == {"measured": 2, "skipped": 0, "no_file": 0, "failed": 0}
    assert [r["outcome"] for r in out["takes"]] == ["measured", "measured"]
    assert [c[0] for c in service.calls] == [f"{job['id']}_v1.wav", f"{job['id']}_v2.wav"]
    for t in takes:
        stored = store.get_take(t["id"])
        js.check_qa(stored["qa"])
        js.check_qa_report(stored["qa_report"])
        assert stored["qa_report"]["verdict"] == "passed" and "backfill" in stored["qa_report"]["note"]
        js.check_qc(stored["qc"])
        assert stored["qc"]["stage1"]["verdict"] == "passed" and stored["qc"]["judge"]["status"] == "absent"
        assert stored["decision"] == "pending"
    assert service.broadcasts == [job["id"]]          # once per changed job


@pytest.mark.asyncio
async def test_the_length_terms_come_from_the_take_and_its_job(tmp_path):
    service = _Service(tmp_path / "dpc")
    store = service._music_jobs_instance
    _, takes = _old_job(store, tmp_path, files=(True,))
    await service.backfill_music_qa(take_id=takes[0]["id"])
    kwargs = service.calls[0][1]
    assert kwargs["requested_s"] == 30.0 and kwargs["length_mode"] == qa.LENGTH_EXACT
    assert kwargs["duration_s"] == 30.0 and "edit" not in kwargs
    yue = {"id": "tak_1", "engine": "yue2:ggml", "duration_requested_s": 90.0}
    assert MusicQcCommandsMixin._backfill_terms(yue, {"type": "generate_from_text", "params": {}}) == \
        {"requested_s": 90.0, "length_mode": qa.LENGTH_CAP}
    none = MusicQcCommandsMixin._backfill_terms({"id": "t"}, {"type": "generate_from_text", "params": {}})
    assert none == {"requested_s": None, "length_mode": qa.LENGTH_EXACT}   # the gate stays not_measured
    assert qa.length_gate(None, 30.0, qa.LENGTH_EXACT)["status"] == "not_measured"


def test_an_edit_take_is_measured_by_the_controls_length_and_its_zone():
    job = {"type": "edit", "action": "regenerate_region", "region": {"t_start": 10.0, "t_end": 14.0},
           "params": {"allowed_zone": {"t_start": 8.0, "t_end": 16.0},
                      "splice_bounds": {"t_start": 9.0, "t_end": 15.0}}}
    take = {"kind": "edit", "duration_requested_s": 60.0}
    terms = MusicQcCommandsMixin._backfill_terms(take, job)
    assert terms["length_mode"] == qa.LENGTH_SAME and terms["requested_s"] == 60.0
    assert terms["edit"]["zone"] == (9.0, 15.0) and terms["edit"]["expects_silence"] is False
    assert terms["edit"]["seams"] == {}
    removal = MusicQcCommandsMixin._backfill_terms(take, {**job, "action": "remove_region"})
    assert removal["edit"]["zone"] == (10.0, 14.0) and removal["edit"]["expects_silence"] is True
    cut = MusicQcCommandsMixin._backfill_terms(take, {**job, "action": "remove_region",
                                                      "params": {**job["params"], "cut_with_shift": True}})
    assert cut["edit"]["zone"] is None


@pytest.mark.asyncio
async def test_a_missing_file_is_no_file_and_the_others_go_on(tmp_path):
    service = _Service(tmp_path / "dpc")
    store = service._music_jobs_instance
    job, takes = _old_job(store, tmp_path, files=(False, True))
    out = await service.backfill_music_qa(job_id=job["id"])
    assert [r["outcome"] for r in out["takes"]] == ["no_file", "measured"]
    assert out["counts"] == {"measured": 1, "skipped": 0, "no_file": 1, "failed": 0}
    assert store.get_take(takes[0]["id"]).get("qa_report") is None
    assert store.get_take(takes[1]["id"])["qa_report"]["verdict"] == "passed"


@pytest.mark.asyncio
async def test_a_failing_meter_fails_that_take_only(tmp_path):
    seen = []

    def meter(path, **kwargs):
        seen.append(path)
        if len(seen) == 1:
            raise qa.QaError("ffmpeg could not be run")
        return _fake_record(path, **kwargs)
    service = _Service(tmp_path / "dpc", meter=meter)
    job, takes = _old_job(service._music_jobs_instance, tmp_path)
    out = await service.backfill_music_qa(job_id=job["id"])
    assert [r["outcome"] for r in out["takes"]] == ["failed", "measured"]
    assert "ffmpeg could not be run" in out["takes"][0]["error"]


@pytest.mark.asyncio
async def test_measured_takes_are_skipped_unless_forced_and_a_judge_result_is_kept(tmp_path):
    service = _Service(tmp_path / "dpc")
    store = service._music_jobs_instance
    job, takes = _old_job(store, tmp_path, files=(True,))
    await service.backfill_music_qa(job_id=job["id"])
    stored = store.get_take(takes[0]["id"])
    judged = qc.assemble(stored, store.get_job(job["id"]), judge={
        "judge": {"status": "done", "model": "m", "runtime": "r", "rubric": qc.RUBRICS["lyrics"], "alias": "a",
                  "error": None, "judged_at": "2026-11-25T00:00:00Z"},
        "scores": {k: 7.0 for k in js.QC_SCORE_KEYS}, "issues": [], "reasons": "fine", "confidence": 0.5})
    store.set_take_qc(takes[0]["id"], judged)
    service.calls.clear()
    again = await service.backfill_music_qa(take_id=takes[0]["id"])
    assert again["takes"][0]["outcome"] == "skipped" and service.calls == []
    forced = await service.backfill_music_qa(take_id=takes[0]["id"], force=True)
    assert forced["takes"][0]["outcome"] == "measured" and len(service.calls) == 1
    kept = store.get_take(takes[0]["id"])["qc"]
    assert kept["judge"]["status"] == "done" and kept["scores"]["overall"] == 7.0


@pytest.mark.asyncio
async def test_a_take_whose_report_is_not_measured_is_measured_again(tmp_path):
    service = _Service(tmp_path / "dpc")
    store = service._music_jobs_instance
    job, takes = _old_job(store, tmp_path, files=(True,))
    bad = qa.not_measured_record("ffmpeg is not installed")
    store.set_take_qa(takes[0]["id"], bad["qa"], bad["qa_report"])
    out = await service.backfill_music_qa()          # every job
    assert out["counts"]["measured"] == 1
    assert store.get_take(takes[0]["id"])["qa_report"]["verdict"] == "passed"


@pytest.mark.asyncio
async def test_the_command_maps_its_errors(tmp_path):
    service = _Service(tmp_path / "dpc")
    assert (await service.backfill_music_qa(take_id="tak_0000000000000000"))["code"] == "not_found"
    assert (await service.backfill_music_qa(job_id="job_0000000000000000"))["code"] == "not_found"
    assert (await service.backfill_music_qa(force="yes"))["code"] == "invalid_params"
    assert (await service.backfill_music_qa(job_id=5))["code"] == "invalid_job_id"
    assert (await service.backfill_music_qa())["counts"] == {"measured": 0, "skipped": 0, "no_file": 0, "failed": 0}


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
async def test_the_real_ffmpeg_meter_measures_a_backfilled_take(tmp_path):
    service = _Service(tmp_path / "dpc", real=True)      # no stand-in: qa.ffmpeg_qa_meter
    job, takes = _old_job(service._music_jobs_instance, tmp_path, files=(True,))
    out = await service.backfill_music_qa(job_id=job["id"])
    assert out["takes"][0]["outcome"] == "measured", out
    stored = service._music_jobs_instance.get_take(takes[0]["id"])
    assert stored["qa_report"]["gates"]["clipping"]["status"] == "pass"


@pytest.mark.asyncio
async def test_the_job_qc_says_whether_a_judge_alias_is_configured(tmp_path):
    service = _Service(tmp_path / "dpc")
    job, _ = _old_job(service._music_jobs_instance, tmp_path)
    assert (await service.get_music_job_qc(job_id=job["id"]))["judge_alias"] is None
    service.settings.get = lambda section, key, fallback=None: (
        "music_flamingo" if (section, key) == ("music", "qc_judge_alias") else fallback)
    assert (await service.get_music_job_qc(job_id=job["id"]))["judge_alias"] == "music_flamingo"


@pytest.mark.asyncio
async def test_a_take_whose_length_cannot_be_recovered_is_marked_unmeasurable_and_skipped_next_time(tmp_path):
    """Follow-up 16 (Zcode #188, finding 6): no requested length, so the length gate can never pass;
    the second library-wide run neither runs ffmpeg, nor writes, nor broadcasts. `force` and a new
    file hash measure it again."""
    service = _Service(tmp_path / "dpc")
    store = service._music_jobs_instance
    job = running_job(store, variants=1)          # the job has no duration_s either
    path = _sine(tmp_path / "none.wav")
    take = store.add_take(job["id"], variant=1, file_uri=path.as_uri(), content_hash=f"sha256:{1:064x}", title="v1",
                          seed=1, synth_batch_size=1, synth_batch_index=0, payload="lm.request.json",
                          sample_rate=16000, channels=1, duration_s=0.2)
    job = store.finish_job(job["id"])
    # a take of an old job: no length was recorded anywhere (a current job always has `duration_s`)
    service._backfill_terms = lambda t, j: {"requested_s": None, "length_mode": qa.LENGTH_EXACT}
    first = await service.backfill_music_qa()
    assert first["counts"]["measured"] == 1 and len(service.calls) == 1
    report = store.get_take(take["id"])["qa_report"]
    js.check_qa_report(report)
    assert report["verdict"] == "not_measured"
    mark = report["unmeasurable"]
    assert mark["attempts"] == 1 and mark["file_hash"] == f"sha256:{1:064x}" and mark["last_attempt_at"]
    assert "length" in mark["reason"]
    service.broadcasts.clear()
    again = await service.backfill_music_qa()
    assert again["counts"] == {"measured": 0, "skipped": 1, "no_file": 0, "failed": 0}
    assert len(service.calls) == 1 and service.broadcasts == []
    assert store.get_take(take["id"])["qa_report"]["unmeasurable"] == mark       # nothing was written
    forced = await service.backfill_music_qa(take_id=take["id"], force=True)
    assert forced["takes"][0]["outcome"] == "measured" and len(service.calls) == 2
    assert store.get_take(take["id"])["qa_report"]["unmeasurable"]["attempts"] == 2
    # the file's hash changed (inputs changed): measured again, the counter starts over
    with store._lock:
        store._take(take["id"])[1]["file"]["content_hash"] = f"sha256:{2:064x}"
    changed = await service.backfill_music_qa()
    assert changed["counts"]["measured"] == 1 and len(service.calls) == 3
    assert store.get_take(take["id"])["qa_report"]["unmeasurable"]["attempts"] == 1


@pytest.mark.asyncio
async def test_a_take_that_stays_not_measured_for_another_reason_gets_a_bounded_number_of_attempts(tmp_path):
    from dpc_client_core.music import qc_commands

    def meter(path, **kwargs):
        service.calls.append((Path(path).name, kwargs))
        rec = qa.not_measured_record("ffmpeg could not read the file", requested_s=kwargs["requested_s"])
        return rec
    service = _Service(tmp_path / "dpc", meter=meter)
    store = service._music_jobs_instance
    job, takes = _old_job(store, tmp_path, files=(True,))
    for _ in range(qc_commands.UNMEASURABLE_ATTEMPTS + 2):
        await service.backfill_music_qa()
    assert len(service.calls) == qc_commands.UNMEASURABLE_ATTEMPTS
    mark = store.get_take(takes[0]["id"])["qa_report"]["unmeasurable"]
    assert mark["attempts"] == qc_commands.UNMEASURABLE_ATTEMPTS and "ffmpeg could not read" in mark["reason"]


def test_the_unmeasurable_mark_has_its_schema_shape():
    rec = qa.not_measured_record("x")
    report = {**rec["qa_report"], "unmeasurable": {"reason": "r", "attempts": 1, "last_attempt_at": "t", "file_hash": None}}
    js.check_qa_report(report)
    for bad in ({"reason": "r"}, {"reason": "r", "attempts": 0, "last_attempt_at": "t", "file_hash": None},
                {"reason": "", "attempts": 1, "last_attempt_at": "t", "file_hash": None}):
        with pytest.raises(js.SchemaError):
            js.check_qa_report({**rec["qa_report"], "unmeasurable": bad})
