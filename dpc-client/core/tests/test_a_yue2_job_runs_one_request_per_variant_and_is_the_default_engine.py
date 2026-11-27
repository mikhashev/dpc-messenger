"""SPEC §12 decision 40 (Mike #198, 2026-11-21): YuE2 via yue2.cpp is the default engine.

A `generate_from_text` job without `engine` runs on yue2.cpp. Each variant is one request at
batch 1 with seed and lm_seed `seed + i`, written as a payload before it is sent; each take
records its own seed, lm_seed, batch 1 and the engine; the sidecar names the commit, the
backbone and the adapters with their split halves' sha256s and strengths. Voice adapters are
sent as their two split halves at music and sound strengths; an adapter that is not there is
refused at propose time. Cancel stops the job in flight. Jobs written before YuE2 (the
legacy engine constant, or no engine) still load and mean acestep.
"""

import asyncio
import json
from pathlib import Path

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.jobs import MusicJobError, MusicJobStore
from dpc_client_core.music.capabilities import MusicCapabilities
from dpc_client_core.music.runner import build_yue2_body, variant_seed
from tests._acestep_helpers import fake_flac, wait_until
from tests._music_jobs_helpers import installed_probe
from tests._yue2_helpers import VOICES, DualRig, install_both, yue2_env, yue2_params


def test_yue2_is_the_default_engine_with_its_own_limits():
    p = js.normalize_params_from_text({"lyrics": "x", "style": "y"})
    assert p["engine"] == "yue2" == js.DEFAULT_FROM_TEXT_ENGINE
    assert p["duration_s"] == 180 and p["backbone"] == "stock" and p["cot"] == "full" and p["adapters"] == []
    for bad in (29, 361):
        with pytest.raises(js.ParamsError) as err:
            js.normalize_params_from_text({"lyrics": "x", "style": "y", "duration_s": bad})
        assert err.value.field == "duration_s"
    a = js.normalize_params_from_text({"lyrics": "x", "style": "y", "engine": "acestep"})
    assert a["duration_s"] == 120 and "backbone" not in a and "adapters" not in a
    assert js.normalize_params_from_text({"lyrics": "x", "style": "y", "engine": "acestep", "duration_s": 10})
    with pytest.raises(js.ParamsError) as err:
        js.normalize_params_from_text({"lyrics": "x", "style": "y", "engine": "acestep", "cot": "off"})
    assert err.value.field == "cot"
    with pytest.raises(js.ParamsError):
        js.normalize_params_from_text({"lyrics": "x", "style": "y", "engine": "suno"})
    adapters = js.normalize_params_from_text(
        {"lyrics": "x", "style": "y", "adapters": [{"name": "v1"}, {"name": "v2", "scale": 0.3}]})["adapters"]
    assert adapters == [{"name": "v1", "music": 0.45, "sound": 0.35}, {"name": "v2", "music": 0.3, "sound": 0.3}]
    for bad in ([{"name": "../x"}], [{"name": "a"}, {"name": "a"}], [{"name": "a", "music": 3}],
                [{"name": "a", "scale": 1, "music": 1}]):
        with pytest.raises(js.ParamsError) as err:
            js.normalize_params_from_text({"lyrics": "x", "style": "y", "adapters": bad})
        assert err.value.field == "adapters"


def test_the_short_cap_estimate_counts_sung_lines_and_the_intro():
    lyrics = "[verse]\none\ntwo\n\n[chorus]\nthree\nfour\nfive\nsix\n"
    assert js.estimate_yue2_seconds(lyrics) == js.YUE2_INTRO_S + 6 * js.YUE2_SECONDS_PER_LINE == 80
    assert js.estimate_yue2_seconds(None) == js.YUE2_INTRO_S


