"""PROVENANCE §3 and §6: the MP3 export fills `source_record` from the source file's own
`mp.provenance/1` and writes USLT from the record when the library has no lyrics; the re-tag
script (`music/provenance_retag.py`, CLI `tools/retag_music_provenance.py`) writes the best
record into existing WAV and FLAC files from their sidecars, never guessing, with a dry run,
the PCM verified, a write-once rule, and the library's identity of the file unchanged."""

import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from dpc_client_core.music import audio_tags as at
from dpc_client_core.music import provenance as pv
from dpc_client_core.music import provenance_retag as rt
from dpc_client_core.music.export import ExportSource, MusicExporter, read_id3_txxx
from dpc_client_core.music.identity import MediaTools, identify
from dpc_client_core.music.runner import pcm_md5_and_sha256, read_wav_info
from tests._acestep_helpers import fake_flac
from tests.test_a_flac_or_wav_gets_its_provenance_once_and_its_audio_is_unchanged import make_wav
from tests.test_the_provenance_record_is_built_per_engine_and_refuses_private_or_oversized_content import (
    LYRICS, acestep, yue2)

FFMPEG = shutil.which("ffmpeg")
HERE = Path(__file__).resolve()


# -- the MP3 export -----------------------------------------------------------------------------------


class FakeFfmpeg:
    def __init__(self):
        self.encodes = []

    def __call__(self, cmd, *, timeout):
        cmd = list(cmd)
        if "-encoders" in cmd:
            return subprocess.CompletedProcess(cmd, 0, b" A....D libmp3lame  libmp3lame MP3\n", b"")
        if "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, b"ffmpeg version 9.9-test Copyright (c) x\n", b"")
        self.encodes.append(cmd)
        Path(cmd[-1]).write_bytes(b"\xff\xfb\x90\x00" + b"audio" * 50)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


