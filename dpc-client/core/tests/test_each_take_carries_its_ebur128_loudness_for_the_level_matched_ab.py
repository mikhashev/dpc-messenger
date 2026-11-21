"""SPEC §12 decision 37 (slice 1: a plain A/B with level matching), §8 (integrated LUFS and
true peak through ffmpeg ebur128), decision 29 (advisory, never a gate).

The runner measures every take after its FLAC is written and stores the result as the
take's `loudness` record; a measurement that fails leaves it null and never fails the job.
`measure_music_take_loudness` measures a take that has none. The person's own job changes
are broadcast as `music_job_updated`, like the runner's.
"""

import asyncio
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from dpc_client_core.music import commands
from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.loudness import (
    MEASURED_BY,
    LoudnessError,
    ffmpeg_loudness_meter,
    measure_loudness,
    parse_ebur128_summary,
)
from dpc_client_core.music.runner import ffmpeg_converter
from dpc_client_core.music.schemas import SchemaError
from tests._acestep_helpers import Rig, free_env, from_text, install_fake_engine
from tests._music_job_commands_helpers import review_job, service_with_engine
from tests._music_jobs_helpers import job_in_review, store_at

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
DONE = (js.REVIEW, js.FAILED, js.CANCELLED)

SUMMARY = """[Parsed_ebur128_0 @ 0000021f] t: 2.9      TARGET:-23 LUFS    M: -9.8 S: -9.8     I: -9.8 LUFS
[Parsed_ebur128_0 @ 0000021f] Summary:

  Integrated loudness:
    I:          -9.8 LUFS
    Threshold: -19.8 LUFS

  Loudness range:
    LRA:         4.1 LU
    Threshold: -29.9 LUFS
    LRA low:   -12.0 LUFS
    LRA high:   -8.1 LUFS

  True peak:
    Peak:       -0.9 dBFS
"""


def sine_wav(path: Path, seconds=3.0, freq=1000.0, amplitude=0.3, rate=48000):
    frames = bytearray()
    for i in range(int(seconds * rate)):
        v = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<hh", v, v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


# -- parsing ------------------------------------------------------------------------


def test_the_summary_is_read_after_the_last_summary_marker():
    values = parse_ebur128_summary(SUMMARY)
    assert values == {"lufs_i": -9.8, "lra": 4.1, "true_peak_dbtp": -0.9}


def test_a_silent_file_has_a_floor_loudness_and_no_peak():
    text = "Summary:\n  Integrated loudness:\n    I: -70.0 LUFS\n  Loudness range:\n    LRA: 0.0 LU\n  True peak:\n    Peak: -inf dBFS\n"
    assert parse_ebur128_summary(text) == {"lufs_i": -70.0, "lra": 0.0, "true_peak_dbtp": None}


def test_no_summary_or_no_integrated_loudness_is_an_error():
    with pytest.raises(LoudnessError):
        parse_ebur128_summary("ffmpeg: no audio stream")
    with pytest.raises(LoudnessError):
        parse_ebur128_summary("Summary:\n  LRA: 1.0 LU\n")


# -- the ffmpeg command ---------------------------------------------------------------


class _Result:
    def __init__(self, returncode, stderr):
        self.returncode = returncode
        self.stderr = stderr.encode("utf-8")


def test_the_meter_asks_ebur128_for_the_true_peak_and_quiet_frame_logs(tmp_path):
    f = sine_wav(tmp_path / "a.wav", seconds=0.1)
    calls = []

    def runner(cmd, *, timeout):
        calls.append(cmd)
        return _Result(0, SUMMARY)
    rec = measure_loudness(f, MediaTools(ffmpeg="ffmpeg", runner=runner))
    assert calls[0][calls[0].index("-af") + 1] == "ebur128=peak=true:framelog=verbose"
    assert "-nostdin" in calls[0] and calls[0][-3:] == ["-f", "null", "-"]
    assert rec["lufs_i"] == -9.8 and rec["true_peak_dbtp"] == -0.9 and rec["lra"] == 4.1
    assert rec["measured_by"] == MEASURED_BY and rec["measured_at"].endswith("Z")
    js.check_loudness(rec)


def test_an_ffmpeg_without_framelog_is_asked_again_without_it(tmp_path):
    f = sine_wav(tmp_path / "a.wav", seconds=0.1)
    calls = []

    def runner(cmd, *, timeout):
        calls.append(cmd[cmd.index("-af") + 1])
        if "framelog" in calls[-1]:
            return _Result(1, "Option 'framelog' not found")
        return _Result(0, SUMMARY)
    assert measure_loudness(f, MediaTools(ffmpeg="ffmpeg", runner=runner))["lufs_i"] == -9.8
    assert calls == ["ebur128=peak=true:framelog=verbose", "ebur128=peak=true"]


