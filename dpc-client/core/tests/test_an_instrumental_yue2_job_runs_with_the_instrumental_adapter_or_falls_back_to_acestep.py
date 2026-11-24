"""Mike, 2026-11-24 (group #75/#76), option (c), built general: an Instrumental job on YuE2 runs
with the instrumental adapter by default, and acestep.cpp carries it when that adapter is not
installed or does not load (backlog AN-INSTRUMENTAL-JOB-ON-YUE2-COMES-BACK-SUNG-...).

The adapter is a one-half (music, AR) file in yue2.cpp's native key layout, found in the loras
folder and copied whole into the cache the server reads. The choice is made at propose time
and recorded on the job (`engine_choice`) and in each take's provenance; a run-time failure
moves the job to acestep.cpp with `engine_choice.fell_back`, never silently.
"""

import copy
import hashlib
import json
import shutil

import pytest

from dpc_client_core.music import job_schemas as js
from dpc_client_core.music.jobs import MusicJobStore
from dpc_client_core.music.schemas import SchemaError
from dpc_client_core.music.yue2 import (
    DEFAULT_INSTRUMENTAL_ADAPTER, INSTRUMENTAL_LYRICS, adapter_key_half, adapter_layout,
)
from tests._yue2_helpers import (
    VOICES, DualRig, install_both, install_fake_yue2, make_yue2, safetensors_bytes, yue2_env, yue2_params,
)

INST = DEFAULT_INSTRUMENTAL_ADAPTER


def install_instrumental(home):
    """The instrumental adapter as goldhub/Ruach_Studio_Models_v2 ships it: a folder in the
    loras folder holding the bf16 file, native keys on the music half only."""
    folder = home / "models" / "music" / "yue2" / "loras" / INST
    folder.mkdir(parents=True, exist_ok=True)
    keys = [f"layers.{i}.{site}.lora_{ab}" for i in range(2)
            for site in ("self_attn.q_proj", "mlp.down_proj") for ab in "AB"]
    path = folder / "ar_lora_inst_v3abc.bf16.safetensors"
    path.write_bytes(safetensors_bytes(keys, {"rank": "64", "intended_cot": "full"}))
    (folder / "README.md").write_text("instrumental")
    return path


def instrumental(**over):
    return yue2_params(lyrics=None, style="drum and bass", variants=1, **over)


def test_adapter_keys_are_read_as_yue2_cpp_reads_them():
    assert adapter_key_half("layers.3.self_attn.q_proj.lora_A") == "music"
    assert adapter_key_half("model.layers.3.nar_mlp.up_proj.lora_down.weight") == "sound"
    assert adapter_key_half("text_encoders.model.layers.0.self_attn.qkv_proj.lora_up.weight") == "music"
    assert adapter_key_half("diffusion_model.model.layers.0.mlp.gate_up_proj.lora_A.weight") == "sound"
    assert adapter_key_half("diffusion_model.llm2vae.diff") == "sound"
    assert adapter_key_half("adapters.model-layers-2-self_attn-k_proj.lora_down.weight") == "music"
    assert adapter_key_half("layers.0.nar_mlp.lora_A") is None             # no site: the server refuses it
    assert adapter_key_half("text_encoders.model.layers.0.nar_mlp.up_proj.lora_A") is None
    assert adapter_key_half("layers.x.self_attn.q_proj.lora_A") is None
    assert adapter_key_half("layers.0.self_attn.q_proj.weight") is None    # a full replacement off vae2llm
    assert adapter_layout({"layers.0.self_attn.q_proj.lora_A": {}}) == ("whole", ["music"], None)
    both = adapter_layout({"layers.0.self_attn.q_proj.lora_A": {}, "layers.0.nar_mlp.up_proj.lora_A": {}})
    assert both[0] is None and "both halves" in both[2]