def _tagged_flac(tmp_path, rec_text=None, name="a.flac"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    wav = make_wav(tmp_path / "w.wav")
    flac = tmp_path / "lib" / name
    flac.parent.mkdir(parents=True, exist_ok=True)
    fake_flac(wav, flac, {})
    if rec_text:
        at.flac_add_comments(flac, {pv.TAG: rec_text})
    return flac


def has_uslt(raw: bytes) -> bool:
    return b"USLT\x00\x00" in raw[:16384]          # a frame header, not the word in the provenance JSON


def _export(tmp_path, source, out="out"):
    exporter = MusicExporter(tmp_path / out, tools=MediaTools(ffmpeg="ffmpeg", ffprobe=None, runner=FakeFfmpeg()),
                             measure=lambda p: {"true_peak_dbtp": -1.5})
    path = Path(exporter.export_track(source)["path"])
    return path, read_id3_txxx(path), path.read_bytes()


def _source(flac, **kw):
    return ExportSource(track_id="trk_a", path=flac, content_hash="md5:" + "ab" * 16, title="Song", artist="Band",
                        true_peak_dbtp=-2.0, true_peak_basis="test", **kw)


def test_the_export_carries_the_sources_own_record_and_writes_uslt_from_it_when_the_library_has_no_lyrics(tmp_path):
    rec, text = pv.finalize(acestep())
    flac = _tagged_flac(tmp_path, text)
    sidecar = {**rec, "take_id": "tak_0123456789abcdef", "library_track_id": "trk_x", "embedded_in_file": True}
    path, tags, raw = _export(tmp_path, _source(flac, take_record=sidecar, origin="generated"))
    out = json.loads(tags["DPC_PROVENANCE"])
    assert out["schema"] == "dpc.export-provenance/1"
    assert out["source_record"] == rec                                   # unchanged, whole (rule 7)
    assert out["take"] == {"schema": "mp.provenance/1", "take_id": "tak_0123456789abcdef", "library_track_id": "trk_x"}
    assert "source_record_note" not in out and str(tmp_path) not in tags["DPC_PROVENANCE"]
    assert has_uslt(raw) and "Ты́сячи умов".encode("utf-8") in raw           # from the record's lyrics
    assert out["source_record"]["audio_codes_sha256"] == rec["audio_codes_sha256"]
    assert len(tags["DPC_PROVENANCE"].encode("utf-8")) <= pv.MAX_BYTES


def test_the_librarys_lyrics_win_over_the_record_and_an_instrumental_has_no_uslt(tmp_path):
    rec, text = pv.finalize(acestep())
    flac = _tagged_flac(tmp_path, text)
    _p, _t, raw = _export(tmp_path, _source(flac, lyrics="the library's own text", lyrics_language="eng"))
    uslt = raw[raw.index(b"USLT\x00\x00"):][:200]
    assert b"the library's own text" in uslt and "Ты́сячи".encode("utf-8") not in uslt
    inst, itext = pv.finalize(acestep(params={"lyrics": None, "duration_s": 30},
                                      lm_request={"caption": "ambient", "lyrics": "[Instrumental]"}))
    flac2 = _tagged_flac(tmp_path / "two", itext, "b.flac")
    _p, _t, raw2 = _export(tmp_path / "two", _source(flac2))
    assert not has_uslt(raw2)


def test_a_source_without_a_record_exports_as_before_with_no_source_record(tmp_path):
    flac = _tagged_flac(tmp_path)
    _p, tags, raw = _export(tmp_path, _source(flac))
    out = json.loads(tags["DPC_PROVENANCE"])
    assert "source_record" not in out and not has_uslt(raw)


def test_a_bad_record_in_the_source_is_noted_not_carried(tmp_path):
    flac = _tagged_flac(tmp_path, '{"schema":"mp.provenance/9"}')
    _p, tags, _raw = _export(tmp_path, _source(flac))
    out = json.loads(tags["DPC_PROVENANCE"])
    assert out["source_record"] is None and "not carried over" in out["source_record_note"]


def test_the_lyrics_come_from_the_source_files_own_lyrics_tag_when_the_record_has_none(tmp_path):
    flac = _tagged_flac(tmp_path)
    at.flac_add_comments(flac, {"UNSYNCEDLYRICS": "from the file's own tag"})
    exporter = MusicExporter(tmp_path / "out", tools=MediaTools(ffmpeg="ffmpeg", ffprobe=None))
    assert exporter._lyrics_for_tag(_source(flac), {"unsyncedlyrics": "from the file's own tag"}, {}) == "from the file's own tag"
    assert exporter._lyrics_for_tag(_source(flac), {}, {}) is None


# -- the re-tag script ----------------------------------------------------------------------------------


ACE_BODY = {"caption": "An indie rock track", "seed": 42, "lyrics": LYRICS, "audio_codes": ",".join(["1000"] * 300),
            "bpm": 110, "duration": 30.0, "keyscale": "E minor", "timesignature": "4", "vocal_language": "ru",
            "lm_seed": 876362762, "inference_steps": 8, "solver": "euler", "lm_mode": "generate",
            "output_format": "wav24", "synth_batch_size": 2, "task_type": "text2music",
            "synth_model": "acestep-v15-turbo-Q8_0.gguf", "guidance_scale": 1.0, "shift": 3.0}
ACE_INFO = {"dit_model_file": "acestep-v15-turbo-Q8_0.gguf", "lm_model": "acestep-5Hz-lm-4B-Q8_0.gguf", "seed": 42,
            "lm_seed": 876362762, "steps": 8, "synth_batch_index": 1, "synth_batch_size": 2, "solver": "euler",
            "duration_requested_s": 30.0, "duration_received_s": 29.44, "engine": "acestep.cpp 973517b",
            "date": "2026-11-20"}


def _ffmpeg_md5(path):
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "md5", "-"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()
    return out


def _trial_wav(folder: Path, name="turbo_v1", body=None, info=None, extra=None, claim=True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    wav = make_wav(folder / f"{name}.wav", info={"INAM": "Song", "ICMT": '{"old":1}'})
    meta = dict(info or ACE_INFO)
    if claim and FFMPEG:
        meta["audio_pcm_md5_before"] = _ffmpeg_md5(wav)
    (folder / f"{name}.json").write_text(json.dumps({"request_body": body or ACE_BODY, "info": meta, **(extra or {})},
                                                    ensure_ascii=False), encoding="utf-8")
    return wav


def _data_md5(wav):
    return pcm_md5_and_sha256(wav, read_wav_info(wav))[0]


def test_a_dry_run_builds_every_record_and_writes_nothing(tmp_path):
    wav = _trial_wav(tmp_path / "trial")
    before = (wav.read_bytes(), wav.with_suffix(".json").read_bytes())
    results = rt.retag_tree([tmp_path / "trial"], dry_run=True, now="2026-11-21T12:00:00Z")
    assert [(r.status, r.container) for r in results] == [("would_tag", "wav")] and results[0].record_bytes > 1000
    assert (wav.read_bytes(), wav.with_suffix(".json").read_bytes()) == before
    assert rt.summarize(results)["by_status"] == {"would_tag": 1}


def test_a_trial_wav_gets_the_record_with_not_stored_where_the_trial_kept_nothing_and_its_pcm_unchanged(tmp_path):
    wav = _trial_wav(tmp_path / "trial")
    pcm = _data_md5(wav)
    (result,) = rt.retag_tree([tmp_path / "trial"], dry_run=False, now="2026-11-21T12:00:00Z")
    assert result.status == "tagged" and result.pcm_md5 == pcm and _data_md5(wav) == pcm
    items = at.wav_read_info(wav)
    assert items["INAM"] == "Song" and items["ICMT"] == '{"old":1}'          # the old tags stay
    assert "DPCP" not in items                                               # the old INFO form is not written any more
    record_text = at.wav_read_provenance(wav)
    assert at.wav_read_id3(wav)["DPC_PROVENANCE"] == record_text
    rec = json.loads(record_text)
    assert rec["schema"] == "mp.provenance/1" and rec["engine"] == "acestep.cpp" and rec["engine_commit"] == "973517b"
    # what the trial stored, exactly
    assert rec["audio_codes"] == ACE_BODY["audio_codes"] and rec["codes_count"] == 300 and rec["solver"] == "euler"
    assert (rec["seed"], rec["variant_seed"], rec["synth_batch_size"], rec["synth_batch_index"]) == (42, 43, 2, 1)
    assert rec["task_type"] == "text2music" and rec["guidance_scale"] == 1.0 and rec["shift"] == 3.0
    assert rec["caption_chosen"] == "An indie rock track" and rec["duration_received_s"] == 29.44
    assert rec["pcm_md5"] == pcm and rec["output"]["container"] == "wav"
    # what it did not: never guessed
    for key in ("caption_sent", "bpm_sent", "engine_binaries", "build", "loudness", "instrumental", "lyrics_prep"):
        assert rec[key] == pv.NOT_STORED, key
    assert all(m["sha256"] == pv.NOT_STORED for m in rec["models"]) and rec["pronunciation"]["stress_marks"] == pv.NOT_STORED
    # the codes are stored, so the take keeps its genome (and render) id although the text it was made from is not;
    # only the intent is unnamed (EVOLUTION section 3)
    assert rec["genome_basis"] == "audio_codes" and rec["genome_sha256"].startswith("sha256:")
    assert rec["render_sha256"].startswith("sha256:") and rec["intent_sha256"] == pv.NOT_STORED
    assert "no intent id" in rec["ids_note"] and "no genome" not in rec["ids_note"]
    assert rec["dpc_postprocess"]["step"] == "retag" and rec["retagged"]["at"] == "2026-11-21T12:00:00Z"
    if FFMPEG:
        assert "MD5=" + rec["engine_output"]["ffmpeg_decoded_md5"] == _ffmpeg_md5(wav)         # the trial's own check, kept
    # the sidecar keeps its own keys and gains the record
    side = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
    assert side["mp_provenance"] == rec and side["request_body"] == ACE_BODY
    assert str(tmp_path) not in record_text


def test_a_yue2_trial_gets_its_weights_hashes_adapters_and_the_tokens_from_its_replay_file(tmp_path):
    replay = {"style": "rock", "lyrics": LYRICS, "abc": "X:1\n", "seed": 42, "lm_seed": 42,
              "semantic_tokens": ",".join(str(100 + i) for i in range(500))}
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "replay.json").write_text(json.dumps(replay), encoding="utf-8")
    body = {"style": "rock", "lyrics": LYRICS, "cot": "full", "duration": 60.0, "lm_seed": 42, "seed": 42, "steps": 32,
            "lm_batch_size": 1, "synth_batch_size": 1, "output_format": "wav24", "peak_clip": 10,
            "adapters": [{"name": "voice-x-music.safetensors", "scale": 0.45},
                         {"name": "voice-x-sound.safetensors", "scale": 0.35}]}
    info = {"engine": "yue2.cpp e646d52", "model_file": "YuE2-3B-Q8_0.gguf", "model_sha256": "b" * 64,
            "model": "YuE2-3B-Q8_0.gguf (own conversion of m-a-p/YuE2-3B)", "vae_file": "YuE2-Vae-F32.gguf",
            "vae_sha256": "c" * 64, "variant": 0, "date": "2026-11-21", "duration_req_s": 60.0, "duration_rcv_s": 58.5,
            "voice": "voice-x", "adapter_source": {"path": "voices/voice-x/voice-x-e029.safetensors", "sha256": "d" * 64},
            "adapter_split_files": {"music": {"sha256": "e" * 64, "scale": 0.45}, "sound": {"sha256": "f" * 64, "scale": 0.35}},
            "loudness": {"lufs_i": -12.9, "lra_lu": 7.1, "true_peak_dbtp": 0.5},
            "raw_dir": "C:/Users/someone/trial/raw", "replay_request_with_tokens": str(tmp_path / "raw" / "replay.json")}
    wav = _trial_wav(tmp_path / "y", "song_v0", body=body, info=info)
    (result,) = rt.retag_tree([wav.parent], dry_run=False)
    assert result.status == "tagged", result.reason
    rec = json.loads(at.wav_read_provenance(wav))
    assert rec["engine"] == "yue2.cpp" and rec["semantic_tokens_count"] == 500 and rec["score_abc"] == "X:1\n"
    assert next(m for m in rec["models"] if m["role"] == "backbone")["sha256"] == "sha256:" + "b" * 64
    assert rec["adapters"][0]["source"] == {"file": "voice-x-e029.safetensors", "bytes": pv.NOT_STORED, "sha256": "d" * 64} \
        and rec["adapters"][0]["sound"]["sha256"] == "f" * 64 and rec["adapters"][0]["music"]["scale"] == 0.45
    assert rec["loudness"]["lufs_i"] == -12.9 and rec["pronunciation"]["stress_marks"] == "present"
    assert rec["genome_sha256"].startswith("sha256:")                 # everything the genotype needs was stored
    assert "C:/Users" not in at.wav_read_provenance(wav)            # the trial's raw_dir is never copied (rule 3)


def test_a_yue2_trial_whose_replay_file_does_not_match_is_tagged_without_tokens_not_with_the_wrong_ones(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "replay.json").write_text(json.dumps({"style": "other", "lyrics": "x", "seed": 1, "lm_seed": 1,
                                                              "semantic_tokens": "1,2,3"}), encoding="utf-8")
    body = {"style": "rock", "lyrics": LYRICS, "cot": "full", "duration": 60.0, "lm_seed": 42, "seed": 42, "steps": 32,
            "lm_batch_size": 1, "synth_batch_size": 1}
    info = {"engine": "yue2.cpp e646d52", "model_file": "m.gguf", "variant": 0, "replay_request_with_tokens":
            str(tmp_path / "raw" / "replay.json")}
    wav = _trial_wav(tmp_path / "y", "song_v0", body=body, info=info)
    (result,) = rt.retag_tree([wav.parent], dry_run=False)
    rec = json.loads(at.wav_read_provenance(wav))
    assert result.status == "tagged" and rec["semantic_tokens"] == "" and "cannot be replayed" in rec["record_notes"][0]
    assert rec["genome_sha256"] == pv.NOT_STORED


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_file_whose_audio_is_not_the_one_its_sidecar_describes_is_refused_and_left_alone(tmp_path):
    wav = _trial_wav(tmp_path / "trial")
    meta = json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))
    meta["info"]["audio_pcm_md5_before"] = "MD5=" + "0" * 32
    wav.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
    before = wav.read_bytes()
    (result,) = rt.retag_tree([wav.parent], dry_run=False)
    assert result.status == "error" and "left alone" in result.reason and wav.read_bytes() == before


