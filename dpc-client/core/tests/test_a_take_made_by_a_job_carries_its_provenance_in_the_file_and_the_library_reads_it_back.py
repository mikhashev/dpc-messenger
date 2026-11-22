"""PROVENANCE §3-§6 end to end: a take made by a job (both engines) carries the full
`mp.provenance/1` record inside its FLAC, the sidecar holds the same record, the decoded audio
is the engine's PCM, a record the privacy guard refuses leaves the take without one (and the
older sidecar), and the library reads a scanned file's record back defensively."""

import json
import logging
from pathlib import Path

import pytest

from dpc_client_core.music import audio_tags as at
from dpc_client_core.music import job_schemas as js
from dpc_client_core.music import provenance as pv
from dpc_client_core.music.identity import MediaTools, read_flac_streaminfo
from dpc_client_core.music.library import MusicLibrary
from tests._acestep_helpers import Rig, fake_flac, free_env, from_text, install_fake_engine
from tests._yue2_helpers import DualRig, install_both, yue2_env, yue2_params

DONE = (js.REVIEW, js.FAILED, js.CANCELLED)


def tag_of(flac: Path) -> dict:
    return json.loads(at.flac_read_comments(flac, max_block_bytes=1 << 20)[pv.TAG])


@pytest.mark.asyncio
async def test_an_acestep_take_carries_the_full_record_in_its_flac_and_the_same_one_in_its_sidecar(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=2))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        takes = rig.store.list_takes(job["id"])
        out = home / "music" / "generated" / job["id"]
        lm_result = json.loads(rig.store.read_payload(job["id"], "lm.result.json"))[0]
        for take in takes:
            flac = out / f"indie-rock-warm-guitars_v{take['variant']}.flac"
            rec = tag_of(flac)
            side = json.loads(flac.with_suffix(".json").read_text(encoding="utf-8"))
            assert rec["schema"] == "mp.provenance/1" and rec["engine"] == "acestep.cpp" and rec["job_id"] == job["id"]
            # the sidecar is the same record plus the ids the store assigns after the file exists
            assert {k: v for k, v in side.items() if k not in ("take_id", "library_track_id", "embedded_in_file")} == rec
            assert side["take_id"] == take["id"] and side["embedded_in_file"] is True
            assert rec["audio_codes"] == lm_result["audio_codes"] and rec["codes_count"] == 150
            assert rec["audio_codes_sha256"] == pv.text_sha256(lm_result["audio_codes"])
            assert (rec["seed"], rec["synth_batch_size"], rec["synth_batch_index"]) == (42, 2, take["synth_batch_index"])
            assert rec["lyrics"] == "[verse]\nТысячи умов, один огонь\n" and rec["lyrics_prep"] is None
            assert rec["caption_sent"] == "indie rock, warm guitars" and rec["task_type"] == "text2music"
            assert rec["payloads"] == {p["name"]: p["sha256"] for p in job["payloads"]}
            assert rec["loudness"]["lufs_i"] == take["loudness"]["lufs_i"]            # measured once, kept in both
            assert rec["genome_sha256"].startswith("sha256:") and rec["render_sha256"] != rec["genome_sha256"]
            # rule 1: the decoded audio is the engine's PCM, after the tag as before it
            assert read_flac_streaminfo(flac).md5 == rec["engine_output"]["pcm_md5"] == rec["output"]["pcm_md5"]
            assert rec["dpc_postprocess"]["cmd_as_sent"][0] == "ffmpeg"
            cmd = rec["dpc_postprocess"]["cmd_as_sent"]
            assert cmd[0] == "ffmpeg" and cmd[cmd.index("-i") + 1].endswith(".wav.part") and cmd[-1].endswith(".flac.part")
            assert not any(a.startswith(("/", "\\")) or a[1:3] in (":\\", ":/") for a in cmd)
            tags = at.flac_read_comments(flac, max_block_bytes=1 << 20)
            assert tags["LYRICS"] == tags["UNSYNCEDLYRICS"] == rec["lyrics"]
            assert str(tmp_path) not in tags[pv.TAG]
            entry = next(e for e in rig.library.list_entries() if e["id"] == take["library_track_id"])
            assert entry["provenance"]["engine"] == "acestep.cpp" and entry["provenance"]["has_codes"] is True
            assert entry["provenance"]["seed"] == 42 and entry["provenance"]["genome_sha256"] == rec["genome_sha256"]


