"""`mp.provenance/1` (`music/provenance.py`, PROVENANCE §2-§4): the builder per engine from
fake payloads (sent versus chosen, the codes in full, the hidden genes, the weights by hash,
no paths), the privacy guard, the 64 KB size cap with its hash-reference fallback, the genome
and render ids, and the defensive reader."""

import getpass
import json
from pathlib import Path

import pytest

from dpc_client_core.music import provenance as pv

CODES = ",".join(str(1000 + i) for i in range(900))        # about 3 minutes of acestep codes
TOKENS = ",".join(str(20000 + i % 5000) for i in range(6000))
SHA = "sha256:" + "a" * 64

ACE_IDENTITY = {
    "engine": "acestep.cpp", "version": "973517b (2026-11-17)",
    "binaries": [{"file": "ace-server.exe", "bytes": 10, "sha256": SHA}, {"file": "ggml-cuda.dll", "bytes": 20, "sha256": SHA}],
    "server_args": {"max_batch": 2, "device": None},
    "models": {r: {"file": f"{r}.gguf", "bytes": 5, "sha256": SHA} for r in ("lm", "dit", "text_encoder", "vae")},
}
YUE_IDENTITY = {
    "engine": "yue2.cpp", "version": "e646d52 (2026-11-17)", "commit": "e646d52",
    "binaries": [{"file": "yue-server.exe", "bytes": 10, "sha256": SHA}],
    "server_args": {"max_batch": 1, "device": "CUDA0"},
    "models": {"backbone": {"file": "YuE2-3B-Q8_0.gguf", "bytes": 5, "sha256": SHA, "choice": "stock"},
               "vae": {"file": "YuE2-Vae-F32.gguf", "bytes": 5, "sha256": SHA}},
    "adapters": [{"name": "voice-ru-m-bass-82", "source": {"file": "v.safetensors", "bytes": 9, "sha256": SHA},
                  "music": {"file": "m.safetensors", "bytes": 4, "sha256": SHA, "scale": 0.45},
                  "sound": {"file": "s.safetensors", "bytes": 4, "sha256": SHA, "scale": 0.35}}],
}
FACTS = dict(
    created_at="2026-11-21T10:00:00Z", duration_received_s=29.6, loudness={"lufs_i": -12.0, "lra": 3.0,
                                                                         "true_peak_dbtp": -1.0},
    device={"gpu": "Test GPU", "driver": "1.2"},
    engine_output={"format": "wav24", "sha256": SHA, "pcm_md5": "0" * 31 + "1", "kept": False},
    postprocess={"step": "wav24_to_flac", "tool": "ffmpeg", "cmd_as_sent": ["ffmpeg", "-i", "a.wav.part", "a.flac.part"]},
    output={"container": "flac", "pcm_md5": "0" * 31 + "1"},
)
LYRICS = "[verse]\nТы́сячи умов, один огонь\n"


def acestep(**over):
    kw = dict(
        job_id="job_0123456789abcdef", variant=2, params={"lyrics": LYRICS, "duration_s": 30, "lyrics_prep": {"vocal_language": "ru"}},
        identity=ACE_IDENTITY,
        lm_request={"caption": "indie rock", "lyrics": LYRICS, "duration": 30, "vocal_language": "ru", "seed": 42,
                    "lm_model": "lm.gguf"},
        lm_result={"caption": "An indie rock track with guitars", "bpm": 110, "keyscale": "E minor", "timesignature": "4",
                   "vocal_language": "ru", "lyrics": LYRICS, "audio_codes": CODES, "lm_seed": 876362762,
                   "task_type": "text2music"},
        synth_request={"inference_steps": 8, "solver": "euler", "synth_model": "dit.gguf", "vae": "vae.gguf",
                       "task_type": "text2music"},
        seed=42, batch_size=2, batch_index=1, window_s=180.0, payloads={"lm.result.json": SHA}, **FACTS)
    kw.update(over)
    return pv.build_acestep_record(**kw)