def test_a_one_half_native_adapter_in_the_loras_folder_is_offered_whole_and_copied_byte_for_byte(tmp_path):
    home = tmp_path / "dpc"
    models = install_fake_yue2(home)
    source = install_instrumental(home)
    engine = make_yue2(home)
    info = engine.form_info()
    offered = {a["name"]: a for a in info["adapters"]}
    assert offered[INST]["kind"] == "instrumental" and offered[INST]["layout"] == "whole"
    assert offered[INST]["halves"] == ["music"]
    assert all(offered[v]["layout"] == "split" for v in VOICES)
    purpose = info["purpose_adapters"]["instrumental"]
    assert purpose["offered"] and purpose["adapter"] == INST and purpose["state"] == "offered"

    entries = engine.prepare_adapters()
    copy_path = models / "loras" / ".split" / f"{INST}-music.safetensors"
    assert copy_path.read_bytes() == source.read_bytes()
    assert entries[INST]["layout"] == "whole" and list(entries[INST]["halves"]) == ["music"]
    assert entries[INST]["halves"]["music"]["sha256"] == entries[INST]["source_sha256"] == \
        "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    assert engine.request_adapters([{"name": INST, "music": 1.0, "sound": 1.0}]) == [
        {"name": f"{INST}-music.safetensors", "scale": 1.0}]
    assert engine.instrumental_lyrics_for([{"name": INST}]) == INSTRUMENTAL_LYRICS
    assert engine.instrumental_lyrics_for([{"name": VOICES[0]}]) is None

    (source.parent / "adapter_config.json").write_text('{"lora_alpha": 32}')
    purpose = make_yue2(home).form_info()["purpose_adapters"]["instrumental"]
    assert not purpose["offered"] and purpose["state"] == "unusable" and "adapter_config.json" in purpose["reason"]


def test_the_engine_choice_round_trips_through_the_job_record_and_a_wrong_one_is_refused(tmp_path):
    home = tmp_path / "dpc"
    install_both(home)
    install_instrumental(home)
    rig = DualRig(home, start_runner=False)
    job = rig.propose(instrumental())
    choice = job["engine_choice"]
    assert choice["rule"] == "instrumental" and choice["engine"] == "yue2" and choice["adapters"] == [INST]
    assert choice["fallback"] == "acestep" and choice["fell_back"] is None and INST in choice["reason"]
    assert {"name": INST, "music": 1.0, "sound": 1.0} in job["params"]["adapters"]
    song = rig.propose(yue2_params())
    assert song["engine_choice"] is None and song["params"]["adapters"] == []

    reopened = MusicJobStore(home, capabilities=rig.store.capabilities)
    reopened.open()
    assert reopened.get_job(job["id"]) == job and reopened.get_job(song["id"]) == song

    for broken in ({"engine": "acestep"}, {"adapters": ["voice-nobody"]}, {"rule": "karaoke"}, {"extra": 1}):
        bad = copy.deepcopy(job)
        bad["engine_choice"].update(broken)
        with pytest.raises(SchemaError):
            js.validate_job(bad)

    report = rig.store.capabilities.report()
    assert report["engine_choice_rules"]["instrumental"]["engine"] == "yue2"
    assert report["engine_choice_rules"]["instrumental"]["adapters"] == [INST]


@pytest.mark.asyncio
async def test_an_instrumental_job_runs_on_yue2_with_the_adapter_and_its_take_names_it(tmp_path, monkeypatch):
    record = tmp_path / "bodies"
    yue2_env(monkeypatch, record=record)
    home = tmp_path / "dpc"
    install_both(home)
    source = install_instrumental(home)
    async with DualRig(home) as rig:
        job = rig.propose(instrumental())
        job = await rig.wait_for(job["id"], ("review", "failed"))
        assert job["state"] == "review", job["state_reason"]
        assert not rig.ace.alive()
        (path,) = record.glob("*-synth.json")
        body = json.loads(path.read_bytes())
        assert body["adapters"] == [{"name": f"{INST}-music.safetensors", "scale": 1.0}]
        assert body["lyrics"] == INSTRUMENTAL_LYRICS and body["cot"] == "full"

        out = home / "music" / "generated" / job["id"]
        sidecar = json.loads(next(out.glob("*_v1.json")).read_text(encoding="utf-8"))
        (a,) = sidecar["adapters"]
        assert a["name"] == INST and "sound" not in a and a["music"]["scale"] == 1.0
        assert a["source"]["sha256"] == "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        assert sidecar["engine_choice"]["rule"] == "instrumental" and sidecar["engine_choice"]["engine"] == "yue2"
        assert sidecar["engine_choice"]["fell_back"] is None and sidecar["instrumental"] is True
        assert str(tmp_path) not in json.dumps(sidecar)