def test_a_file_that_has_a_record_is_not_tagged_again_and_derived_copies_and_unreadable_sidecars_are_skipped(tmp_path):
    wav = _trial_wav(tmp_path / "trial")
    assert rt.retag_tree([wav.parent], dry_run=False)[0].status == "tagged"
    once = wav.read_bytes()
    assert rt.retag_tree([wav.parent], dry_run=False)[0].status == "already" and wav.read_bytes() == once
    derived = make_wav(tmp_path / "level-matched-18lufs" / "x_m18lufs.wav") if (tmp_path / "level-matched-18lufs").mkdir() is None else None
    orphan = make_wav(tmp_path / "trial" / "orphan.wav")
    results = {r.file: r for r in rt.retag_tree([tmp_path], dry_run=False)}
    assert results["x_m18lufs.wav"].status == "derived" and derived.read_bytes()[:4] == b"RIFF"
    assert results["orphan.wav"].status == "error" and "no readable sidecar" in results["orphan.wav"].reason
    assert at.wav_read_info(orphan) == {} and at.wav_read_provenance(orphan) is None


def test_a_wav_re_tagged_in_the_old_info_form_is_migrated_verbatim_to_the_id3_chunk(tmp_path):
    wav = _trial_wav(tmp_path / "trial")
    old_record = json.dumps({"schema": "mp.provenance/1", "engine": "acestep.cpp", "seed": 7, "ids": "wide"})
    at.wav_add_info(wav, "DPCP", old_record)                    # how the 55 files of 2026-11-21 were tagged
    pcm, before = _data_md5(wav), wav.read_bytes()
    assert at.wav_read_provenance(wav) == old_record and at.wav_read_id3(wav) == {}
    (dry,) = rt.retag_tree([wav.parent], dry_run=True)
    assert dry.status == "would_migrate" and wav.read_bytes() == before
    (res,) = rt.retag_tree([wav.parent], dry_run=False)
    assert res.status == "migrated"
    assert at.wav_read_id3(wav)["DPC_PROVENANCE"] == old_record          # moved as it was, not rebuilt from the sidecar
    info = at.wav_read_info(wav)
    assert "DPCP" not in info and info["INAM"] == "Song" and _data_md5(wav) == pcm
    once = wav.read_bytes()
    (again,) = rt.retag_tree([wav.parent], dry_run=False)
    assert again.status == "already" and wav.read_bytes() == once
    assert rt.summarize([res])["by_status"] == {"migrated": 1}