def yue2(**over):
    kw = dict(
        job_id="job_0123456789abcdef", variant=1, params={"lyrics": LYRICS, "duration_s": 120}, identity=YUE_IDENTITY,
        sent={"style": "rock", "lyrics": LYRICS, "cot": "full", "steps": 32, "peak_clip": 10, "lm_batch_size": 1},
        replay={"style": "rock", "abc": "X:1\nT:\nK:C\n", "semantic_tokens": TOKENS, "seed": 42, "lm_seed": 42},
        seed=42, variant_seed=42, lm_seed=42, backbone="stock", payloads={"synth.v1.result.json": SHA}, **FACTS)
    kw.update(over)
    return pv.build_yue2_record(**kw)


# -- the builder per engine -------------------------------------------------------------------


def test_an_acestep_record_keeps_sent_and_chosen_apart_and_the_codes_in_full():
    rec = acestep()
    assert rec["schema"] == "mp.provenance/1" and rec["engine"] == "acestep.cpp"
    assert (rec["engine_version"], rec["engine_commit"]) == ("973517b (2026-11-17)", "973517b")
    assert rec["caption_sent"] == "indie rock" and rec["caption_chosen"] == "An indie rock track with guitars"
    assert rec["bpm_sent"] is None and rec["bpm_chosen"] == 110      # DPC sent no bpm; the LM chose one
    assert rec["lyrics"] == LYRICS and "\u0301" in rec["lyrics"]       # exactly as sent, stress mark kept
    assert rec["pronunciation"] == {"stress_marks": "present", "count": 1}
    assert rec["audio_codes"] == CODES and rec["codes_count"] == 900
    assert rec["audio_codes_sha256"] == pv.text_sha256(CODES)
    assert rec["task_type"] == "text2music" and rec["lm_seed"] == 876362762
    assert (rec["seed"], rec["variant_seed"], rec["synth_batch_size"], rec["synth_batch_index"]) == (42, 43, 2, 1)
    assert rec["solver"] == "euler" and rec["inference_steps"] == 8
    assert rec["guidance_scale"] == pv.ENGINE_DEFAULT and rec["shift"] == pv.ENGINE_DEFAULT   # not sent: engine decides
    assert rec["engine_window_s"] == 180.0 and rec["duration_received_s"] == 29.6
    assert rec["loudness"]["lufs_i"] == -12.0
    assert {m["role"] for m in rec["models"]} == {"lm", "dit", "text_encoder", "vae"}
    assert all(m["sha256"] == SHA and m["bytes"] == 5 and "/" not in m["file"] for m in rec["models"])
    assert rec["engine_binaries"][0] == {"file": "ace-server.exe", "sha256": SHA, "bytes": 10}
    assert rec["device"] == {"gpu": "Test GPU", "driver": "1.2"} and rec["build"] == pv.NOT_STORED
    assert "same synth_batch_size" in rec["reproducible_within"]
    assert rec["relation"] == "song_from_own_text" and rec["parents"] == []
    assert rec["pcm_md5"] == rec["output"]["pcm_md5"] and "ffmpeg" in rec["dpc_postprocess"]["tool"]
    assert rec["instrumental"] is False


def test_an_instrumental_acestep_take_says_so_and_the_marker_sent_stays_as_sent():
    rec = acestep(params={"lyrics": None, "duration_s": 30},
                  lm_request={"caption": "ambient", "lyrics": "[Instrumental]", "duration": 30})
    assert rec["instrumental"] is True and rec["lyrics"] == "[Instrumental]"
    assert rec["pronunciation"] == {"stress_marks": "none_sent", "count": 0}


def test_a_yue2_record_carries_the_score_the_semantic_tokens_adapters_and_batch_one():
    rec = yue2()
    assert rec["engine"] == "yue2.cpp" and rec["engine_commit"] == "e646d52" and rec["backend"] == "CUDA0"
    assert rec["caption_sent"] == "rock" and rec["lyrics"] == LYRICS
    assert rec["score_abc"].startswith("X:1") and rec["score_abc_sha256"] == pv.text_sha256(rec["score_abc"])
    assert rec["semantic_tokens"] == TOKENS and rec["semantic_tokens_count"] == 6000
    assert rec["semantic_tokens_sha256"] == pv.text_sha256(TOKENS)
    assert (rec["synth_batch_size"], rec["synth_batch_index"], rec["lm_batch_size"]) == (1, 0, 1)
    assert rec["cot"] == "full" and rec["inference_steps"] == 32 and rec["lm_seed"] == 42
    assert rec["adapters"][0]["music"]["scale"] == 0.45 and rec["adapters"][0]["sound"]["sha256"] == SHA
    backbone = next(m for m in rec["models"] if m["role"] == "backbone")
    assert backbone["choice"] == "stock" and backbone["sha256"] == SHA


