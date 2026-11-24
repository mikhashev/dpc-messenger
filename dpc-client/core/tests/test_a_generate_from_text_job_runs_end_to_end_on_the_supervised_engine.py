"""SPEC §5-§6, §12 decisions 32-35 (ADR-051 D30-D32): the runner takes a queued
`generate_from_text` job through the acestep.cpp server to `review`.

Each request body is on disk as a payload before it is sent, and the LM's answer (the
codes) is kept; one `/synth` carries every variant (`synth_batch_size` = variants), and
each take records its seed, batch size and batch index; the takes are FLAC files in the
library, with a sidecar, and no `.part` or `.wav` is left beside them. A DPC engine on the
card keeps the job queued and named, and nothing is stopped; Cancel stops the engine's job;
a failed synth fails the job with the reason; a restart resumes queued jobs with their seed.
The server is `_fake_acestep_server.py` (no GPU); FLAC is written by `fake_flac`, and by the
real ffmpeg in the last test when ffmpeg is installed.
"""

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.runner import LM_PAYLOAD, LM_RESULT_PAYLOAD, SYNTH_PAYLOAD, ffmpeg_converter
from tests._acestep_helpers import Rig, files_in, free_env, from_text, install_fake_engine, wait_until


class FakeSup:
    """A supervisor child on the card, as `_CARD_CLAIMS` holds one (ADR-052, Confirmation test 13):
    running and idle; the swap is off in these tests (no settings bound), so nothing stops it."""

    EXCLUSIVE_ON_THE_CARD = True
    DROPS_PARKED_ON_STOP = False

    def __init__(self, alias, engine):
        self.alias, self.ENGINE = alias, engine
        self._proc = type("Proc", (), {"returncode": None, "pid": 4242})()
        self.props = {"total_slots": 1}
        self._draining = False
        self._swap_for = None
        self.port = 51234

    def _busy_for_the_card(self, state, but_for=None):
        return False


def on_the_card(alias, engine):
    sup = FakeSup(alias, engine)
    base_mod._CARD_CLAIMS[sup] = "claimed"
    return sup

DONE = (js.REVIEW, js.FAILED, js.CANCELLED)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "dpc"
    install_fake_engine(h)
    return h