def test_old_jobs_with_the_legacy_engine_or_none_still_load_as_acestep(tmp_path):
    store = MusicJobStore(tmp_path, capabilities=MusicCapabilities(installed_probe))
    store.open()
    legacy = store.create_job("generate_from_text", {"lyrics": "x", "style": "y", "engine": js.LEGACY_ACESTEP_ENGINE})
    assert legacy["params"]["engine"] == js.LEGACY_ACESTEP_ENGINE and js.engine_of(legacy["params"]) == "acestep"
    # A record from before `engine` was written at all.
    old = dict(legacy, id="job_00000000000000aa")
    old["params"] = {k: v for k, v in legacy["params"].items() if k != "engine"}
    folder = store.root / old["id"]
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({"job": old, "takes": []}), encoding="utf-8")
    fresh = MusicJobStore(tmp_path, capabilities=MusicCapabilities(installed_probe))
    fresh.open()
    assert fresh.get_job(legacy["id"])["params"]["engine"] == js.LEGACY_ACESTEP_ENGINE
    loaded = fresh.get_job(old["id"])
    assert "engine" not in loaded["params"] and js.engine_of(loaded["params"]) == "acestep"
    assert not list(folder.glob("job.json.rejected-*"))


def test_a_variant_body_is_batch_one_with_both_seeds():
    params = js.normalize_params_from_text(yue2_params())
    body = json.loads(build_yue2_body(params, variant_seed(42, 1)))
    assert body["seed"] == body["lm_seed"] == 43
    assert body["lm_batch_size"] == body["synth_batch_size"] == 1
    assert body["output_format"] == "wav24" and body["cot"] == "full" and body["duration"] == 60.0
    assert "adapters" not in body
    assert variant_seed(js.SEED_MAX, 1) == 0


@pytest.mark.asyncio
async def test_a_default_job_runs_on_yue2_one_request_per_variant_and_records_each_take(tmp_path, monkeypatch):
    record = tmp_path / "bodies"
    yue2_env(monkeypatch, record=record)
    home = tmp_path / "dpc"
    install_both(home)
    fake_flac.calls.clear()
    async with DualRig(home) as rig:
        job = rig.propose(yue2_params())
        assert job["params"]["engine"] == "yue2" and job["state"] == "queued"
        job = await rig.wait_for(job["id"], ("review", "failed"))
        assert job["state"] == "review", job["state_reason"]
        assert not rig.ace.alive()

        names = [p["name"] for p in job["payloads"]]
        assert names[:2] == ["synth.v1.request.json", "synth.v2.request.json"]
        assert {"synth.v1.result.json", "synth.v2.result.json"} <= set(names)
        sent = sorted(record.glob("*-synth.json"))
        assert len(sent) == 2
        for v, path in enumerate(sent, start=1):
            assert path.read_bytes() == rig.store.read_payload(job["id"], f"synth.v{v}.request.json")
            body = json.loads(path.read_bytes())
            assert body["seed"] == body["lm_seed"] == 41 + v
            assert body["lm_batch_size"] == body["synth_batch_size"] == 1

        takes = rig.store.list_takes(job["id"])
        assert [(t["variant"], t["seed"], t["lm_seed"], t["synth_batch_size"], t["synth_batch_index"], t["engine"],
                 t["payload"]) for t in takes] == [
            (1, 42, 42, 1, 0, "yue2.cpp", "synth.v1.request.json"),
            (2, 43, 43, 1, 0, "yue2.cpp", "synth.v2.request.json")]
        assert all(t["library_track_id"] and t["duration_requested_s"] == 60 for t in takes)
        assert len(rig.library.list_entries()) == 2

        out = home / "music" / "generated" / job["id"]
        sidecar = json.loads((out / "indie-rock-male-vocal_v2.json").read_text(encoding="utf-8"))
        assert sidecar["schema"] == "mp.provenance/1"
        assert sidecar["engine"] == "yue2.cpp" and sidecar["engine_commit"] == "e646d52"
        backbone = next(m for m in sidecar["models"] if m["role"] == "backbone")
        assert backbone["choice"] == "stock" and backbone["sha256"].startswith("sha256:")
        assert sidecar["backbone"] == "stock" and sidecar["adapters"] == []
        assert (sidecar["seed"], sidecar["variant_seed"], sidecar["lm_seed"]) == (42, 43, 43)
        assert sidecar["synth_batch_size"] == 1 and sidecar["lm_batch_size"] == 1
        assert sidecar["cot"] == "full" and sidecar["duration_requested_s"] == 60
        assert sidecar["duration_received_s"] > 0 and sidecar["semantic_tokens_count"] == 25
        assert sidecar["take_id"] == takes[1]["id"] and str(tmp_path) not in json.dumps(sidecar)
        assert fake_flac.calls[-1]["DPC_ENGINE"] == "yue2.cpp" and fake_flac.calls[-1]["DPC_LM_SEED"] == "43"
        assert sorted(p.name for p in out.iterdir() if p.suffix == ".part") == []