def test_a_yue2_take_whose_engine_gave_no_tokens_says_it_cannot_be_replayed():
    rec = yue2(replay={})
    assert rec["semantic_tokens_sha256"] == pv.NOT_STORED and "cannot be replayed" in rec["record_notes"][0]
    pv.add_ids(rec)
    assert rec["genome_sha256"] == pv.NOT_STORED          # no half-known genome id


# -- ids ------------------------------------------------------------------------------------------


def test_the_genome_id_ignores_lm_seed_and_the_render_id_follows_the_substrate():
    a = acestep()
    b = acestep(lm_result={"caption": "An indie rock track with guitars", "bpm": 110, "keyscale": "E minor",
                           "timesignature": "4", "vocal_language": "ru", "lyrics": LYRICS, "audio_codes": CODES,
                           "lm_seed": 1, "task_type": "text2music"})
    c = acestep(seed=7)
    for r in (a, b, c):
        pv.add_ids(r)
    assert a["lm_seed"] != b["lm_seed"] and a["genome_sha256"] == b["genome_sha256"]       # diagnostic only
    assert a["render_sha256"] == b["render_sha256"]
    assert c["genome_sha256"] == a["genome_sha256"] and c["render_sha256"] != a["render_sha256"]   # seed is substrate
    assert acestep(batch_index=0)["synth_batch_index"] == 0
    d = acestep(batch_index=0)
    pv.add_ids(d)
    assert d["render_sha256"] != a["render_sha256"]                                         # a hidden gene
    assert all(x.startswith("sha256:") and len(x) == 71 for x in (a["genome_sha256"], a["render_sha256"]))


# -- privacy --------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    '{"x":"C:\\\\Users\\\\someone\\\\music\\\\a.wav"}', '{"x":"c:/models/a.gguf"}', '{"x":"\\\\\\\\server\\\\share\\\\a"}',
    '{"x":"/home/someone/a.wav"}', '{"x":"opened /Users/someone/a.wav today"}', '{"x":"seen from 192.168.1.20"}',
    '{"x":"Bearer abcdefghijklmnop12345"}', '{"x":"sk-abcdefghijklmnopqrstuvwx"}', '{"api_key":"abcdef123456"}',
    '{"x":"2001:0db8:85a3:0000:0000:8a2e:0370:7334"}',
])
def test_the_guard_refuses_paths_addresses_and_tokens(text):
    with pytest.raises(pv.PrivacyError):
        pv.check_privacy(text)