@pytest.mark.asyncio
async def test_an_instrumental_take_has_no_lyrics_tags_but_its_record(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    async with Rig(home) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1, lyrics=None))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        flac = next((home / "music" / "generated" / job["id"]).glob("*.flac"))
        tags = at.flac_read_comments(flac, max_block_bytes=1 << 20)
        assert "LYRICS" not in tags and "UNSYNCEDLYRICS" not in tags
        rec = json.loads(tags[pv.TAG])
        assert rec["instrumental"] is True and rec["lyrics"] == "[Instrumental]"


@pytest.mark.asyncio
async def test_a_yue2_take_carries_the_score_the_semantic_tokens_and_batch_one(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    async with DualRig(home) as rig:
        job = rig.propose(yue2_params(variants=2))
        job = await rig.wait_for(job["id"], ("review", "failed"))
        assert job["state"] == "review", job["state_reason"]
        out = home / "music" / "generated" / job["id"]
        flac = out / "indie-rock-male-vocal_v2.flac"
        rec = tag_of(flac)
        replay = json.loads(rig.store.read_payload(job["id"], "synth.v2.result.json"))
        assert rec["engine"] == "yue2.cpp" and rec["variant"] == 2
        assert rec["semantic_tokens"] == replay["semantic_tokens"] and rec["semantic_tokens_count"] == 25
        assert rec["score_abc"] == replay["abc"] and rec["backbone"] == "stock" and rec["adapters"] == []
        assert (rec["seed"], rec["variant_seed"], rec["lm_seed"]) == (42, 43, 43)
        assert (rec["synth_batch_size"], rec["synth_batch_index"], rec["lm_batch_size"]) == (1, 0, 1)
        assert rec["cot"] == "full" and rec["inference_steps"] == 32
        assert next(m for m in rec["models"] if m["role"] == "backbone")["sha256"].startswith("sha256:")
        assert read_flac_streaminfo(flac).md5 == rec["output"]["pcm_md5"]
        side = json.loads(flac.with_suffix(".json").read_text(encoding="utf-8"))
        assert side["schema"] == "mp.provenance/1" and side["semantic_tokens"] == rec["semantic_tokens"]
        entry = next(e for e in rig.library.list_entries() if e["path"].endswith("_v2.flac"))
        assert entry["provenance"]["engine"] == "yue2.cpp" and entry["provenance"]["has_codes"] is True


@pytest.mark.asyncio
async def test_a_record_the_privacy_guard_refuses_leaves_the_take_without_one_and_the_job_goes_on(tmp_path, monkeypatch, caplog):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    async with Rig(home) as rig:
        with caplog.at_level(logging.WARNING):
            job = rig.store.create_job("generate_from_text",
                                       from_text(variants=1, lyrics="[verse]\nsaved as C:\\Users\\someone\\song.wav\n"))
            rig.runner.wake()
            job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        flac = next((home / "music" / "generated" / job["id"]).glob("*.flac"))
        assert pv.TAG not in at.flac_read_comments(flac, max_block_bytes=1 << 20)
        side = json.loads(flac.with_suffix(".json").read_text(encoding="utf-8"))
        assert side["schema"] == "dpc.take-sidecar/1" and "mp.provenance/1 record could not be built" in side["note"]
        assert "absolute Windows path" in caplog.text and "C:\\Users" not in caplog.text
        assert len(rig.store.list_takes(job["id"])) == 1


@pytest.mark.asyncio
async def test_a_job_whose_flac_cannot_take_the_comment_keeps_the_take_and_says_it_is_not_embedded(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)

    def odd_converter(wav, flac, tags):
        fake_flac(wav, flac, tags)
        flac.write_bytes(flac.read_bytes()[:4] + b"\x80\x00\x00\x22" + b"\x00" * 34)   # STREAMINFO md5 left zero
    async with Rig(home, converter=odd_converter) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], (js.REVIEW, js.FAILED))
        # the converter's own check refuses a FLAC whose md5 is not the engine's PCM: the job fails, nothing half-tagged
        assert job["state"] == js.FAILED and "not lossless" in job["state_reason"]


# -- the library reads a scanned file's record back --------------------------------------------------


def _flac_with(tmp_path: Path, name: str, comment: str, seed: int) -> Path:
    from tests.test_a_flac_or_wav_gets_its_provenance_once_and_its_audio_is_unchanged import make_wav
    wav = make_wav(tmp_path / f"{name}.wav", seconds=0.1 + seed / 100)
    flac = tmp_path / "scan" / f"{name}.flac"
    flac.parent.mkdir(parents=True, exist_ok=True)
    fake_flac(wav, flac, {})
    wav.unlink()
    if comment:
        at.flac_add_comments(flac, {pv.TAG: comment})
    return flac