@pytest.mark.asyncio
async def test_a_job_yields_one_flac_take_per_variant_in_the_library(home, monkeypatch, tmp_path):
    free_env(monkeypatch, record=tmp_path / "sent")
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=2))
        assert job["state"] == js.QUEUED
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        assert job["progress"] == 1
        takes = rig.store.list_takes(job["id"])
        assert [t["variant"] for t in takes] == [1, 2]
        assert [t["synth_batch_index"] for t in takes] == [0, 1]
        assert {t["synth_batch_size"] for t in takes} == {2}
        assert {t["seed"] for t in takes} == {42}
        assert {t["payload"] for t in takes} == {SYNTH_PAYLOAD}

        out = home / "music" / "generated" / job["id"]
        assert files_in(out) == ["indie-rock-warm-guitars_v1.flac", "indie-rock-warm-guitars_v1.json",
                                 "indie-rock-warm-guitars_v2.flac", "indie-rock-warm-guitars_v2.json"]
        entries = {e["id"]: e for e in rig.library.list_entries()}
        assert len(entries) == 2
        for take in takes:
            entry = entries[take["library_track_id"]]
            assert entry["origin"] == "generated"
            assert entry["path"].endswith(f"_v{take['variant']}.flac")
            assert Path(entry["path"]).as_uri() == take["file"]["uri"]
            assert entry["source"]["lineage"]["job_id"] == job["id"]
            assert take["file"]["content_hash"] == entry["content_hash"]
            assert take["file"]["sample_rate"] == 48000 and take["file"]["channels"] == 2

        side = json.loads((out / "indie-rock-warm-guitars_v2.json").read_text(encoding="utf-8"))
        assert side["schema"] == "mp.provenance/1"  # the same record the FLAC carries (PROVENANCE §3)
        assert side["take_id"] == takes[1]["id"] and side["library_track_id"] == takes[1]["library_track_id"]
        assert (side["seed"], side["variant_seed"], side["synth_batch_size"], side["synth_batch_index"]) == (42, 43, 2, 1)
        assert set(side["payloads"]) == {LM_PAYLOAD, LM_RESULT_PAYLOAD, SYNTH_PAYLOAD}
        assert side["payloads"][SYNTH_PAYLOAD] == next(p["sha256"] for p in job["payloads"] if p["name"] == SYNTH_PAYLOAD)
        assert side["codes_count"] == 150 and side["lm_seed"] == 876362762
        assert side["engine"] == "acestep.cpp" and side["engine_version"] == "fake0000 (2026-11-21)"
        dit = next(m for m in side["models"] if m["role"] == "dit")
        assert dit["file"] == "acestep-v15-turbo-Q8_0.gguf" and dit["sha256"].startswith("sha256:")
        assert side["engine_output"]["pcm_md5"] == side["output"]["pcm_md5"]
        assert side["engine_output"]["bits"] == 24 and side["output"]["bits"] == 24
        assert side["engine_output"]["kept"] is False
        assert str(home) not in json.dumps(side)  # no local paths (PROVENANCE rule 3)

        # The bytes the engine received are the payloads, byte for byte.
        sent = sorted((tmp_path / "sent").iterdir())
        assert [p.name.split("-", 1)[1] for p in sent] == ["lm.json", "synth.json"]
        assert sent[0].read_bytes() == rig.store.read_payload(job["id"], LM_PAYLOAD)
        assert sent[1].read_bytes() == rig.store.read_payload(job["id"], SYNTH_PAYLOAD)
        lm_req = json.loads(sent[0].read_bytes())
        assert lm_req["caption"] == "indie rock, warm guitars" and lm_req["lyrics"].startswith("[verse]\nТысячи")
        assert (lm_req["duration"], lm_req["seed"], lm_req["inference_steps"], lm_req["output_format"],
                lm_req["synth_batch_size"]) == (30, 42, 8, "wav24", 2)
        assert lm_req["synth_model"] == "acestep-v15-turbo-Q8_0.gguf"
        synth = json.loads(sent[1].read_bytes())
        assert synth["audio_codes"].startswith("1000,1001")
        assert (synth["synth_batch_size"], synth["seed"], synth["inference_steps"], synth["output_format"]) == (2, 42, 8, "wav24")
        lm_result = json.loads(rig.store.read_payload(job["id"], LM_RESULT_PAYLOAD))
        assert lm_result[0]["audio_codes"] == synth["audio_codes"]

        assert [e["state"] for e in rig.events if e["id"] == job["id"]][-1] == js.REVIEW
        assert rig.engine.state == "running"  # left for the idle timer


@pytest.mark.asyncio
async def test_a_folder_scan_of_the_generated_folder_never_sees_a_part_or_a_wav(home, monkeypatch):
    free_env(monkeypatch)
    async with Rig(home) as rig:
        generated = home / "music" / "generated"
        generated.mkdir(parents=True, exist_ok=True)
        rig.library.poll_interval_s = 0.01
        await rig.library.add_folder(str(generated), watch=True)
        await rig.library.start()
        seen = set()
        job = rig.store.create_job("generate_from_text", from_text(variants=2))
        rig.runner.wake()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 60
        while rig.store.get_job(job["id"])["state"] not in DONE and loop.time() < deadline:
            seen.update(Path(e["path"]).name for e in rig.library.list_entries() if e.get("path"))
            await asyncio.sleep(0.005)
        assert rig.store.get_job(job["id"])["state"] == js.REVIEW
        await rig.library.rescan_all()
        seen.update(Path(e["path"]).name for e in rig.library.list_entries() if e.get("path"))
        assert seen == {"indie-rock-warm-guitars_v1.flac", "indie-rock-warm-guitars_v2.flac"}
        assert len(rig.library.list_entries()) == 2  # one entry per take, whoever saw it first