@pytest.mark.asyncio
async def test_without_the_adapter_an_instrumental_job_goes_to_acestep_and_says_why(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home) as rig:
        report = rig.store.capabilities.report()
        rule = report["engine_choice_rules"]["instrumental"]
        assert rule["engine"] == "acestep" and "not installed" in rule["reason"]
        assert report["engines"]["yue2"]["purpose_adapters"]["instrumental"]["state"] == "missing"

        job = rig.propose(instrumental(adapters=[{"name": VOICES[0]}]))
        assert job["params"]["engine"] == "acestep" and "adapters" not in job["params"]
        choice = job["engine_choice"]
        assert choice["engine"] == "acestep" and choice["fallback"] is None and choice["adapters"] == []
        assert f"{INST} is not installed" in choice["reason"] and VOICES[0] in choice["reason"]
        job = await rig.wait_for(job["id"], ("review", "failed"))
        assert job["state"] == "review", job["state_reason"]
        assert not rig.yue2.alive()
        assert [t["engine"] for t in rig.store.list_takes(job["id"])] == ["acestep.cpp"]
        out = home / "music" / "generated" / job["id"]
        sidecar = json.loads(next(out.glob("*_v1.json")).read_text(encoding="utf-8"))
        assert sidecar["engine"] == "acestep.cpp" and sidecar["engine_choice"]["engine"] == "acestep"


@pytest.mark.asyncio
async def test_an_adapter_that_does_not_load_moves_the_running_job_to_acestep(tmp_path, monkeypatch):
    yue2_env(monkeypatch, "skip_instrumental")
    home = tmp_path / "dpc"
    install_both(home)
    install_instrumental(home)
    async with DualRig(home) as rig:
        job = rig.propose(instrumental())
        assert job["params"]["engine"] == "yue2"
        job = await rig.wait_for(job["id"], ("review", "failed"))
        assert job["state"] == "review", job["state_reason"]
        assert job["params"]["engine"] == "acestep"
        choice = job["engine_choice"]
        assert choice["engine"] == "acestep" and choice["fallback"] is None
        assert choice["fell_back"]["from"] == "yue2" and choice["fell_back"]["adapters"] == [INST]
        assert "did not load" in choice["reason"] and str(tmp_path) not in json.dumps(choice)
        assert [h["state"] for h in job["history"]] == ["queued", "running", "review"]
        names = [p["name"] for p in job["payloads"]]
        assert "synth.v1.request.json" in names and "lm.request.json" in names
        assert [t["engine"] for t in rig.store.list_takes(job["id"])] == ["acestep.cpp"]
        assert not rig.yue2.alive()
        out = home / "music" / "generated" / job["id"]
        sidecar = json.loads(next(out.glob("*_v1.json")).read_text(encoding="utf-8"))
        assert sidecar["engine_choice"]["fell_back"]["from"] == "yue2"


@pytest.mark.asyncio
async def test_an_adapter_gone_before_the_run_moves_the_queued_job_to_acestep(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    source = install_instrumental(home)
    async with DualRig(home, start_runner=False) as rig:
        job = rig.propose(instrumental())
        assert job["engine_choice"]["engine"] == "yue2"
        shutil.rmtree(source.parent)
        rig.runner.start()
        job = await rig.wait_for(job["id"], ("review", "failed", "unsupported"))
        assert job["state"] == "review", job["state_reason"]
        assert job["engine_choice"]["fell_back"]["from"] == "yue2"
        assert "no longer offered" in job["engine_choice"]["reason"]
        assert [t["engine"] for t in rig.store.list_takes(job["id"])] == ["acestep.cpp"]