def test_a_job_made_flac_gets_its_record_from_its_sidecar_and_the_codes_back_from_a_matching_payload(tmp_path):
    home = tmp_path / "dpc"
    job = "job_0123456789abcdef"
    payload = home / "music" / "jobs" / job / "payload"
    payload.mkdir(parents=True)
    lm_result = json.dumps([{"caption": "chosen", "audio_codes": ",".join(["77"] * 120), "bpm": 100}]).encode()
    lm_request = json.dumps({"caption": "sent", "lyrics": LYRICS, "duration": 30}, ensure_ascii=False).encode()
    (payload / "lm.result.json").write_bytes(lm_result)
    (payload / "lm.request.json").write_bytes(lm_request)
    sums = {"lm.result.json": pv.sha256_of(lm_result), "lm.request.json": pv.sha256_of(lm_request),
            "synth.request.json": "sha256:" + "9" * 64}               # this one's file is gone: not read
    folder = tmp_path / "gen" / job
    folder.mkdir(parents=True)
    wav = make_wav(tmp_path / "w.wav")
    flac = folder / "take_v1.flac"
    fake_flac(wav, flac, {})
    md5 = read_flac_md5(flac)
    side = {"schema": "dpc.take-sidecar/1", "job_id": job, "take_id": "tak_0123456789abcdef", "variant": 1,
            "created_at": "2026-11-20T20:27:34Z", "task_type": "text2music",
            "engine": {"engine": "acestep.cpp", "version": "973517b (2026-11-17)",
                       "binaries": [{"file": "ace-server.exe", "bytes": 1, "sha256": "sha256:" + "1" * 64}],
                       "server_args": {"max_batch": 2, "device": None}},
            "models": {"dit": {"file": "dit.gguf", "bytes": 3, "sha256": "sha256:" + "2" * 64}},
            "payloads": sums, "seed": 42, "variant_seed": 42, "synth_batch_size": 2, "synth_batch_index": 0,
            "inference_steps": 8, "solver": "euler", "lm_seed": 5, "audio_codes_sha256": pv.text_sha256(",".join(["77"] * 120)),
            "codes_count": 120, "duration_requested_s": 30, "duration_received_s": 24.0,
            "output": {"container": "flac", "pcm_md5": md5}}
    flac.with_suffix(".json").write_text(json.dumps(side), encoding="utf-8")
    (result,) = rt.retag_tree([folder], dry_run=False, dpc_home=home)
    assert result.status == "tagged", result.reason
    rec = json.loads(at.flac_read_comments(flac)[pv.TAG])
    assert rec["audio_codes"] == ",".join(["77"] * 120) and rec["caption_sent"] == "sent" and rec["caption_chosen"] == "chosen"
    assert rec["engine_binaries"][0]["sha256"] == "sha256:" + "1" * 64 and rec["job_id"] == job
    assert at.flac_read_comments(flac)["LYRICS"] == LYRICS and rec["output"]["pcm_md5"] == md5
    # the same file with the payload edited since: the codes are not taken, the hash and count stay
    flac2 = folder / "take_v2.flac"
    fake_flac(make_wav(tmp_path / "w2.wav", seconds=0.7), flac2, {})
    side2 = {**side, "output": {"container": "flac", "pcm_md5": read_flac_md5(flac2)},
             "payloads": {**sums, "lm.result.json": "sha256:" + "0" * 64}}
    flac2.with_suffix(".json").write_text(json.dumps(side2), encoding="utf-8")
    assert rt.retag_tree([flac2], dry_run=False, dpc_home=home)[0].status == "tagged"
    rec2 = json.loads(at.flac_read_comments(flac2)[pv.TAG])
    assert "audio_codes" not in rec2 and rec2["audio_codes_sha256"] == side["audio_codes_sha256"] and rec2["codes_count"] == 120
    assert rec2["caption_chosen"] == pv.NOT_STORED