@pytest.mark.asyncio
async def test_each_body_is_a_payload_on_disk_before_it_is_sent(home, monkeypatch):
    free_env(monkeypatch)
    async with Rig(home, start_runner=False) as rig:
        seen = []
        real_submit = rig.engine.submit

        async def watching_submit(path, body):
            job = rig.store.list_jobs(states=[js.RUNNING])[0]
            name = LM_PAYLOAD if path == "/lm" else SYNTH_PAYLOAD
            on_disk = rig.store.payload_path(job["id"], name)
            seen.append((path, on_disk.is_file() and on_disk.read_bytes() == body,
                         [p["name"] for p in job["payloads"]]))
            return await real_submit(path, body)
        rig.engine.submit = watching_submit
        job = rig.store.create_job("generate_from_text", from_text(variants=1, seed=None))
        rig.runner.start()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        assert seen == [("/lm", True, [LM_PAYLOAD]),
                        ("/synth", True, [LM_PAYLOAD, LM_RESULT_PAYLOAD, SYNTH_PAYLOAD])]
        drawn = json.loads(rig.store.read_payload(job["id"], LM_PAYLOAD))["seed"]
        assert isinstance(drawn, int) and 0 <= drawn <= js.SEED_MAX
        assert rig.store.list_takes(job["id"])[0]["seed"] == drawn


@pytest.mark.asyncio
async def test_a_dpc_engine_on_the_card_keeps_the_job_queued_and_nothing_is_stopped(home, monkeypatch):
    free_env(monkeypatch)
    stops = []
    import dpc_client_core.providers.llamacpp_server_provider as provider
    monkeypatch.setattr(provider, "stop_local_engine", lambda *a, **k: stops.append(a))
    holder = on_the_card("qwen-27b", "strata")
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text())
        rig.runner.wake()
        await wait_until(lambda: rig.store.get_job(job["id"]).get("card_wait") is not None)
        waiting = rig.store.get_job(job["id"])
        assert waiting["state"] == js.QUEUED
        assert waiting["state_reason"] == "waiting for the card: qwen-27b holds it"
        assert waiting["card_wait"]["holders"] == [{"alias": "qwen-27b", "engine": "strata"}]
        assert [p["name"] for p in waiting["payloads"]] == [LM_PAYLOAD]  # visible while it waits
        await asyncio.sleep(0.3)
        assert rig.store.get_job(job["id"])["state"] == js.QUEUED
        assert rig.engine.state == "stopped" and not rig.engine.alive()  # never started while held
        assert stops == []

        base_mod._CARD_CLAIMS.pop(holder)  # the person pressed "Stop qwen-27b"
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        assert job["card_wait"] is None
        assert stops == []


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_the_card(home, monkeypatch):
    free_env(monkeypatch)
    on_the_card("llama", "llamacpp_server")
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text())
        rig.runner.wake()
        await wait_until(lambda: rig.store.get_job(job["id"]).get("card_wait") is not None)
        rig.store.cancel_job(job["id"])
        rig.runner.wake()
        await wait_until(lambda: rig.runner.current_job_id is None)
        assert rig.store.get_job(job["id"])["state"] == js.CANCELLED
        assert not rig.engine.alive()
        assert rig.engine.card_lease._waiters() == [], "the cancelled wait left the lease line"