def test_no_ffmpeg_or_no_file_is_a_loudness_error(tmp_path):
    f = sine_wav(tmp_path / "a.wav", seconds=0.1)
    with pytest.raises(LoudnessError, match="ffmpeg is not on PATH"):
        measure_loudness(f, MediaTools(ffmpeg=None))
    with pytest.raises(LoudnessError, match="not there"):
        measure_loudness(tmp_path / "gone.flac", MediaTools(ffmpeg="ffmpeg"))


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
def test_the_real_ffmpeg_measures_a_sine_where_bs1770_puts_it(tmp_path):
    # A 1 kHz sine at 0.3 of full scale on both channels: peak -10.46 dBFS; BS.1770 gives
    # about -9.8 LUFS (-10.46 - 3.01 per channel, +0.7 K-weighting at 1 kHz, +3.01 for two).
    rec = measure_loudness(sine_wav(tmp_path / "sine.wav"))
    assert -11.0 < rec["lufs_i"] < -8.5, rec
    assert -11.5 < rec["true_peak_dbtp"] < -9.5, rec
    assert isinstance(rec["lra"], float)  # 3 s is one short-term window: the LRA itself says little here
    assert rec["measured_by"] == "ffmpeg ebur128"


# -- the record on the take -----------------------------------------------------------


def test_a_loudness_record_has_exactly_its_keys():
    good = {"lufs_i": -14.0, "lra": None, "true_peak_dbtp": None, "measured_by": MEASURED_BY,
            "measured_at": "2026-11-21T20:00:00Z"}
    js.check_loudness(good)
    for bad in ({**good, "extra": 1}, {k: v for k, v in good.items() if k != "lra"},
                {**good, "lufs_i": None}, {**good, "lufs_i": float("nan")}, {**good, "measured_by": ""}):
        with pytest.raises(SchemaError):
            js.check_loudness(bad)


def test_the_store_records_a_measurement_and_keeps_it_across_a_restart(tmp_path):
    home = tmp_path / "dpc"
    store = store_at(home)
    job, takes = job_in_review(store)
    assert takes[0]["loudness"] is None
    rec = {"lufs_i": -13.1, "lra": 5.0, "true_peak_dbtp": -0.4, "measured_by": MEASURED_BY,
           "measured_at": "2026-11-21T20:00:00Z"}
    store.set_take_loudness(takes[0]["id"], rec)
    store.keep_take(takes[1]["id"])
    store.set_take_loudness(takes[1]["id"], rec)  # a measurement, not a decision: allowed after Keep
    again = store_at(home)
    assert again.get_take(takes[0]["id"])["loudness"] == rec
    assert again.get_take(takes[1]["id"])["loudness"] == rec
    from dpc_client_core.music.jobs import MusicJobError
    with pytest.raises(MusicJobError) as err:
        again.set_take_loudness(takes[0]["id"], {"lufs_i": "loud"})
    assert err.value.field == "loudness"


# -- the runner ---------------------------------------------------------------------


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "dpc"
    install_fake_engine(h)
    return h


@pytest.mark.asyncio
async def test_the_runner_stores_each_takes_loudness(home, monkeypatch):
    free_env(monkeypatch)
    measured = []

    def meter(flac):
        measured.append(flac.name.removesuffix(".part"))
        # The staged FLAC: measured before the file is final so the provenance record, written into
        # the file once, can hold the loudness (PROVENANCE §2 phenotype).
        assert flac.is_file() and flac.name.endswith(".flac.part")
        return {"lufs_i": -12.0 - len(measured), "lra": 2.0, "true_peak_dbtp": -1.0,
                "measured_by": MEASURED_BY, "measured_at": "2026-11-21T20:00:00Z"}
    async with Rig(home, loudness_meter=meter) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=2))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        takes = rig.store.list_takes(job["id"])
        assert measured == ["indie-rock-warm-guitars_v1.flac", "indie-rock-warm-guitars_v2.flac"]
        assert [t["loudness"]["lufs_i"] for t in takes] == [-13.0, -14.0]


@pytest.mark.asyncio
async def test_a_failed_measurement_leaves_loudness_null_and_the_job_goes_to_review(home, monkeypatch):
    free_env(monkeypatch)

    def broken(flac):
        raise LoudnessError("ffmpeg printed no ebur128 summary")

    def crashing(flac):
        raise RuntimeError("boom")
    for meter in (broken, crashing):
        async with Rig(home, loudness_meter=meter) as rig:
            job = rig.store.create_job("generate_from_text", from_text(variants=1))
            rig.runner.wake()
            job = await rig.wait_for(job["id"], DONE)
            assert job["state"] == js.REVIEW, job["state_reason"]
            assert rig.store.list_takes(job["id"])[0]["loudness"] is None