def read_flac_md5(path):
    from dpc_client_core.music.identity import read_flac_streaminfo
    return read_flac_streaminfo(path).md5


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_re_tagging_a_wav_leaves_the_librarys_hash_title_and_duration_the_same(tmp_path):
    wav = _trial_wav(tmp_path / "trial")
    tools = MediaTools.discover()
    before = identify(wav, tools)
    assert before.hash_kind == "audio_stream" and rt.retag_tree([wav.parent], dry_run=False)[0].status == "tagged"
    after = identify(wav, tools)
    assert (after.hash_kind, after.content_hash, after.title, after.duration_s) == \
        (before.hash_kind, before.content_hash, before.title, before.duration_s)
    assert after.size_bytes != before.size_bytes and after.provenance["engine"] == "acestep.cpp"


def test_the_command_line_tool_reports_and_honours_dry_run(tmp_path):
    _trial_wav(tmp_path / "trial")
    tool = HERE.parents[3] / "tools" / "retag_music_provenance.py"
    run = lambda *a: subprocess.run([sys.executable, str(tool), *a, "--json", "--dpc-home", str(tmp_path)],
                                    capture_output=True, text=True, encoding="utf-8", cwd=HERE.parents[1],
                                    env={**__import__("os").environ, "PYTHONUTF8": "1"})
    dry = run("--dry-run", str(tmp_path / "trial"))
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry.stdout)["summary"]["by_status"] == {"would_tag": 1}
    assert at.wav_read_provenance(tmp_path / "trial" / "turbo_v1.wav") is None
    real = run(str(tmp_path / "trial"))
    assert json.loads(real.stdout)["summary"]["by_status"] == {"tagged": 1}
    assert at.wav_read_provenance(tmp_path / "trial" / "turbo_v1.wav")