def test_the_guard_refuses_the_home_folder_and_the_user_name(monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/srv/people/zebulonq")))
    monkeypatch.setattr(getpass, "getuser", lambda: "zebulonq")
    with pytest.raises(pv.PrivacyError):
        pv.check_privacy('{"x":"made by zebulonq"}')
    with pytest.raises(pv.PrivacyError):
        pv.check_privacy('{"x":"/srv/people/zebulonq"}')
    pv.check_privacy('{"x":"made by someone else"}')


def test_ordinary_lyrics_captions_hashes_and_numbers_pass_the_guard():
    rec = acestep()
    pv.check_privacy(pv.compact_json(rec))
    pv.check_privacy(pv.compact_json({"lyrics": "Verse 1: город спит\nВ 3:30 ночи, 4/4, 120.5 bpm\nhttp://example.com/a"}))


def test_a_record_with_a_local_path_is_refused_and_never_returned():
    rec = acestep(params={"lyrics": "see C:\\Users\\x\\a.wav", "duration_s": 30})
    rec["lyrics"] = "see C:\\Users\\x\\a.wav"
    with pytest.raises(pv.PrivacyError):
        pv.finalize(rec)
    # the same text with the path reduced to a name is fine
    rec["lyrics"] = "see a.wav"
    assert pv.finalize(rec)[0]["lyrics"] == "see a.wav"


# -- size ----------------------------------------------------------------------------------------


def test_typical_records_fit_and_keep_their_codes_in_full():
    for rec in (acestep(), yue2()):
        done, text = pv.finalize(rec)
        assert len(text.encode("utf-8")) <= pv.MAX_BYTES and "omitted_for_size" not in done
        print(done["engine"], len(text.encode("utf-8")))


def test_an_oversized_record_replaces_codes_tokens_and_score_by_hashes_in_order_and_keeps_its_ids():
    big = ",".join(str(10000 + i % 9000) for i in range(30000))        # about 150 KB of tokens
    small = yue2()
    pv.add_ids(small)
    rec = yue2(replay={"style": "rock", "abc": "X:1\n" + "K:C\n" * 50, "semantic_tokens": big})
    done, text = pv.finalize(rec)
    assert len(text.encode("utf-8")) <= pv.MAX_BYTES
    ref = done["omitted_for_size"]["semantic_tokens"]
    assert "semantic_tokens" not in done and ref["sha256"] == pv.text_sha256(big) and ref["count"] == 30000
    assert ref["payload"] == "synth.v1.result.json" and ref["payload_sha256"] == SHA and ref["bytes"] > 100000
    assert done["semantic_tokens_sha256"] == pv.text_sha256(big)      # the hash field stays
    assert "score_abc" in done                                        # only as much as needed gives way
    # the ids were computed from the hashes, so they are what the full record would have had
    full = yue2(replay={"style": "rock", "abc": "X:1\n" + "K:C\n" * 50, "semantic_tokens": big})
    pv.add_ids(full)
    assert done["genome_sha256"] == full["genome_sha256"] and done["render_sha256"] == full["render_sha256"]


def test_acestep_codes_give_way_with_their_payload_reference():
    rec = acestep(lm_result={"caption": "c", "audio_codes": ",".join(["12345"] * 30000), "lyrics": LYRICS})
    done, _text = pv.finalize(rec)
    ref = done["omitted_for_size"]["audio_codes"]
    assert "audio_codes" not in done and ref["payload"] == "lm.result.json" and ref["count"] == 30000


def test_a_record_too_big_even_without_the_codes_is_refused():
    rec = acestep(params={"lyrics": "la " * 40000, "duration_s": 30}, lm_request={"caption": "c", "lyrics": "la " * 40000})
    with pytest.raises(pv.ProvenanceError):
        pv.finalize(rec)


# -- reading -------------------------------------------------------------------------------------


def test_the_reader_summarises_a_good_record_and_never_raises_on_a_bad_one(caplog):
    good, text = pv.finalize(acestep())
    summary = pv.read_summary(text, where="a.flac")
    assert summary["engine"] == "acestep.cpp" and summary["seed"] == 42 and summary["has_codes"] is True
    assert summary["genome_sha256"] == good["genome_sha256"] and "path" not in json.dumps(summary)
    for bad in ("not json", "[]", '{"schema":"mp.provenance/2","engine":"x"}', '{"schema":"mp.provenance/1"}',
                '{"schema":"mp.provenance/1","engine":"a/../b"}',
                '{"schema":"mp.provenance/1","engine":"x","seed":"abc"}',
                '{"schema":"mp.provenance/1","engine":"x","audio_codes":"1,2,<script>"}',
                '{"schema":"mp.provenance/1","engine":"x","models":{"a":1}}',
                '{"schema":"mp.provenance/1","engine":"x","genome_sha256":"nope"}', "x" * 10, 5, "[" * 100000):
        rec, why = pv.parse_embedded(bad)
        assert rec is None and (why or bad in ("", None))
    assert pv.parse_embedded(None) == (None, None) and pv.parse_embedded("  ") == (None, None)
    with caplog.at_level("INFO"):
        assert pv.read_summary("not json", where="b.flac") is None
    assert "ignoring the provenance record in b.flac" in caplog.text


def test_the_reader_refuses_an_oversized_record_without_parsing_it():
    huge = '{"schema":"mp.provenance/1","engine":"x","pad":"' + "a" * (pv.MAX_BYTES + 10) + '"}'
    rec, why = pv.parse_embedded(huge)
    assert rec is None and "over the" in why


def test_a_record_without_codes_by_hash_is_summarised_as_such():
    rec = acestep(lm_result={"caption": "c", "audio_codes": ",".join(["12345"] * 30000), "lyrics": LYRICS})
    done, text = pv.finalize(rec)
    summary = pv.read_summary(text)
    assert summary["has_codes"] is False and summary["codes_by_hash_only"] is True