@pytest.mark.asyncio
async def test_voice_adapters_are_sent_as_two_split_halves_and_recorded(tmp_path, monkeypatch):
    record = tmp_path / "bodies"
    yue2_env(monkeypatch, record=record)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home) as rig:
        job = rig.propose(yue2_params(
            variants=1, adapters=[{"name": VOICES[1], "music": 0.45, "sound": 0.35}]))
        job = await rig.wait_for(job["id"], ("review", "failed"))
        assert job["state"] == "review", job["state_reason"]
        (path,) = record.glob("*-synth.json")
        assert json.loads(path.read_bytes())["adapters"] == [
            {"name": f"{VOICES[1]}-music.safetensors", "scale": 0.45},
            {"name": f"{VOICES[1]}-sound.safetensors", "scale": 0.35}]
        out = home / "music" / "generated" / job["id"]
        sidecar = json.loads(next(out.glob("*_v1.json")).read_text(encoding="utf-8"))
        (a,) = sidecar["adapters"]
        assert a["name"] == VOICES[1] and a["source"]["sha256"].startswith("sha256:")
        assert a["music"]["scale"] == 0.45 and a["sound"]["scale"] == 0.35
        assert a["music"]["sha256"] != a["sound"]["sha256"]


def test_a_missing_adapter_or_backbone_is_refused_at_propose_time(tmp_path):
    home = tmp_path / "dpc"
    install_both(home)
    rig = DualRig(home, start_runner=False)
    with pytest.raises(MusicJobError) as err:
        rig.store.create_job("generate_from_text", yue2_params(adapters=[{"name": "voice-nobody"}]))
    assert err.value.code == "invalid_params" and err.value.field == "adapters"
    with pytest.raises(MusicJobError) as err:
        rig.store.create_job("generate_from_text", yue2_params(backbone="rs-tw"))
    assert err.value.field == "backbone" and "rs-tw" in err.value.message


def test_a_job_on_an_engine_that_is_not_installed_is_unsupported_and_the_other_still_queues(tmp_path):
    home = tmp_path / "dpc"
    from tests._acestep_helpers import install_fake_engine
    install_fake_engine(home)  # acestep only
    rig = DualRig(home, start_runner=False)
    yue = rig.propose(yue2_params())
    assert yue["state"] == "unsupported" and "yue2.cpp server binary is missing" in yue["state_reason"]
    ace = rig.propose(yue2_params(engine="acestep", duration_s=30))
    assert ace["state"] == "queued"
    report = rig.store.capabilities.report()
    assert report["configured_default_engine"] == "yue2"
    assert report["default_engine"] == "acestep" and report["engine"]["key"] == "acestep"
    assert report["default_engine_fallback_reason"] and "yue2" in report["default_engine_fallback_reason"]
    assert report["engines"]["acestep"]["available"] and not report["engines"]["yue2"]["available"]
    assert report["job_types"]["generate_from_text"]["status"] == "supported"
    assert report["limits"]["generate_from_text"]["engines"]["yue2"]["duration_s"] == {"min": 30, "max": 360,
                                                                                       "default": 180}


@pytest.mark.asyncio
async def test_cancel_stops_the_variant_in_flight(tmp_path, monkeypatch):
    record = tmp_path / "bodies"
    yue2_env(monkeypatch, "slow_synth", record=record)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home) as rig:
        job = rig.propose(yue2_params())
        await wait_until(lambda: list(record.glob("*-synth.json")))
        rig.store.cancel_job(job["id"])
        rig.runner.wake()
        await wait_until(lambda: list(record.glob("*-cancel.txt")))
        await wait_until(lambda: rig.runner.current_job_id is None)
        assert rig.store.get_job(job["id"])["state"] == "cancelled"
        assert len(list(record.glob("*-synth.json"))) == 1   # variant 2 was never sent
        assert rig.store.list_takes(job["id"]) == []


@pytest.mark.asyncio
async def test_a_failed_variant_fails_the_job_with_the_engine_log(tmp_path, monkeypatch):
    yue2_env(monkeypatch, "fail_synth")
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home) as rig:
        job = rig.propose(yue2_params())
        job = await rig.wait_for(job["id"], ("failed", "review"))
        assert job["state"] == "failed" and "yue2.cpp /synth job" in job["state_reason"]