@pytest.mark.asyncio
async def test_cancel_mid_run_cancels_the_engine_job_and_keeps_the_state(home, monkeypatch):
    free_env(monkeypatch, "slow_synth")
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text())
        rig.runner.wake()
        await wait_until(lambda: any(p["name"] == SYNTH_PAYLOAD for p in rig.store.get_job(job["id"])["payloads"]))
        await asyncio.sleep(0.2)  # the synth job is running on the engine
        rig.store.cancel_job(job["id"])
        await wait_until(lambda: rig.runner.current_job_id is None)
        job = rig.store.get_job(job["id"])
        assert job["state"] == js.CANCELLED
        assert rig.store.list_takes(job["id"]) == []
        await wait_until(lambda: "cancel requested" in rig.engine.tail_log())
        assert files_in(home / "music" / "generated" / job["id"]) == []  # the partial answer is gone

        nxt = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        await wait_until(lambda: rig.store.get_job(nxt["id"])["state"] == js.RUNNING)  # the runner moved on


@pytest.mark.asyncio
async def test_a_failed_synth_fails_the_job_with_the_reason(home, monkeypatch):
    free_env(monkeypatch, "fail_synth")
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text())
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.FAILED
        assert "/synth" in job["state_reason"] and "failed" in job["state_reason"]
        assert "synth failed on purpose" in job["state_reason"]  # the engine's log tail
        assert rig.library.list_entries() == []


@pytest.mark.asyncio
async def test_a_short_batch_fails_rather_than_guess_batch_indexes(home, monkeypatch):
    free_env(monkeypatch, "short_batch")
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=3))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.FAILED
        assert "2 audio part(s) for synth_batch_size 3" in job["state_reason"]


@pytest.mark.asyncio
async def test_an_engine_that_dies_mid_job_fails_it_and_the_next_job_restarts_it(home, monkeypatch):
    free_env(monkeypatch, "crash_on_lm")
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text())
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.FAILED
        assert "acestep.cpp" in job["state_reason"]
        await wait_until(lambda: rig.engine.state == "error")

        monkeypatch.setenv("FAKE_ACESTEP_MODE", "")
        retry = rig.store.create_job("generate_from_text", from_text(variants=1))
        assert retry["state"] == js.UNSUPPORTED  # proposals see the error
        await rig.engine.start()  # what start_music_engine does
        again = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        again = await rig.wait_for(again["id"], DONE)
        assert again["state"] == js.REVIEW, again["state_reason"]


@pytest.mark.asyncio
async def test_a_restart_resumes_queued_jobs_with_the_seed_already_written(home, monkeypatch):
    free_env(monkeypatch)
    strata = on_the_card("strata-a", "strata")
    async with Rig(home) as first:
        job = first.store.create_job("generate_from_text", from_text(seed=None))
        first.runner.wake()
        await wait_until(lambda: first.store.get_job(job["id"]).get("card_wait") is not None)
        seed = json.loads(first.store.read_payload(job["id"], LM_PAYLOAD))["seed"]
    base_mod._CARD_CLAIMS.pop(strata)
    # The backend stops while the job waits; the next start finds it queued.
    async with Rig(home) as second:
        resumed = second.store.get_job(job["id"])
        assert resumed["state"] == js.QUEUED and resumed["card_wait"] is None
        resumed = await second.wait_for(job["id"], DONE)
        assert resumed["state"] == js.REVIEW, resumed["state_reason"]
        assert {t["seed"] for t in second.store.list_takes(job["id"])} == {seed}
        assert [p["name"] for p in resumed["payloads"]].count(LM_PAYLOAD) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
async def test_the_real_ffmpeg_writes_a_lossless_24_bit_flac(home, monkeypatch):
    from dpc_client_core.music.identity import read_flac_streaminfo
    free_env(monkeypatch)
    async with Rig(home, converter=ffmpeg_converter()) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        take = rig.store.list_takes(job["id"])[0]
        flac = Path(rig.library.get_entry(take["library_track_id"])["path"])
        info = read_flac_streaminfo(flac)
        assert info.bits_per_sample == 24 and info.sample_rate == 48000 and info.channels == 2
        side = json.loads(flac.with_suffix(".json").read_text(encoding="utf-8"))
        assert side["output"]["pcm_md5"] == side["engine_output"]["pcm_md5"] == info.md5
        assert files_in(flac.parent) == [flac.name, flac.with_suffix(".json").name]