@pytest.mark.asyncio
async def test_a_scan_reads_a_good_record_ignores_a_bad_or_oversized_one_and_keeps_every_track(tmp_path, caplog):
    good, _ = pv.finalize(__import__("tests.test_the_provenance_record_is_built_per_engine_and_refuses_private_or_oversized_content",
                                     fromlist=["acestep"]).acestep())
    files = {
        "good": _flac_with(tmp_path, "good", pv.compact_json(good), 1),
        "garbage": _flac_with(tmp_path, "garbage", "{not json", 2),
        "wrong": _flac_with(tmp_path, "wrong", '{"schema":"mp.provenance/9","engine":"x"}', 3),
        "huge": _flac_with(tmp_path, "huge", '{"schema":"mp.provenance/1","engine":"x","pad":"' + "a" * 70000 + '"}', 4),
        "plain": _flac_with(tmp_path, "plain", "", 5),
    }
    lib = MusicLibrary(tmp_path / "home", tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    try:
        with caplog.at_level(logging.INFO):
            folder = await lib.add_folder(str(tmp_path / "scan"), watch=False, scan=False)
            await lib.rescan_folder(folder["id"])
            await lib.wait_hashed()
        entries = {Path(e["path"]).stem: e for e in lib.list_entries()}
        assert set(entries) == set(files) and all(e["hash_state"] == "done" for e in entries.values())
        assert entries["good"]["provenance"] == {
            "schema": "mp.provenance/1", "engine": "acestep.cpp", "seed": 42, "has_codes": True,
            "codes_by_hash_only": False, "genome_basis": "audio_codes", "genome_sha256": good["genome_sha256"],
            "intent_sha256": good["intent_sha256"], "render_sha256": good["render_sha256"]}
        for name in ("garbage", "wrong", "huge", "plain"):
            assert "provenance" not in entries[name]
        assert "ignoring the provenance record in garbage.flac" in caplog.text
        assert "ignoring the provenance record in wrong.flac" in caplog.text
        assert "ignoring the provenance record in huge.flac" in caplog.text and "over the 65536-byte limit" in caplog.text
        # the summary never holds a path, and the library file passes its own schema check
        from dpc_client_core.music import schemas
        for e in entries.values():
            schemas.validate_library_entry(e)
        assert str(tmp_path) not in json.dumps(entries["good"]["provenance"])
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_re_tagging_a_scanned_file_keeps_its_track_its_hidden_flag_and_adds_the_summary(tmp_path):
    flac = _flac_with(tmp_path, "song", "", 1)
    lib = MusicLibrary(tmp_path / "home", tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    try:
        folder = await lib.add_folder(str(tmp_path / "scan"), watch=False, scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        (before,) = lib.list_entries()
        await lib.set_hidden(before["id"], True)
        good, text = pv.finalize(__import__("tests.test_the_provenance_record_is_built_per_engine_and_refuses_private_or_oversized_content",
                                            fromlist=["acestep"]).acestep())
        at.flac_add_comments(flac, {pv.TAG: text})          # what the re-tag script does: new bytes, same audio
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        (after,) = lib.list_entries()
        assert after["id"] == before["id"] and after["hidden"] is True and after["content_hash"] == before["content_hash"]
        assert after["provenance"]["engine"] == "acestep.cpp" and after.get("missing") is not True
    finally:
        await lib.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg is not installed")
async def test_the_real_ffmpeg_flac_takes_the_record_and_keeps_its_audio_and_ffmpeg_tags(tmp_path, monkeypatch):
    import subprocess
    from dpc_client_core.music.runner import ffmpeg_converter
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    async with Rig(home, converter=ffmpeg_converter()) as rig:
        job = rig.store.create_job("generate_from_text", from_text(variants=1))
        rig.runner.wake()
        job = await rig.wait_for(job["id"], DONE)
        assert job["state"] == js.REVIEW, job["state_reason"]
        flac = next((home / "music" / "generated" / job["id"]).glob("*.flac"))
        tags = at.flac_read_comments(flac, max_block_bytes=1 << 20)
        rec = json.loads(tags[pv.TAG])
        assert tags["DPC_JOB_ID"] == job["id"] and tags["TITLE"] and tags["LYRICS"] == rec["lyrics"]   # ffmpeg's tags kept
        assert read_flac_streaminfo(flac).md5 == rec["engine_output"]["pcm_md5"]
        # an independent decode agrees the file is whole FLAC audio
        out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(flac), "-map", "0:a:0", "-f", "md5", "-"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL)
        assert out.returncode == 0 and out.stdout.startswith("MD5=")
        assert rec["dpc_postprocess"]["tool_version"].startswith("ffmpeg version")