@pytest.mark.asyncio
@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
async def test_the_real_ffmpeg_measures_the_real_flac(home, monkeypatch):
    free_env(monkeypatch)
    async with Rig(home, converter=ffmpeg_converter(), loudness_meter=ffmpeg_loudness_meter()) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        loud = rig.store.list_takes(job["id"])[0]["loudness"]
        # The fake engine's audio is 50 ms of a 0.3 sine: shorter than one gating block,
        # so ebur128 reports its -70 LUFS floor; the true peak is the sine's.
        assert loud["measured_by"] == "ffmpeg ebur128"
        assert loud["lufs_i"] == -70.0
        assert -11.5 < loud["true_peak_dbtp"] < -9.5


# -- the command --------------------------------------------------------------------


class _LocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _review_with_files(service, tmp_path, names=("v1.wav", "v2.wav")):
    store = service._music_jobs_instance
    job, takes = review_job(store, [None] * len(names))
    files = []
    for take, name in zip(takes, names):
        path = sine_wav(tmp_path / name, seconds=0.2)
        files.append(path)
        take_rec = store._take(take["id"])[1]
        take_rec["file"]["uri"] = path.as_uri()
    return job, takes, files


@pytest.mark.asyncio
async def test_the_command_measures_once_and_again_only_when_forced(tmp_path, monkeypatch):
    service = service_with_engine(tmp_path / "dpc")
    _, takes, files = _review_with_files(service, tmp_path)
    seen = []

    def fake_measure(path):
        seen.append(Path(path))
        return {"lufs_i": -15.5, "lra": 1.0, "true_peak_dbtp": -2.0, "measured_by": MEASURED_BY,
                "measured_at": "2026-11-21T20:00:00Z"}
    monkeypatch.setattr(commands, "measure_loudness", fake_measure)
    first = await service.measure_music_take_loudness(take_id=takes[0]["id"])
    assert first["status"] == "ok" and first["measured"] is True
    assert first["take"]["loudness"]["lufs_i"] == -15.5
    assert seen == [files[0]]
    second = await service.measure_music_take_loudness(take_id=takes[0]["id"])
    assert second["measured"] is False and len(seen) == 1
    forced = await service.measure_music_take_loudness(take_id=takes[0]["id"], force=True)
    assert forced["measured"] is True and len(seen) == 2
    assert service._music_jobs_instance.get_take(takes[0]["id"])["loudness"]["lufs_i"] == -15.5


@pytest.mark.asyncio
async def test_the_command_maps_its_errors(tmp_path):
    service = service_with_engine(tmp_path / "dpc")
    _, takes, files = _review_with_files(service, tmp_path)
    assert (await service.measure_music_take_loudness(take_id="tak_0000000000000000"))["code"] == "not_found"
    assert (await service.measure_music_take_loudness())["code"] == "invalid_take_id"
    files[1].unlink()
    gone = await service.measure_music_take_loudness(take_id=takes[1]["id"])
    assert gone["status"] == "error" and gone["code"] == "measure_failed" and "not there" in gone["message"]


@pytest.mark.asyncio
@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is not installed")
async def test_the_command_measures_with_the_real_ffmpeg(tmp_path):
    service = service_with_engine(tmp_path / "dpc")
    _, takes, _ = _review_with_files(service, tmp_path, names=("long.wav",))
    store = service._music_jobs_instance
    sine_wav(Path(commands.path_of_file_uri(store.get_take(takes[0]["id"])["file"]["uri"])), seconds=2.0)
    answer = await service.measure_music_take_loudness(take_id=takes[0]["id"])
    assert answer["status"] == "ok", answer
    assert -11.0 < answer["take"]["loudness"]["lufs_i"] < -8.5


def test_a_file_uri_becomes_its_local_path(tmp_path):
    p = tmp_path / "a b" / "é.flac"
    assert commands.path_of_file_uri(p.as_uri()) == p
    assert commands.path_of_file_uri("http://127.0.0.1/x") is None
    assert commands.path_of_file_uri(None) is None


@pytest.mark.asyncio
async def test_the_persons_job_changes_are_broadcast(tmp_path):
    service = service_with_engine(tmp_path / "dpc")
    service.local_api = _LocalApi()
    store = service._music_jobs_instance
    job, takes = review_job(store, [None, None])
    await service.keep_music_take(take_id=takes[0]["id"])
    await service.discard_music_take(take_id=takes[1]["id"], reason_text="muddy mix")
    queued = await service.propose_music_job(type="generate_from_text",
                                             params={"lyrics": None, "style": "ambient pads"})
    await service.cancel_music_job(job_id=queued["job"]["id"])
    other, _ = review_job(store, [None])
    await service.reject_all_music_takes(job_id=other["id"], reason_class="sel")
    await asyncio.sleep(0)
    names = [n for n, _ in service.local_api.events]
    assert names == ["music_job_updated"] * 5
    states = [p["job"]["state"] for _, p in service.local_api.events]
    assert states == ["accepted", "accepted", "queued", "cancelled", "rejected"]
