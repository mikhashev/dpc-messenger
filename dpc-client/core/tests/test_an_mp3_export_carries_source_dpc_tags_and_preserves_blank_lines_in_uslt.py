"""MP3 and Opus exports carry safe source `DPC_*` tags and preserve blank lines between
sections in `USLT` / `LYRICS` (Card AN-MP3-EXPORT-CARRIES-NO-DPC-SEED-OR-JOB-ID-FRAMES-AND-ITS-USLT-DROPS-THE-BLANK-LINES-BETWEEN-SECTIONS).

Generated FLAC takes write `DPC_SEED`, `DPC_LM_SEED`, `DPC_JOB_ID`, `DPC_ENGINE`,
`DPC_BACKBONE`, `DPC_VARIANT`, `DPC_SYNTH_BATCH_SIZE`, `DPC_SYNTH_BATCH_INDEX`, etc. as
Vorbis comments (and `mp.provenance/1` also records `engine_version`, `lm_seed`, etc.).
Lossy exports must carry those safe `DPC_*` tags into ID3v2.4 `TXXX` frames (and Opus
Vorbis comments) without overwriting export-own tags or leaking node-local secrets such as
`node_id` or `conversation_id`, and `USLT` must preserve blank lines between lyrics sections
even when the library's cached `plain_lines` store stripped them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dpc_client_core.music import audio_tags as at
from dpc_client_core.music import lyrics as lyrics_mod
from dpc_client_core.music import provenance as pv
from dpc_client_core.music import export as export_mod
from dpc_client_core.music.export import (
    ExportSource,
    MusicExporter,
    read_id3_txxx,
    read_opus_comments,
)
from dpc_client_core.music.id3 import id3v24_frames
from dpc_client_core.music.identity import MediaTools
from tests._acestep_helpers import fake_flac
from tests.test_a_flac_or_wav_gets_its_provenance_once_and_its_audio_is_unchanged import make_wav
from tests.test_the_lossy_export_has_an_opus_preset_and_a_true_peak_switch import fake_ogg_opus
from tests.test_the_provenance_record_is_built_per_engine_and_refuses_private_or_oversized_content import (
    acestep,
    yue2,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
HAVE_LAME = bool(FFMPEG) and "libmp3lame" in (
    subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if FFMPEG else ""
)

MULTI_SECTION_LYRICS = (
    "[Verse 1]\n"
    "First line of the verse\n"
    "Second line of the verse\n"
    "\n"
    "[Chorus]\n"
    "Shout it out loud\n"
    "Keep the rhythm alive\n"
    "\n"
    "[Bridge]\n"
    "Quiet before the storm\n"
    "\n"
    "[Outro]\n"
    "Fade into the night\n"
)


class _FakeFfmpeg:
    def __call__(self, cmd, *, timeout):
        cmd = list(cmd)
        if "-encoders" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                b" A....D libmp3lame  libmp3lame MP3\n A....D libopus  libopus Opus\n",
                b"",
            )
        if "-version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, b"ffmpeg version 9.9-test Copyright (c) x\n", b"")
        out = Path(cmd[-1])
        out.write_bytes(fake_ogg_opus() if "libopus" in cmd else b"\xff\xfb\x90\x00" + b"audio" * 50)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


def _stripped_library_lyrics(raw_lyrics: str) -> str:
    """Mirror `lyrics.plain_lines` + `commands._music_export_source` stripping blank lines."""
    lines = [str(item.get("text") or "") for item in lyrics_mod.plain_lines(raw_lyrics)]
    return "\n".join(lines).strip()


def _parse_uslt_texts(mp3_path: Path) -> list[tuple[str, str]]:
    """Decode `(lang, text)` from every `USLT` frame in an ID3v2.4 tag using `id3.py`."""
    frames = id3v24_frames(mp3_path.read_bytes())
    assert frames is not None
    out: list[tuple[str, str]] = []
    for raw in frames:
        if raw[:4] != b"USLT":
            continue
        payload = raw[10:]
        enc = payload[0]
        assert enc == 3
        lang = payload[1:4].decode("ascii", errors="replace")
        rest = payload[4:]
        cut = rest.find(b"\x00")
        assert cut >= 0
        text = rest[cut + 1:].decode("utf-8")
        out.append((lang, text))
    return out


def _build_flac(tmp_path: Path, comments: dict[str, str], name: str = "take_v1.flac") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    wav = make_wav(tmp_path / f"{name}.wav")
    flac = tmp_path / "lib" / name
    flac.parent.mkdir(parents=True, exist_ok=True)
    fake_flac(wav, flac, {})
    at.flac_add_comments(flac, comments, once_key="")
    return flac


def _exporter(tmp_path: Path, *, ffprobe: str | None = None, runner=None) -> MusicExporter:
    tools = MediaTools(ffmpeg="ffmpeg", ffprobe=ffprobe, runner=runner or _FakeFfmpeg())
    return MusicExporter(tmp_path / "out", tools=tools, measure=lambda _p: {"true_peak_dbtp": -1.5})


def test_generated_yue2_flac_take_round_trips_dpc_tags_and_multiline_lyrics_byte_for_byte(tmp_path):
    rec = yue2(
        job_id="job_a606685a83a10faa",
        seed=3555429983,
        variant_seed=3555429983,
        lm_seed=3555429983,
        sent={"style": "pop-punk", "lyrics": MULTI_SECTION_LYRICS, "cot": "full", "steps": 32, "peak_clip": 10},
        replay={"style": "pop-punk", "abc": "X:1\nT:\nK:C\n", "semantic_tokens": "1,2,3", "seed": 3555429983, "lm_seed": 3555429983},
    )
    rec["node_id"] = "dpc-node-secret12345"
    rec, rec_text = pv.finalize(rec)
    flac = _build_flac(
        tmp_path,
        {
            "TITLE": "Generated Track",
            "ARTIST": "DPC (yue2.cpp)",
            "DPC_JOB_ID": "job_a606685a83a10faa",
            "DPC_VARIANT": "1",
            "DPC_ENGINE": "yue2.cpp",
            "DPC_SEED": "3555429983",
            "DPC_LM_SEED": "3555429983",
            "DPC_BACKBONE": "stock",
            "DPC_SYNTH_BATCH_SIZE": "1",
            "DPC_SYNTH_BATCH_INDEX": "0",
            "LYRICS": MULTI_SECTION_LYRICS,
            "UNSYNCEDLYRICS": MULTI_SECTION_LYRICS,
            pv.TAG: rec_text,
        },
    )
    library_lyrics = _stripped_library_lyrics(MULTI_SECTION_LYRICS)
    assert len(library_lyrics) < len(MULTI_SECTION_LYRICS)

    source = ExportSource(
        track_id="trk_0123456789abcdef",
        path=flac,
        content_hash="md5:" + "ab" * 16,
        title="Generated Track",
        artist="DPC (yue2.cpp)",
        lyrics=library_lyrics,
        lyrics_language="eng",
        true_peak_dbtp=-2.0,
        true_peak_basis="test",
        take_record={**rec, "take_id": "tak_0123456789abcdef", "library_track_id": "trk_0123456789abcdef"},
        origin="generated",
    )
    mp3 = Path(_exporter(tmp_path).export_track(source, "mp3-v0")["path"])
    txxx = read_id3_txxx(mp3)

    assert txxx["DPC_SEED"] == "3555429983"
    assert txxx["DPC_LM_SEED"] == "3555429983"
    assert txxx["DPC_JOB_ID"] == "job_a606685a83a10faa"
    assert txxx["DPC_ENGINE"] == "yue2.cpp"
    assert txxx["DPC_ENGINE_VERSION"] == "e646d52 (2026-11-17)"
    assert txxx["DPC_BACKBONE"] == "stock"
    assert txxx["DPC_VARIANT"] == "1"
    assert txxx["DPC_SYNTH_BATCH_SIZE"] == "1"
    assert txxx["DPC_SYNTH_BATCH_INDEX"] == "0"

    assert txxx["DPC_EXPORT_OF"] == source.content_hash
    assert txxx["DPC_EXPORT_PRESET"] == "mp3-v0"
    provenance = json.loads(txxx["DPC_PROVENANCE"])
    assert provenance["source_record"]["node_id"] == pv.NODE_ID_WITHHELD
    assert provenance["take"] == {
        "schema": pv.SCHEMA,
        "take_id": "tak_0123456789abcdef",
        "library_track_id": "trk_0123456789abcdef",
    }
    assert b"dpc-node-secret12345" not in mp3.read_bytes()

    uslt = _parse_uslt_texts(mp3)
    assert uslt == [("eng", MULTI_SECTION_LYRICS)]


def test_instrumental_generated_take_copies_dpc_tags_without_uslt(tmp_path):
    rec, rec_text = pv.finalize(
        acestep(
            job_id="job_inst0011223344",
            seed=777,
            params={"lyrics": None, "duration_s": 30},
            lm_request={"caption": "ambient", "lyrics": "[Instrumental]", "duration": 30},
            lm_result={"caption": "ambient", "audio_codes": "10,20", "lyrics": "[Instrumental]", "lm_seed": 888},
        )
    )
    flac = _build_flac(
        tmp_path,
        {"DPC_JOB_ID": "job_inst0011223344", "DPC_SEED": "777", pv.TAG: rec_text},
        name="inst.flac",
    )
    mp3 = Path(
        _exporter(tmp_path).export_track(
            ExportSource(
                track_id="trk_inst",
                path=flac,
                content_hash="md5:" + "55" * 16,
                title="Ambient",
                true_peak_dbtp=-2.0,
            ),
            "mp3-v0",
        )["path"]
    )
    txxx = read_id3_txxx(mp3)
    assert txxx["DPC_JOB_ID"] == "job_inst0011223344"
    assert txxx["DPC_SEED"] == "777"
    assert txxx["DPC_LM_SEED"] == "888"
    assert txxx["DPC_ENGINE"] == "acestep.cpp"
    assert _parse_uslt_texts(mp3) == []


def test_acestep_mp3_and_opus_exports_copy_vorbis_and_source_record_dpc_tags(tmp_path):
    rec = acestep(
        job_id="job_1122334455667788",
        seed=42,
        batch_size=2,
        batch_index=1,
        lm_request={"caption": "indie rock", "lyrics": MULTI_SECTION_LYRICS, "duration": 30, "vocal_language": "en"},
        lm_result={"caption": "indie rock", "audio_codes": "1000,1001", "lyrics": MULTI_SECTION_LYRICS, "lm_seed": 876362762},
    )
    rec, rec_text = pv.finalize(rec)
    flac = _build_flac(
        tmp_path,
        {
            "DPC_JOB_ID": "job_1122334455667788",
            "DPC_VARIANT": "2",
            "DPC_SEED": "42",
            "DPC_SYNTH_BATCH_SIZE": "2",
            "DPC_SYNTH_BATCH_INDEX": "1",
            "LYRICS": MULTI_SECTION_LYRICS,
            pv.TAG: rec_text,
        },
    )
    source = ExportSource(
        track_id="trk_ace",
        path=flac,
        content_hash="md5:" + "cd" * 16,
        title="Ace Song",
        artist="DPC (acestep.cpp)",
        lyrics=_stripped_library_lyrics(MULTI_SECTION_LYRICS),
        true_peak_dbtp=-2.0,
        true_peak_basis="test",
        origin="generated",
    )
    exporter = _exporter(tmp_path)
    mp3 = Path(exporter.export_track(source, "mp3-v0")["path"])
    opus = Path(exporter.export_track(source, "opus-160")["path"])

    mp3_txxx = read_id3_txxx(mp3)
    opus_tags = read_opus_comments(opus)
    for tag_map in (mp3_txxx, opus_tags):
        assert tag_map["DPC_JOB_ID"] == "job_1122334455667788"
        assert tag_map["DPC_VARIANT"] == "2"
        assert tag_map["DPC_SEED"] == "42"
        assert tag_map["DPC_LM_SEED"] == "876362762"
        assert tag_map["DPC_ENGINE"] == "acestep.cpp"
        assert tag_map["DPC_ENGINE_VERSION"] == "973517b (2026-11-17)"
        assert tag_map["DPC_SYNTH_BATCH_SIZE"] == "2"
        assert tag_map["DPC_SYNTH_BATCH_INDEX"] == "1"

    assert _parse_uslt_texts(mp3) == [("xxx", MULTI_SECTION_LYRICS)]
    assert opus_tags["LYRICS"] == MULTI_SECTION_LYRICS


def test_edit_and_cover_tags_copy_while_export_own_and_private_tags_are_withheld(tmp_path):
    flac = _build_flac(
        tmp_path,
        {
            "DPC_JOB_ID": "job_edit_1",
            "DPC_SEED": "99",
            "DPC_EDIT_ACTION": "regenerate_region",
            "DPC_EDIT_OF": "md5:deadbeef",
            "DPC_EDIT_REGION": "10.0-20.0",
            "DPC_COVER_OF": "md5:cafebabe",
            # Attempted spoofing of export-own tags:
            "DPC_EXPORT_OF": "md5:evil",
            "DPC_EXPORT_PRESET": "mp3-320",
            "DPC_EXPORT_TP_LIMIT": "off",
            # Node-private, secret, and local-path tags that must never leak:
            "DPC_NODE_ID": "dpc-node-private001",
            "DPC_CONVERSATION_ID": "conv_secret_chat_1",
            "DPC_TURN_ID": "20261126T110533Z-41c8aba5",
            "DPC_AGENT_ID": "agent_forge",
            "DPC_API_KEY": "sk-secretkey1234567890",
            "DPC_RAW_DIR": "C:\\Users\\alice\\secret",
            "DPC_CUSTOM_PATH": "/home/alice/secret.wav",
            "DPC_OPAQUE_NODE": "dpc-node-hidden999",
        },
    )
    source = ExportSource(
        track_id="trk_priv",
        path=flac,
        content_hash="md5:" + "ef" * 16,
        title="Edited",
        artist="Band",
        true_peak_dbtp=-2.0,
        true_peak_basis="test",
    )
    exporter = _exporter(tmp_path)
    mp3 = Path(exporter.export_track(source, "mp3-v0", limit_true_peak=True)["path"])
    opus = Path(exporter.export_track(source, "opus-160", limit_true_peak=True)["path"])

    mp3_txxx = read_id3_txxx(mp3)
    opus_tags = read_opus_comments(opus)
    for tag_map, preset in ((mp3_txxx, "mp3-v0"), (opus_tags, "opus-160")):
        assert tag_map["DPC_JOB_ID"] == "job_edit_1"
        assert tag_map["DPC_SEED"] == "99"
        assert tag_map["DPC_EDIT_ACTION"] == "regenerate_region"
        assert tag_map["DPC_EDIT_OF"] == "md5:deadbeef"
        assert tag_map["DPC_EDIT_REGION"] == "10.0-20.0"
        assert tag_map["DPC_COVER_OF"] == "md5:cafebabe"

        assert tag_map["DPC_EXPORT_OF"] == source.content_hash
        assert tag_map["DPC_EXPORT_PRESET"] == preset
        assert "DPC_EXPORT_TP_LIMIT" not in tag_map

        for forbidden in (
            "DPC_NODE_ID",
            "DPC_CONVERSATION_ID",
            "DPC_TURN_ID",
            "DPC_AGENT_ID",
            "DPC_API_KEY",
            "DPC_RAW_DIR",
            "DPC_CUSTOM_PATH",
            "DPC_OPAQUE_NODE",
        ):
            assert forbidden not in tag_map

    mp3_bytes = mp3.read_bytes()
    opus_bytes = opus.read_bytes()
    for secret in (
        b"dpc-node-private001",
        b"conv_secret_chat_1",
        b"20261126T110533Z-41c8aba5",
        b"agent_forge",
        b"sk-secretkey1234567890",
        b"C:\\Users\\alice",
        b"/home/alice",
        b"dpc-node-hidden999",
    ):
        assert secret not in mp3_bytes
        assert secret not in opus_bytes


def test_manual_library_edits_synced_lrc_and_tag_fallback_precedence(tmp_path):
    rec, rec_text = pv.finalize(
        acestep(
            lm_request={"caption": "rock", "lyrics": MULTI_SECTION_LYRICS, "duration": 30},
            lm_result={"caption": "rock", "audio_codes": "1,2", "lyrics": MULTI_SECTION_LYRICS},
        )
    )
    flac = _build_flac(tmp_path, {"LYRICS": MULTI_SECTION_LYRICS, pv.TAG: rec_text}, name="edited.flac")

    # Manual library edit with different text must NOT be clobbered by source_record.lyrics.
    edited = _exporter(tmp_path / "m").export_track(
        ExportSource(
            track_id="trk_m",
            path=flac,
            content_hash="md5:" + "11" * 16,
            title="Edited Lyrics",
            lyrics="[Verse 1]\nUser edited line\n[Chorus]\nCustom words",
            lyrics_language="eng",
            true_peak_dbtp=-2.0,
        )
    )
    assert _parse_uslt_texts(Path(edited["path"])) == [("eng", "[Verse 1]\nUser edited line\n[Chorus]\nCustom words")]

    # Explicit synced LRC text must NOT be clobbered either.
    synced_lrc = "[00:01.00] First line of the verse\n[00:05.00] Second line of the verse"
    synced = _exporter(tmp_path / "s").export_track(
        ExportSource(
            track_id="trk_s",
            path=flac,
            content_hash="md5:" + "22" * 16,
            title="Synced Lyrics",
            lyrics=synced_lrc,
            true_peak_dbtp=-2.0,
        )
    )
    assert _parse_uslt_texts(Path(synced["path"])) == [("xxx", synced_lrc)]

    # Non-generated FLAC without source_record whose own LYRICS tag preserves blank lines.
    plain_flac = _build_flac(tmp_path / "p", {"LYRICS": MULTI_SECTION_LYRICS}, name="plain.flac")
    restored = _exporter(tmp_path / "p_out").export_track(
        ExportSource(
            track_id="trk_p",
            path=plain_flac,
            content_hash="md5:" + "33" * 16,
            title="Plain Track",
            lyrics=_stripped_library_lyrics(MULTI_SECTION_LYRICS),
            true_peak_dbtp=-2.0,
        )
    )
    assert _parse_uslt_texts(Path(restored["path"])) == [("xxx", MULTI_SECTION_LYRICS)]


@pytest.mark.skipif(not (HAVE_LAME and FFPROBE), reason="ffmpeg with libmp3lame and ffprobe are required")
def test_real_ffmpeg_and_ffprobe_round_trip_dpc_tags_and_uslt_blank_lines(tmp_path):
    flac = tmp_path / "lib" / "real_take.flac"
    flac.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2:sample_rate=44100",
            "-ac",
            "2",
            "-c:a",
            "flac",
            "-sample_fmt",
            "s16",
            "-metadata",
            "TITLE=Real Take",
            "-metadata",
            "ARTIST=DPC (yue2.cpp)",
            "-metadata",
            "DPC_JOB_ID=job_real1234",
            "-metadata",
            "DPC_SEED=3555429983",
            "-metadata",
            "DPC_LM_SEED=3555429983",
            "-metadata",
            "DPC_ENGINE=yue2.cpp",
            str(flac),
        ],
        check=True,
    )
    rec = yue2(
        job_id="job_real1234",
        seed=3555429983,
        variant_seed=3555429983,
        lm_seed=3555429983,
        sent={"style": "indie", "lyrics": MULTI_SECTION_LYRICS, "cot": "full", "steps": 32, "peak_clip": 10},
        replay={"style": "indie", "abc": "X:1\n", "semantic_tokens": "1,2,3", "seed": 3555429983, "lm_seed": 3555429983},
    )
    _rec, rec_text = pv.finalize(rec)
    at.flac_add_comments(flac, {"LYRICS": MULTI_SECTION_LYRICS, pv.TAG: rec_text})

    exporter = MusicExporter(tmp_path / "out")
    mp3 = Path(
        exporter.export_track(
            ExportSource(
                track_id="trk_real",
                path=flac,
                content_hash="md5:" + "44" * 16,
                title="Real Take",
                artist="DPC (yue2.cpp)",
                lyrics=_stripped_library_lyrics(MULTI_SECTION_LYRICS),
                origin="generated",
            ),
            "mp3-v0",
        )["path"]
    )

    txxx = read_id3_txxx(mp3)
    assert txxx["DPC_SEED"] == "3555429983"
    assert txxx["DPC_LM_SEED"] == "3555429983"
    assert txxx["DPC_JOB_ID"] == "job_real1234"
    assert txxx["DPC_ENGINE"] == "yue2.cpp"
    assert txxx["DPC_ENGINE_VERSION"] == "e646d52 (2026-11-17)"
    assert txxx["DPC_EXPORT_OF"] == "md5:" + "44" * 16
    assert txxx["DPC_EXPORT_PRESET"] == "mp3-v0"
    assert _parse_uslt_texts(mp3) == [("xxx", MULTI_SECTION_LYRICS)]

    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-show_format", "-of", "json", str(mp3)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    probed_tags = json.loads(probe.stdout)["format"]["tags"]
    assert probed_tags["DPC_SEED"] == "3555429983"
    assert probed_tags["DPC_JOB_ID"] == "job_real1234"
    assert probed_tags["DPC_ENGINE"] == "yue2.cpp"
    assert any(k.startswith("lyrics") and v == MULTI_SECTION_LYRICS for k, v in probed_tags.items())


def test_is_safe_dpc_key_uses_strict_allowlist_and_rejects_chat_prompt_and_custom_tags(tmp_path):
    allowed = (
        "DPC_SEED",
        "DPC_LM_SEED",
        "DPC_ENGINE",
        "DPC_ENGINE_VERSION",
        "DPC_VARIANT",
        "DPC_BACKBONE",
        "DPC_JOB_ID",
        "DPC_COVER_OF",
        "DPC_SYNTH_BATCH_SIZE",
        "DPC_SYNTH_BATCH_INDEX",
        "DPC_EDIT_ACTION",
        "DPC_EDIT_OF",
        "DPC_EDIT_REGION",
    )
    for key in allowed:
        assert MusicExporter._is_safe_dpc_key(key) is True, key

    rejected = (
        "DPC_CHAT_ID",
        "DPC_CONVERSATION",
        "DPC_MESSAGE",
        "DPC_REQUESTER",
        "DPC_PROMPT",
        "DPC_NODE_ID",
        "DPC_PROVENANCE",
        "DPC_UNKNOWN_CUSTOM",
        "DPC_EXPORT_OF",
        "DPC_EXPORT_PRESET",
        "DPC_EXPORT_TP_LIMIT",
        "DPC_EDIT_PROMPT",
        "DPC_SYNTH_BATCH_NODE_ID",
        "OTHER_TAG",
    )
    for key in rejected:
        assert MusicExporter._is_safe_dpc_key(key) is False, key

    flac = _build_flac(
        tmp_path,
        {
            "DPC_SEED": "101",
            "DPC_LM_SEED": "202",
            "DPC_ENGINE": "yue2.cpp",
            "DPC_ENGINE_VERSION": "v1.2.3",
            "DPC_VARIANT": "1",
            "DPC_BACKBONE": "stock",
            "DPC_JOB_ID": "job_allow_01",
            "DPC_COVER_OF": "md5:1111",
            "DPC_SYNTH_BATCH_SIZE": "2",
            "DPC_SYNTH_BATCH_INDEX": "0",
            "DPC_EDIT_ACTION": "repaint",
            "DPC_EDIT_OF": "md5:2222",
            "DPC_EDIT_REGION": "5.0-15.0",
            # Unrecognized / chat / prompt tags that must never be copied:
            "DPC_CHAT_ID": "chat_leak_99",
            "DPC_CONVERSATION": "conv_leak_88",
            "DPC_MESSAGE": "msg_leak_77",
            "DPC_REQUESTER": "requester_leak_66",
            "DPC_PROMPT": "secret prompt text",
            "DPC_NODE_ID": "dpc-node-leak0055",
            "DPC_UNKNOWN_CUSTOM": "custom_leak_44",
            "DPC_EDIT_PROMPT": "edit prompt leak",
            "DPC_SYNTH_BATCH_NODE_ID": "batch_node_leak",
        },
        name="allowlist.flac",
    )
    source = ExportSource(
        track_id="trk_allow",
        path=flac,
        content_hash="md5:" + "aa" * 16,
        title="Allowlist",
        artist="DPC",
        true_peak_dbtp=-2.0,
        true_peak_basis="test",
    )
    exporter = _exporter(tmp_path)
    mp3 = Path(exporter.export_track(source, "mp3-v0")["path"])
    opus = Path(exporter.export_track(source, "opus-160")["path"])

    for tag_map in (read_id3_txxx(mp3), read_opus_comments(opus)):
        assert tag_map["DPC_SEED"] == "101"
        assert tag_map["DPC_LM_SEED"] == "202"
        assert tag_map["DPC_ENGINE"] == "yue2.cpp"
        assert tag_map["DPC_ENGINE_VERSION"] == "v1.2.3"
        assert tag_map["DPC_VARIANT"] == "1"
        assert tag_map["DPC_BACKBONE"] == "stock"
        assert tag_map["DPC_JOB_ID"] == "job_allow_01"
        assert tag_map["DPC_COVER_OF"] == "md5:1111"
        assert tag_map["DPC_SYNTH_BATCH_SIZE"] == "2"
        assert tag_map["DPC_SYNTH_BATCH_INDEX"] == "0"
        assert tag_map["DPC_EDIT_ACTION"] == "repaint"
        assert tag_map["DPC_EDIT_OF"] == "md5:2222"
        assert tag_map["DPC_EDIT_REGION"] == "5.0-15.0"
        for blocked in (
            "DPC_CHAT_ID",
            "DPC_CONVERSATION",
            "DPC_MESSAGE",
            "DPC_REQUESTER",
            "DPC_PROMPT",
            "DPC_NODE_ID",
            "DPC_UNKNOWN_CUSTOM",
            "DPC_EDIT_PROMPT",
            "DPC_SYNTH_BATCH_NODE_ID",
        ):
            assert blocked not in tag_map

    for raw in (mp3.read_bytes(), opus.read_bytes()):
        for secret in (
            b"chat_leak_99",
            b"conv_leak_88",
            b"msg_leak_77",
            b"requester_leak_66",
            b"secret prompt text",
            b"dpc-node-leak0055",
            b"custom_leak_44",
            b"edit prompt leak",
            b"batch_node_leak",
        ):
            assert secret not in raw


def test_copied_dpc_tags_enforce_value_cap_total_byte_budget_count_cap_and_deterministic_sort(
    tmp_path, monkeypatch
):
    assert export_mod.MAX_COPIED_DPC_TAGS == 32
    assert export_mod.MAX_COPIED_DPC_VALUE_BYTES == 512
    assert export_mod.MAX_COPIED_DPC_TOTAL_BYTES == 4096

    # 1. Per-value cap: 512 bytes passes, 513 bytes is omitted.
    exact_val = "v" * export_mod.MAX_COPIED_DPC_VALUE_BYTES
    over_val = "w" * (export_mod.MAX_COPIED_DPC_VALUE_BYTES + 1)
    source = ExportSource(
        track_id="trk_cap",
        path=tmp_path / "missing.flac",
        content_hash="md5:" + "bb" * 16,
        title="Cap Test",
    )
    per_val_tags = dict(
        MusicExporter._source_dpc_tags(
            source,
            {"DPC_BACKBONE": exact_val, "DPC_ENGINE_VERSION": over_val},
            {},
        )
    )
    assert per_val_tags == {"DPC_BACKBONE": exact_val}

    # 2. Total byte budget across 12 allowed tags (each 450 bytes -> >5400 bytes total > 4096).
    big_tags = {
        "DPC_SEED": "s" * 450,
        "DPC_LM_SEED": "l" * 450,
        "DPC_ENGINE": "e" * 450,
        "DPC_ENGINE_VERSION": "v" * 450,
        "DPC_VARIANT": "r" * 450,
        "DPC_BACKBONE": "b" * 450,
        "DPC_JOB_ID": "j" * 450,
        "DPC_COVER_OF": "c" * 450,
        "DPC_SYNTH_BATCH_SIZE": "z" * 450,
        "DPC_SYNTH_BATCH_INDEX": "i" * 450,
        "DPC_EDIT_ACTION": "a" * 450,
        "DPC_EDIT_OF": "o" * 450,
    }
    flac = _build_flac(tmp_path, {**big_tags, "LYRICS": MULTI_SECTION_LYRICS}, name="budget.flac")
    export_source = ExportSource(
        track_id="trk_budget",
        path=flac,
        content_hash="md5:" + "cc" * 16,
        title="Budget",
        artist="DPC",
        true_peak_dbtp=-2.0,
        true_peak_basis="test",
    )
    opus = Path(_exporter(tmp_path).export_track(export_source, "opus-160")["path"])
    opus_comments = read_opus_comments(opus)
    copied_in_opus = {
        k: v for k, v in opus_comments.items() if k in export_mod.ALLOWED_SOURCE_DPC_TAGS
    }
    assert len(copied_in_opus) < len(big_tags)
    total_copied_bytes = sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in copied_in_opus.items())
    assert total_copied_bytes <= export_mod.MAX_COPIED_DPC_TOTAL_BYTES
    assert opus_comments["DPC_EXPORT_OF"] == export_source.content_hash
    assert opus_comments["LYRICS"] == MULTI_SECTION_LYRICS
    packet = export_mod.read_opus_tags_packet(opus)
    assert packet is not None and len(packet) <= export_mod.OPUS_TAGS_MAX_BYTES

    # 3. Count cap (32) and deterministic key sorting regardless of input tag order.
    synthetic_allowed = frozenset(f"DPC_SYNTH_BATCH_K{i:02d}" for i in range(40))
    monkeypatch.setattr(export_mod, "ALLOWED_SOURCE_DPC_TAGS", synthetic_allowed)
    forward_input = {f"DPC_SYNTH_BATCH_K{i:02d}": str(i) for i in range(40)}
    reverse_input = {f"DPC_SYNTH_BATCH_K{i:02d}": str(i) for i in reversed(range(40))}
    res_forward = MusicExporter._source_dpc_tags(source, forward_input, {})
    res_reverse = MusicExporter._source_dpc_tags(source, reverse_input, {})
    assert len(res_forward) == export_mod.MAX_COPIED_DPC_TAGS
    assert res_forward == res_reverse
    assert [k for k, _ in res_forward] == sorted(k for k, _ in res_forward)
