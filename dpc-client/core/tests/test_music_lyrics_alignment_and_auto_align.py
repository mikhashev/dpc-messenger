"""Wave 2 Card 10 tests: forced lyrics alignment, align_music_lyrics, .lrc/SYLT/Opus export,
and automatic post-generation alignment for generated vocal takes.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.managers.card_lease import CardQueueRefusal
from dpc_client_core.music import id3_sylt, lyrics as lyrics_mod, schemas
from dpc_client_core.music.capabilities import MusicCapabilities
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from dpc_client_core.music.export import ExportSource, MusicExporter, read_opus_comments
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.jobs import MusicJobStore
from dpc_client_core.music.library import MusicLibrary, MusicLibraryError
from dpc_client_core.music.lyrics import MusicLyrics, parse_lrc
from dpc_client_core.music.runner import MusicJobRunner
from dpc_client_core.providers.base import ModelNotCachedError
from dpc_client_core.service import CoreService
from dpc_client_core.settings import _SEEDED_KEYS, Settings

from tests._acestep_helpers import Rig, fake_flac, free_env, install_fake_engine
from tests._yue2_helpers import DualRig, install_both, install_fake_yue2, yue2_env, yue2_params
from tests.test_a_flac_or_wav_gets_its_provenance_once_and_its_audio_is_unchanged import make_wav
from tests.test_the_lossy_export_has_an_opus_preset_and_a_true_peak_switch import fake_ogg_opus


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    orig = httpx.AsyncHTTPTransport.handle_async_request

    async def guard(self, request):
        if request.url.host not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError(f"a test tried to reach external network: {request.url}")
        return await orig(self, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guard)


def _words_from_timed_text(pairs: list[tuple[str, float, float]]) -> list[dict]:
    return [{"word": w, "start": s, "end": e} for w, s, e in pairs]


def _make_realistic_fake_flac(min_duration_s: float = 20.0):
    """Fake FLAC converter keeping WAV PCM MD5 while reporting realistic song duration in STREAMINFO."""
    import struct
    from dpc_client_core.music.runner import pcm_md5_and_sha256, read_wav_info

    def _convert(wav: Path, flac: Path, tags) -> None:
        info = read_wav_info(wav)
        md5, _ = pcm_md5_and_sha256(wav, info)
        frame = info.channels * ((info.bits + 7) // 8)
        total = max(info.data_size // frame, int(round(min_duration_s * info.sample_rate)))
        packed = (info.sample_rate << 44) | ((info.channels - 1) << 41) | ((info.bits - 1) << 36) | total
        si = struct.pack(">HH", 4096, 4096) + (0).to_bytes(3, "big") * 2 + packed.to_bytes(8, "big") + bytes.fromhex(md5)
        flac.write_bytes(b"fLaC" + bytes([0x80]) + len(si).to_bytes(3, "big") + si)
        fake_flac.calls.append(dict(tags))

    return _convert


class _FakeFfmpegExport:
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


# -- 1. Pure sequence aligner (lyrics_align.py) --------------------------------------------


def test_aligner_preserves_exact_text_contractions_hyphens_and_cyrillic_stress():
    from dpc_client_core.music import lyrics_align

    known = (
        "[Verse 1]\n"
        "We don't stop rock-and-roll!\n"
        "Ты́сячи огней, мой ёжик поёт.\n"
        "[Chorus]\n"
        "Hold on tight...\n"
    )
    whisper_words = _words_from_timed_text([
        ("we", 4.10, 4.30),
        ("don", 4.35, 4.50),
        ("'t", 4.50, 4.65),
        ("stop", 4.70, 5.00),
        ("rock", 5.05, 5.25),
        ("and", 5.25, 5.40),
        ("roll", 5.40, 5.80),
        ("тысячи", 8.20, 8.70),
        ("огней", 8.75, 9.20),
        ("мой", 9.25, 9.50),
        ("ежик", 9.55, 9.95),
        ("поет", 10.00, 10.60),
        ("hold", 14.00, 14.35),
        ("on", 14.40, 14.60),
        ("tight", 14.65, 15.10),
    ])

    res = lyrics_align.align_lyrics(known, whisper_words, duration_s=30.0)
    assert res.sufficient is True
    assert [ln["text"] for ln in res.lines] == [
        "[Verse 1]",
        "We don't stop rock-and-roll!",
        "Ты́сячи огней, мой ёжик поёт.",
        "[Chorus]",
        "Hold on tight...",
    ]
    assert res.lines[1]["t_start"] == pytest.approx(4.10, abs=0.001)
    assert res.lines[2]["t_start"] == pytest.approx(8.20, abs=0.001)
    assert res.lines[4]["t_start"] == pytest.approx(14.00, abs=0.001)
    # Section headers precede their sung lines monotonically and round-trip cleanly through LRC.
    assert 0.0 <= res.lines[0]["t_start"] <= res.lines[1]["t_start"]
    assert res.lines[1]["t_start"] < res.lines[3]["t_start"] <= res.lines[4]["t_start"]
    assert all(ln["t_start"] <= res.lines[i + 1]["t_start"] for i, ln in enumerate(res.lines[:-1]))

    rec = lyrics_mod.make_lyrics("sha256:x", "aligned", res.lines, language="ru")
    lrc_text = lyrics_mod.format_lrc(rec)
    reparsed = parse_lrc(lrc_text)
    assert [ln["text"] for ln in reparsed.lines] == [ln["text"] for ln in res.lines]


def test_aligner_tolerates_mishearings_dropped_extra_words_and_repeated_choruses():
    from dpc_client_core.music import lyrics_align

    known = (
        "[Verse 1]\n"
        "First morning light shines through\n"
        "Shadow fades away now\n"
        "[Chorus]\n"
        "We rise into the sky\n"
        "We never say goodbye\n"
        "[Verse 2]\n"
        "Cold rain falls on stone\n"
        "[Chorus]\n"
        "We rise into the sky\n"
        "We never say goodbye\n"
    )
    whisper_words = _words_from_timed_text([
        # Extra ad-lib before verse
        ("yeah", 1.0, 1.3),
        ("uh", 1.5, 1.7),
        # Verse 1 (with mishearing "shines"->"signs", dropped "away")
        ("first", 5.0, 5.3),
        ("morning", 5.4, 5.8),
        ("light", 5.9, 6.2),
        ("signs", 6.2, 6.6),
        ("through", 6.7, 7.0),
        ("shadow", 8.0, 8.4),
        ("fades", 8.5, 8.9),
        ("now", 9.0, 9.3),
        # Chorus 1 (~15s) with extra vocalization
        ("we", 15.0, 15.2),
        ("rise", 15.25, 15.7),
        ("ooh", 15.8, 16.0),
        ("into", 16.0, 16.3),
        ("the", 16.3, 16.45),
        ("sky", 16.5, 17.0),
        ("we", 18.0, 18.2),
        ("never", 18.25, 18.6),
        ("say", 18.7, 19.0),
        ("goodbye", 19.0, 19.7),
        # Verse 2 (~28s) with mishearing "cold"->"gold"
        ("gold", 28.0, 28.4),
        ("rain", 28.5, 28.9),
        ("falls", 29.0, 29.4),
        ("on", 29.45, 29.6),
        ("stone", 29.7, 30.2),
        # Chorus 2 (~42s)
        ("we", 42.0, 42.2),
        ("rise", 42.3, 42.7),
        ("into", 42.8, 43.1),
        ("the", 43.15, 43.3),
        ("sky", 43.35, 43.9),
        ("we", 45.0, 45.2),
        ("never", 45.25, 45.6),
        ("say", 45.7, 46.0),
        ("goodbye", 46.1, 46.8),
    ])

    res = lyrics_align.align_lyrics(known, whisper_words, duration_s=60.0)
    assert res.sufficient is True
    by_idx = {i: ln["t_start"] for i, ln in enumerate(res.lines)}
    # Sung lines indices: 1,2 (V1); 4,5 (C1); 7 (V2); 9,10 (C2)
    assert by_idx[1] == pytest.approx(5.0, abs=0.01)
    assert by_idx[2] == pytest.approx(8.0, abs=0.01)
    assert by_idx[4] == pytest.approx(15.0, abs=0.01)
    assert by_idx[5] == pytest.approx(18.0, abs=0.01)
    assert by_idx[7] == pytest.approx(28.0, abs=0.01)
    assert by_idx[9] == pytest.approx(42.0, abs=0.01)
    assert by_idx[10] == pytest.approx(45.0, abs=0.01)
    assert all(res.lines[i]["t_start"] <= res.lines[i + 1]["t_start"] for i in range(len(res.lines) - 1))


def test_aligner_interpolates_unmatched_leading_middle_trailing_and_supports_25fps_anchors():
    from dpc_client_core.music import lyrics_align

    known = (
        "[Intro]\n"
        "Leading\n"
        "Anchored early one\n"
        "Middle unmatched A\n"
        "Middle unmatched B\n"
        "Anchored late two\n"
        "Trailing line\n"
    )
    whisper_words = _words_from_timed_text([
        ("anchored", 10.0, 10.5),
        ("early", 10.6, 11.0),
        ("one", 11.1, 11.4),
        ("anchored", 25.0, 25.5),
        ("late", 25.6, 26.0),
        ("two", 26.1, 26.4),
    ])

    res = lyrics_align.align_lyrics(known, whisper_words, duration_s=40.0)
    assert res.sufficient is True
    t = [ln["t_start"] for ln in res.lines]
    # All timestamps finite and within [0, 40.0], strictly monotonic for sung lines
    assert all(0.0 <= x <= 40.0 for x in t)
    assert all(t[i] <= t[i + 1] for i in range(len(t) - 1))
    assert t[1] < t[2] == pytest.approx(10.0, abs=0.01)
    assert t[2] < t[3] < t[4] < t[5] == pytest.approx(25.0, abs=0.01)
    assert t[5] < t[6] <= 40.0

    # Optional 25 fps yue2 section anchors conversion
    anchors = lyrics_align.anchors_from_yue2_result({
        "section_frames": [{"label": "verse", "frame": 125}, {"label": "chorus", "frame": 500}],
    })
    assert anchors == [{"label": "verse", "start_s": 5.0}, {"label": "chorus", "start_s": 20.0}]

    # When only `abc` score is present (as in current yue2.cpp replay), score_parts yields anchors.
    abc = "\n".join([
        "X:1", "M:4/4", "L:1/16", "Q:1/4=120", "K:C",
        "V: Vocal",
        "% intro", "Z2|",
        "% verse", "C4D4E4F4|",
    ])
    abc_anchors = lyrics_align.anchors_from_yue2_result({"abc": abc})
    assert abc_anchors and abc_anchors[1]["label"] == "verse" and abc_anchors[1]["start_s"] == pytest.approx(4.0)


def test_aligner_handles_mlx_words_threshold_and_large_songs_without_quadratic_blowup():
    from dpc_client_core.music import lyrics_align

    # MLX nested words inside segments
    mlx_result = {
        "segments": [
            {"start": 2.0, "end": 4.0, "text": "Hello world", "words": [
                {"word": "Hello", "start": 2.1, "end": 2.5},
                {"word": "world", "start": 2.6, "end": 3.0},
            ]},
        ],
        "duration": 10.0,
    }
    words = lyrics_align.extract_whisper_words(mlx_result)
    assert words == [{"word": "Hello", "start": 2.1, "end": 2.5}, {"word": "world", "start": 2.6, "end": 3.0}]

    # Too few matches rejected cleanly
    long_lyrics = "\n".join(f"Unique lyric line number {i} alpha beta" for i in range(12))
    low = lyrics_align.align_lyrics(long_lyrics, [{"word": "alpha", "start": 5.0, "end": 5.3}], duration_s=40.0)
    assert low.sufficient is False

    # Bounded runtime on >1000 words
    many_lines = [f"Verse word{i} alpha beta gamma delta" for i in range(250)]  # 1250 words
    many_words = []
    t = 1.0
    for i in range(250):
        for tok in (f"word{i}", "alpha", "gamma", "delta"):
            many_words.append({"word": tok, "start": round(t, 2), "end": round(t + 0.15, 2)})
            t += 0.2
    t0 = time.monotonic()
    big = lyrics_align.align_lyrics("\n".join(many_lines), many_words, duration_s=t + 5.0)
    elapsed = time.monotonic() - t0
    assert big.sufficient is True
    assert len(big.lines) == 250
    assert elapsed < 3.0


# -- 2. Schema, Command, Sidecar, SYLT / Opus Export, Capabilities ---------------------------


class _TrackLib:
    def __init__(self, path: Path, *, source_hash="sha256:lib1"):
        self.path = path
        self.entry = {
            "id": "trk_a1b2c3d4e5f60001",
            "title": path.stem,
            "artist": "Band",
            "album": None,
            "duration_s": 30.0,
            "source_hash": source_hash,
            "content_hash": "md5:" + "aa" * 16,
            "hash_state": "done",
            "source": {"kind": "local", "name": path.name, "uri": path.as_uri()},
        }

    def get_entry(self, track_id):
        if track_id != self.entry["id"]:
            raise MusicLibraryError("unknown_track", f"no track {track_id}")
        return dict(self.entry)

    def get_track_path(self, track_id):
        return self.path if track_id == self.entry["id"] else None


def test_schema_capability_and_command_registrations():
    assert "aligned" in schemas.LYRICS_SOURCES
    assert "align_music_lyrics" in MUSIC_COMMANDS
    assert "align_music_lyrics" in ALLOWED_COMMANDS
    assert hasattr(CoreService, "align_music_lyrics")
    assert asyncio.iscoroutinefunction(MusicCommandsMixin.align_music_lyrics)
    assert getattr(MusicCommandsMixin.align_music_lyrics, "dpc_slow", False) is True

    caps = MusicCapabilities().report()
    assert caps["features"]["word_timings"]["status"] == "supported"


@pytest.mark.asyncio
async def test_align_music_lyrics_command_sidecar_and_mp3_sylt_opus_export_round_trip(tmp_path):
    raw_lyrics = (
        "[Verse 1]\n"
        "First line exact text!\n"
        "Second line exact text?\n"
        "\n"
        "[Chorus]\n"
        "Sing it loud together\n"
    )
    flac = tmp_path / "lib" / "Band - Song.flac"
    flac.parent.mkdir(parents=True, exist_ok=True)
    wav = make_wav(tmp_path / "src.wav")
    fake_flac(wav, flac, {})
    from dpc_client_core.music import audio_tags as at
    at.flac_add_comments(flac, {"LYRICS": raw_lyrics}, once_key="")

    whisper_result = {
        "text": "First line exact text Second line exact text Sing it loud together",
        "language": "en",
        "duration": 30.0,
        "provider": "local_whisper",
        "model": "openai/whisper-large-v3-turbo",
        "words": _words_from_timed_text([
            ("First", 2.0, 2.3), ("line", 2.35, 2.6), ("exact", 2.65, 2.9), ("text", 2.95, 3.3),
            ("Second", 6.0, 6.3), ("line", 6.35, 6.6), ("exact", 6.65, 6.9), ("text", 6.95, 7.3),
            ("Sing", 14.0, 14.3), ("it", 14.35, 14.5), ("loud", 14.55, 14.9), ("together", 15.0, 15.6),
        ]),
    }

    class FakeWhisper:
        alias = "local_whisper_large"
        config = {"type": "local_whisper"}
        model_name = "openai/whisper-large-v3-turbo"
        model_loaded = True

        async def transcribe(self, path, **kw):
            assert kw.get("with_detail") is True
            return whisper_result

    class AlignService(MusicCommandsMixin):
        def __init__(self, home: Path, lib: _TrackLib):
            self.settings = SimpleNamespace(dpc_home_dir=home, get=lambda s, k, f=None: f,
                                            get_local_transcription_enabled=lambda: True)
            self.llm_manager = SimpleNamespace(providers={"w": FakeWhisper()}, voice_provider=None)
            self.lib = lib

        async def _music(self):
            return self.lib

    lib = _TrackLib(flac)
    svc = AlignService(tmp_path / "dpc", lib)
    lyr = await svc._music_lyrics()
    lyr._tools = MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=lambda cmd, *, timeout: subprocess.CompletedProcess(
        cmd, 0, json.dumps({"format": {"duration": "30.0", "tags": {"LYRICS": raw_lyrics}}, "streams": []}).encode(), b""))

    # Before aligning: chain reads plain unsynced lyrics from tags
    before = await svc.get_music_lyrics(track_id=lib.entry["id"])
    assert before["lyrics"]["synced"] is False
    assert not flac.with_suffix(".lrc").exists()

    aligned = await svc.align_music_lyrics(track_id=lib.entry["id"])
    assert aligned["status"] == "ok"
    assert aligned["applied"] is True
    assert aligned["lyrics"]["source"] == "aligned"
    assert aligned["lyrics"].get("synced") is not False
    assert [ln["text"] for ln in aligned["lyrics"]["lines"]] == [
        "[Verse 1]",
        "First line exact text!",
        "Second line exact text?",
        "[Chorus]",
        "Sing it loud together",
    ]
    assert aligned["lyrics"]["lines"][1]["t_start"] == pytest.approx(2.0)
    assert aligned["lyrics"]["lines"][2]["t_start"] == pytest.approx(6.0)
    assert aligned["lyrics"]["lines"][4]["t_start"] == pytest.approx(14.0)

    # Watched-folder track MUST NOT get an .lrc sidecar or modified FLAC tags (ADR-051 Q4);
    # aligned lyrics live only in DPC home JSON, and MP3/Opus export reads them from there.
    sidecar = flac.with_suffix(".lrc")
    assert not sidecar.exists()
    assert aligned.get("sidecar_written") is False
    assert aligned.get("lrc_path") is None
    assert "SYNCEDLYRICS" not in at.flac_read_comments(flac)
    reparsed = parse_lrc(aligned["lrc"])
    assert [ln["text"] for ln in reparsed.lines] == [ln["text"] for ln in aligned["lyrics"]["lines"]]

    # Export to MP3 and Opus picks up aligned synced lines -> SYLT (MP3) + SYNCEDLYRICS (Opus) + preserved USLT/LYRICS
    src = await svc._music_export_source(lib, lib.entry)
    exporter = MusicExporter(
        tmp_path / "exports",
        tools=MediaTools(ffmpeg="ffmpeg", ffprobe=None, runner=_FakeFfmpegExport()),
        measure=lambda _p: {"true_peak_dbtp": -1.5},
    )
    mp3 = Path(exporter.export_track(src, "mp3-v0")["path"])
    opus = Path(exporter.export_track(src, "opus-160")["path"])

    sylt = id3_sylt.read_id3_sylt(mp3)
    assert sylt is not None
    assert sylt.language == "eng"
    assert [ln["text"] for ln in sylt.lines] == [ln["text"] for ln in aligned["lyrics"]["lines"]]
    assert sylt.lines[1]["t_start"] == pytest.approx(2.0, abs=0.01)

    # MP3 tags readable through MusicLyrics chain even without .lrc
    mp3_lib = _TrackLib(mp3, source_hash="sha256:mp3")
    mp3_chain = MusicLyrics(tmp_path / "dpc2", mp3_lib, lrclib_enabled=lambda: False,
                            tools=MediaTools(ffmpeg=None, ffprobe=None))
    from_mp3 = await mp3_chain.get(mp3_lib.entry["id"])
    assert from_mp3["lyrics"]["source"] == "tags"
    assert [ln["text"] for ln in from_mp3["lyrics"]["lines"]] == [ln["text"] for ln in aligned["lyrics"]["lines"]]

    # Opus Vorbis comments carry SYNCEDLYRICS (LRC) and LYRICS (preserved blank lines)
    opus_tags = read_opus_comments(opus)
    assert opus_tags["LYRICS"] == raw_lyrics
    assert "SYNCEDLYRICS" in opus_tags
    picked_key, _picked_val = lyrics_mod.pick_lyrics_tag(opus_tags)
    assert picked_key.upper() == "SYNCEDLYRICS"
    assert parse_lrc(opus_tags["SYNCEDLYRICS"]).lines[1]["text"] == "First line exact text!"


@pytest.mark.asyncio
async def test_align_music_lyrics_errors_when_no_lyrics_or_alignment_fails(tmp_path):
    mp3 = tmp_path / "Music" / "Empty.mp3"
    mp3.parent.mkdir(parents=True)
    mp3.write_bytes(b"audio")
    lib = _TrackLib(mp3)

    class _AlignSvc(MusicCommandsMixin):
        def __init__(self, whisper):
            self.settings = SimpleNamespace(dpc_home_dir=tmp_path / "dpc", get=lambda s, k, f=None: f,
                                            get_local_transcription_enabled=lambda: True)
            self.llm_manager = SimpleNamespace(providers={"w": whisper} if whisper else {}, voice_provider=None)
            self._lib = lib

        async def _music(self):
            return self._lib

    # 1. Track with no lyrics at all
    svc_no_lyr = _AlignSvc(SimpleNamespace(alias="w", config={"type": "local_whisper"}, model_name="m"))
    lyr = await svc_no_lyr._music_lyrics()
    lyr._tools = MediaTools(ffmpeg=None, ffprobe=None)
    ans = await svc_no_lyr.align_music_lyrics(track_id=lib.entry["id"])
    assert ans["status"] == "error" and ans["code"] == "no_lyrics"

    # 2. Track with unsynced lyrics, Whisper matches too few words -> alignment_failed, no .lrc written
    plain_text = "\n".join(f"Line number {i} never spoken by whisper" for i in range(10))
    from dpc_client_core.music.lyrics import plain_lines, make_lyrics
    await lyr._store(lib.entry["id"], make_lyrics(lib.entry["source_hash"], "tags", plain_lines(plain_text), synced=False))

    class _SilentWhisper:
        alias = "w"
        config = {"type": "local_whisper"}
        model_name = "m"
        async def transcribe(self, path, **kw):
            return {"text": "completely unrelated words only", "language": "en", "duration": 20.0,
                    "provider": "local_whisper", "words": [{"word": "completely", "start": 1.0, "end": 1.5}]}

    svc_no_lyr.llm_manager.providers = {"w": _SilentWhisper()}
    failed = await svc_no_lyr.align_music_lyrics(track_id=lib.entry["id"])
    assert failed["status"] == "error" and failed["code"] == "alignment_failed"
    assert not mp3.with_suffix(".lrc").exists()
    still = await svc_no_lyr.get_music_lyrics(track_id=lib.entry["id"])
    assert still["lyrics"]["synced"] is False


# -- 3. Settings & Runner Auto-align After Generated Take -----------------------------------


def test_settings_auto_align_lyrics_default_seeded_and_parsed(tmp_path):
    assert ("music", "auto_align_lyrics") in _SEEDED_KEYS
    s = Settings(tmp_path)
    assert s.get_music_auto_align_lyrics() is True

    ini = tmp_path / "config.ini"
    ini.write_text(ini.read_text(encoding="utf-8").replace("auto_align_lyrics = true", "auto_align_lyrics = false"), encoding="utf-8")
    s2 = Settings(tmp_path)
    assert s2.get_music_auto_align_lyrics() is False


@pytest.mark.asyncio
async def test_generated_take_auto_aligns_when_enabled_and_degrades_gracefully(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    install_fake_yue2(home)
    Settings(home)

    vocal_lyrics = "[verse]\nHello brave morning\nSing across the night\n"
    aligned_calls = []

    class _FakeWhisper:
        alias = "w"
        config = {"type": "local_whisper"}
        model_name = "openai/whisper-large-v3-turbo"
        model_loaded = True
        mode = "ok"

        async def transcribe(self, path, **kw):
            aligned_calls.append((Path(path).name, kw))
            if self.mode == "busy":
                raise CardQueueRefusal("busy", "other", "llama")
            if self.mode == "uncached":
                raise ModelNotCachedError("openai/whisper-large-v3-turbo", "/cache")
            if self.mode == "few":
                return {"text": "none", "language": "en", "duration": 10.0, "provider": "local_whisper",
                        "words": [{"word": "zzz", "start": 1.0, "end": 1.2}]}
            return {
                "text": "Hello brave morning Sing across the night",
                "language": "en",
                "duration": 20.0,
                "provider": "local_whisper",
                "words": _words_from_timed_text([
                    ("Hello", 1.5, 1.8), ("brave", 1.85, 2.2), ("morning", 2.25, 2.7),
                    ("Sing", 5.0, 5.3), ("across", 5.35, 5.7), ("the", 5.75, 5.9), ("night", 5.95, 6.5),
                ]),
            }

    whisper = _FakeWhisper()

    class _CmdSvc(MusicCommandsMixin):
        def __init__(self, dpc_home: Path):
            self.settings = Settings(dpc_home)
            self.llm_manager = SimpleNamespace(providers={"w": whisper}, voice_provider=None)

        async def _music(self):
            return rig.library

    cmd_svc = _CmdSvc(home)

    # 3a. ACE-Step vocal take: job enters review immediately; auto-align finishes in background with .lrc + SYNCEDLYRICS
    async with Rig(home, converter=_make_realistic_fake_flac(20.0)) as rig:
        rig.runner._auto_align = cmd_svc._music_auto_align_take
        job = rig.store.create_job("generate_from_text", {
            "style": "indie rock", "lyrics": vocal_lyrics, "variants": 1, "duration_s": 20, "seed": 42, "engine": "acestep",
        })
        rig.runner.wake()
        done = await rig.wait_for(job["id"], ("review",))
        await rig.runner.wait_auto_align()
        take = rig.store.list_takes(done["id"])[0]
        tid = take["library_track_id"]
        flac_path = rig.library.get_track_path(tid)
        assert flac_path.with_suffix(".lrc").is_file()
        got = await (await cmd_svc._music_lyrics()).get(tid)
        assert got["lyrics"]["source"] == "aligned"
        assert got["lyrics"].get("synced") is not False
        assert [ln["text"] for ln in got["lyrics"]["lines"]] == [
            "[verse]", "Hello brave morning", "Sing across the night",
        ]
        assert got["lyrics"]["lines"][1]["t_start"] == pytest.approx(1.5)
        assert got["lyrics"]["lines"][2]["t_start"] == pytest.approx(5.0)
        assert len(aligned_calls) == 1

        # 3b. Card lease refusal / uncached / too-few-matches MUST NOT fail the job or lose unsynced take
        for mode in ("busy", "uncached", "few"):
            whisper.mode = mode
            j2 = rig.store.create_job("generate_from_text", {
                "style": "indie rock", "lyrics": vocal_lyrics, "variants": 1, "duration_s": 20, "seed": 43, "engine": "acestep",
            })
            rig.runner.wake()
            d2 = await rig.wait_for(j2["id"], ("review", "failed"))
            await rig.runner.wait_auto_align()
            assert d2["state"] == "review"
            tk2 = rig.store.list_takes(d2["id"])[0]
            p2 = rig.library.get_track_path(tk2["library_track_id"])
            assert p2 is not None and p2.is_file()
            assert not p2.with_suffix(".lrc").exists()

        # 3c. Instrumental take does NOT invoke Whisper
        whisper.mode = "ok"
        before_calls = len(aligned_calls)
        j3 = rig.store.create_job("generate_from_text", {
            "style": "ambient", "lyrics": None, "variants": 1, "duration_s": 20, "seed": 44, "engine": "acestep",
        })
        rig.runner.wake()
        d3 = await rig.wait_for(j3["id"], ("review",))
        await rig.runner.wait_auto_align()
        assert d3["state"] == "review"
        assert len(aligned_calls) == before_calls

        # 3d. auto_align_lyrics = false skips Whisper completely
        cmd_svc.settings._config.set("music", "auto_align_lyrics", "false")
        j4 = rig.store.create_job("generate_from_text", {
            "style": "pop", "lyrics": vocal_lyrics, "variants": 1, "duration_s": 20, "seed": 45, "engine": "acestep",
        })
        rig.runner.wake()
        d4 = await rig.wait_for(j4["id"], ("review",))
        await rig.runner.wait_auto_align()
        assert d4["state"] == "review"
        assert len(aligned_calls) == before_calls

    # 3e. YuE2 vocal take with DualRig also auto-aligns when enabled
    yue2_env(monkeypatch)
    cmd_svc.settings._config.set("music", "auto_align_lyrics", "true")
    home2 = tmp_path / "dpc_yue2"
    install_both(home2)
    Settings(home2)
    cmd_svc2 = _CmdSvc(home2)
    async with DualRig(home2) as drig:
        drig.runner._convert = _make_realistic_fake_flac(20.0)
        cmd_svc2._music_library_instance = drig.library
        async def _lib2():
            return drig.library
        cmd_svc2._music = _lib2
        drig.runner._auto_align = cmd_svc2._music_auto_align_take
        yjob = drig.store.create_job("generate_from_text", {
            "style": "indie rock", "lyrics": vocal_lyrics, "variants": 1, "duration_s": 60, "seed": 42, "engine": "yue2",
        })
        drig.runner.wake()
        ydone = await drig.wait_for(yjob["id"], ("review",))
        await drig.runner.wait_auto_align()
        ytake = drig.store.list_takes(ydone["id"])[0]
        yflac = drig.library.get_track_path(ytake["library_track_id"])
        assert yflac.with_suffix(".lrc").is_file()
        ygot = await (await cmd_svc2._music_lyrics()).get(ytake["library_track_id"])
        assert ygot["lyrics"]["source"] == "aligned"
        # Deleting generated take also cleans up <stem>.lrc sidecar
        deleted = await drig.library.delete_generated_file(ytake["library_track_id"])
        assert not yflac.exists()
        assert not yflac.with_suffix(".lrc").exists()
        assert any(p.endswith(".lrc") for p in deleted["deleted"])

    # 3f. Uncached real LocalWhisperProvider skips fast without downloading or touching torch
    from dpc_client_core.providers.whisper_provider import LocalWhisperProvider
    from dpc_client_core.providers import model_sizes

    cache_checks = []
    real_whisper = LocalWhisperProvider("w_real", {"type": "local_whisper", "model": "openai/whisper-large-v3-turbo"})
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda m: cache_checks.append(m) or False)
    cmd_svc.llm_manager.providers = {"w_real": real_whisper}
    dummy_flac = tmp_path / "dummy.flac"
    dummy_flac.write_bytes(b"fake")
    track_lib_3f = _TrackLib(dummy_flac)
    async def _lib3f():
        return track_lib_3f
    cmd_svc._music = _lib3f
    cmd_svc._music_lyrics_instance = None
    # Safe no-op on non-cached real provider (would raise if it tried to load/download)
    await cmd_svc._music_auto_align_take(
        track_id=track_lib_3f.entry["id"], path=dummy_flac, lyrics=vocal_lyrics, duration_s=20.0
    )
    assert cache_checks == ["openai/whisper-large-v3-turbo"]
    assert not dummy_flac.with_suffix(".lrc").exists()


# -- 4. CC #59 Fixes: 5 Important + 5 Nits ---------------------------------------------------


def test_cc59_section_headers_colons_sublabels_and_duration_clamping():
    from dpc_client_core.music import lyrics_align

    # Nit #3: section headers with colons, dashes, and parentheses are recognized as headers, not sung words
    for hdr in ("[Verse 1: Lead]", "[Chorus - Choir]", "[Bridge (Acoustic)]", "[Pre-Chorus: Female Vocal]"):
        assert lyrics_align.is_section_header(hdr) is True, hdr
    for non_hdr in ("Real sung line [echo]", "[00:12.50]", "Plain lyric line"):
        assert lyrics_align.is_section_header(non_hdr) is False, non_hdr

    known = "[Verse 1: Lead]\nFirst sung line here\n[Chorus - Choir]\nSecond sung line now\n[Bridge (Acoustic)]\n"
    words = _words_from_timed_text([
        ("first", 3.0, 3.4), ("sung", 3.5, 3.8), ("line", 3.9, 4.2), ("here", 4.3, 4.7),
        ("second", 12.0, 12.4), ("sung", 12.5, 12.8), ("line", 12.9, 13.2), ("now", 13.3, 13.7),
        # Nit #4: hallucinated Whisper trailing timestamps far past real duration (20.0 s)
        ("trailing", 42.0, 44.0),
    ])
    res = lyrics_align.align_lyrics(known, words, duration_s=20.0)
    assert res.sufficient is True
    assert res.total_lyric_words == 8
    assert all(0.0 <= ln["t_start"] <= 20.0 for ln in res.lines)
    assert res.lines[-1]["t_start"] <= 20.0

    # Hallucinated matched word past duration_s is also clamped to real duration_s
    known2 = "Alpha line here\nBeta line end\n"
    words2 = _words_from_timed_text([
        ("alpha", 2.0, 2.5), ("line", 2.6, 3.0), ("here", 3.1, 3.5),
        ("beta", 38.0, 39.0), ("line", 39.1, 39.5), ("end", 39.6, 40.0),
    ])
    res2 = lyrics_align.align_lyrics(known2, words2, duration_s=20.0)
    assert res2.sufficient is True
    assert all(ln["t_start"] <= 20.0 for ln in res2.lines)


def test_cc59_rejects_unrelated_transcript_and_prefix_false_matches():
    from dpc_client_core.music import lyrics_align

    # Tightened fuzzy similarity must NOT reward unrelated 3-letter prefix matches with 0.9
    assert lyrics_align._token_match_score("star", "standing") < 0
    assert lyrics_align._token_match_score("problem", "procrastination") < 0
    assert lyrics_align._token_match_score("the", "she") < 0

    # CC #59 exact probe: 8 English lyric lines (~73 words) vs unrelated 37-word English transcript
    eight_lines = "\n".join([
        "Midnight shadows stretching across the silent boulevard tonight",
        "Golden lanterns flickering above the quiet cobblestone street",
        "Someone whispers softly through the velvet evening breeze",
        "Distant thunder rolling beyond the darkened mountain ridge",
        "Silver raindrops dancing on the broken window glass",
        "Echoes linger gently in the hollow empty hall",
        "Candlelight is fading while the winter moon ascends",
        "Waiting for tomorrow when the frozen rivers bend",
    ])
    unrelated_37_words = (
        "Actually today I went to the grocery store and bought some apples "
        "oranges bananas and a carton of fresh almond milk before driving "
        "back home through heavy traffic near the downtown bridge station "
        "earlier this afternoon"
    ).split()
    t = 1.0
    whisper_words = []
    for w in unrelated_37_words:
        whisper_words.append({"word": w, "start": round(t, 2), "end": round(t + 0.3, 2)})
        t += 0.4

    res = lyrics_align.align_lyrics(eight_lines, whisper_words, duration_s=30.0)
    assert res.sufficient is False

    # Short 1-4 word song requires >= 50% coverage and >= 1 match
    short_fail = lyrics_align.align_lyrics("One two three four", _words_from_timed_text([("one", 1.0, 1.3)]), duration_s=10.0)
    assert short_fail.sufficient is False
    short_ok = lyrics_align.align_lyrics("One two three four", _words_from_timed_text([("one", 1.0, 1.3), ("two", 1.4, 1.7)]), duration_s=10.0)
    assert short_ok.sufficient is True


@pytest.mark.asyncio
async def test_cc59_watched_vs_generated_sidecar_flac_tag_and_overwrite_guard(tmp_path):
    from dpc_client_core.music import audio_tags as at, provenance as pv

    dpc_home = tmp_path / "dpc"
    lib = MusicLibrary(dpc_home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    try:
        # 1. Watched-folder track: align MUST NOT write .lrc or touch audio file tags; refuses unrelated transcript
        watched_dir = tmp_path / "watched"
        watched_dir.mkdir()
        watched_flac = watched_dir / "Artist - Watched Song.flac"
        wav = make_wav(tmp_path / "src.wav", seconds=1.0)
        fake_flac(wav, watched_flac, {})
        at.flac_add_comments(watched_flac, {"LYRICS": "Alpha bravo charlie\nDelta echo foxtrot"}, once_key="")
        watched_bytes_before = watched_flac.read_bytes()

        folder = await lib.add_folder(str(watched_dir), watch=False, scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        [w_entry] = lib.list_entries()
        w_id = w_entry["id"]

        # Unrelated transcript rejected before saving anything
        async def bad_whisper(_p):
            return {"text": "completely different words", "language": "en", "duration": 20.0,
                    "words": _words_from_timed_text([("completely", 1.0, 1.3), ("different", 1.4, 1.8)])}

        lyr = MusicLyrics(
            dpc_home, lib, lrclib_enabled=lambda: False,
            tools=MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=lambda cmd, *, timeout: subprocess.CompletedProcess(
                cmd, 0, json.dumps({"format": {"duration": "20.0", "tags": {"LYRICS": "Alpha bravo charlie\nDelta echo foxtrot"}}, "streams": []}).encode(), b"")),
            transcriber=bad_whisper,
        )
        with pytest.raises(MusicLibraryError) as exc_bad:
            await lyr.align(w_id)
        assert exc_bad.value.code == "alignment_failed"
        assert not lyr.lyrics_path(w_id).exists()
        assert not watched_flac.with_suffix(".lrc").exists()

        # Pre-existing untimed .lrc beside watched audio stays untouched even after align succeeds
        watched_lrc = watched_flac.with_suffix(".lrc")
        watched_lrc.write_text("Untouched plain lrc note\n", encoding="utf-8")

        async def good_whisper(_p):
            return {"text": "Alpha bravo charlie Delta echo foxtrot", "language": "en", "duration": 20.0,
                    "words": _words_from_timed_text([
                        ("Alpha", 2.0, 2.4), ("bravo", 2.5, 2.9), ("charlie", 3.0, 3.4),
                        ("Delta", 7.0, 7.4), ("echo", 7.5, 7.9), ("foxtrot", 8.0, 8.5),
                    ])}

        lyr.transcriber = good_whisper
        res_w = await lyr.align(w_id)
        assert res_w["applied"] is True
        assert res_w["sidecar_written"] is False
        assert res_w["lrc_path"] is None
        assert lyr.lyrics_path(w_id).is_file()
        assert watched_lrc.read_text(encoding="utf-8") == "Untouched plain lrc note\n"
        assert watched_flac.read_bytes() == watched_bytes_before

        # 2. Existing synced lyrics (lrc, lrclib, tags, manual, aligned) require overwrite_manual=True
        with pytest.raises(MusicLibraryError) as exc_ow:
            await lyr.align(w_id, overwrite_manual=False)
        assert exc_ow.value.code == "overwrite_manual_required"

        for src_name in ("lrc", "lrclib", "tags", "manual", "aligned"):
            rec = lyrics_mod.make_lyrics(w_entry["source_hash"], src_name, [{"t_start": 1.0, "text": "Synced line"}], synced=True)
            await lyr._store(w_id, rec)
            with pytest.raises(MusicLibraryError) as exc_src:
                await lyr.align(w_id, overwrite_manual=False)
            assert exc_src.value.code == "overwrite_manual_required"

        # Pre-existing timed .lrc sidecar on a track with no DPC JSON yet also triggers overwrite_manual_required
        timed_flac = watched_dir / "Artist - Has Sidecar.flac"
        wav2 = make_wav(tmp_path / "src2.wav", seconds=1.5)
        fake_flac(wav2, timed_flac, {})
        timed_lrc = timed_flac.with_suffix(".lrc")
        timed_lrc.write_text("[00:02.00] Already timed line\n", encoding="utf-8")
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        s_entry = next(e for e in lib.list_entries() if Path(e["path"]).name == timed_flac.name)
        with pytest.raises(MusicLibraryError) as exc_sc:
            await lyr.align(s_entry["id"], overwrite_manual=False)
        assert exc_sc.value.code == "overwrite_manual_required"
        assert timed_lrc.read_text(encoding="utf-8") == "[00:02.00] Already timed line\n"

        # 3. Generated track inside _generated_root(): writes .lrc sidecar AND embeds SYNCEDLYRICS in FLAC without breaking DPC_PROVENANCE
        gen_dir = lib._generated_root() / "job_test1"
        gen_dir.mkdir(parents=True, exist_ok=True)
        gen_flac = gen_dir / "take_v1.flac"
        wav_gen = make_wav(tmp_path / "src_gen.wav", seconds=12.0)
        fake_flac(wav_gen, gen_flac, {})
        prov_json = json.dumps({"schema": "mp.provenance/1", "engine": "acestep.cpp"})
        at.flac_add_comments(gen_flac, {pv.TAG: prov_json, "LYRICS": "Alpha bravo charlie\nDelta echo foxtrot", "UNSYNCEDLYRICS": "Alpha bravo charlie\nDelta echo foxtrot"})
        from dpc_client_core.music.identity import read_flac_streaminfo
        before_md5 = read_flac_streaminfo(gen_flac).md5

        g_entry = await lib.register_generated(str(gen_flac), job_id="job_test1", title="Gen Track")
        lyr._tools = MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=lambda cmd, *, timeout: subprocess.CompletedProcess(
            cmd, 0, json.dumps({"format": {"duration": "20.0", "tags": {"LYRICS": "Alpha bravo charlie\nDelta echo foxtrot"}}, "streams": []}).encode(), b""))

        # Nit #2: verify align_result_to_lyrics is offloaded via asyncio.to_thread onto a worker thread
        import threading
        from dpc_client_core.music import lyrics_align
        main_tid = threading.get_ident()
        seen_tids = []
        orig_align_res = lyrics_align.align_result_to_lyrics
        def spy_align_res(*a, **kw):
            seen_tids.append(threading.get_ident())
            return orig_align_res(*a, **kw)
        lyr_orig = lyrics_align.align_result_to_lyrics
        lyrics_align.align_result_to_lyrics = spy_align_res
        try:
            res_g = await lyr.align(g_entry["id"])
        finally:
            lyrics_align.align_result_to_lyrics = lyr_orig
        assert seen_tids and all(tid != main_tid for tid in seen_tids)
        assert res_g["applied"] is True
        assert res_g["sidecar_written"] is True
        assert gen_flac.with_suffix(".lrc").is_file()
        tags_after = at.flac_read_comments(gen_flac)
        assert tags_after[pv.TAG] == prov_json
        assert tags_after["LYRICS"] == "Alpha bravo charlie\nDelta echo foxtrot"
        assert "SYNCEDLYRICS" in tags_after
        assert parse_lrc(tags_after["SYNCEDLYRICS"]).lines[0]["t_start"] == pytest.approx(2.0)
        assert read_flac_streaminfo(gen_flac).md5 == before_md5

        # Re-align with overwrite_manual=True updates SYNCEDLYRICS while keeping DPC_PROVENANCE and PCM MD5
        async def updated_whisper(_p):
            return {"text": "Alpha bravo charlie Delta echo foxtrot", "language": "en", "duration": 20.0,
                    "words": _words_from_timed_text([
                        ("Alpha", 4.0, 4.4), ("bravo", 4.5, 4.9), ("charlie", 5.0, 5.4),
                        ("Delta", 9.0, 9.4), ("echo", 9.5, 9.9), ("foxtrot", 10.0, 10.5),
                    ])}
        lyr.transcriber = updated_whisper
        res_realign = await lyr.align(g_entry["id"], overwrite_manual=True)
        assert res_realign["synced_tag_written"] is True
        tags_realigned = at.flac_read_comments(gen_flac)
        assert tags_realigned[pv.TAG] == prov_json
        assert parse_lrc(tags_realigned["SYNCEDLYRICS"]).lines[0]["t_start"] == pytest.approx(4.0)
        assert read_flac_streaminfo(gen_flac).md5 == before_md5

        # Corrupt/non-FLAC audio bytes inside _generated_root() fails tagging gracefully while keeping aligned lyrics + sidecar
        bad_flac = gen_dir / "bad_v1.flac"
        bad_flac.write_bytes(b"not a valid flac stream")
        b_entry = await lib.register_generated(str(bad_flac), job_id="job_test1", title="Bad Flac")
        await lyr._store(b_entry["id"], lyrics_mod.make_lyrics(b_entry["source_hash"], "tags", lyrics_mod.plain_lines("Alpha bravo charlie\nDelta echo foxtrot"), synced=False))
        res_bad_flac = await lyr.align(b_entry["id"])
        assert res_bad_flac["applied"] is True
        assert res_bad_flac["sidecar_written"] is True
        assert res_bad_flac["synced_tag_written"] is False

        # Symlink containment check: symlinked .lrc inside _generated_root() pointing outside MUST NOT be written through
        outside_target = watched_dir / "outside_secret.lrc"
        outside_target.write_text("KEEP ME", encoding="utf-8")
        sym_flac = gen_dir / "sym_v1.flac"
        wav3 = make_wav(tmp_path / "src3.wav", seconds=11.0)
        fake_flac(wav3, sym_flac, {})
        sym_lrc = sym_flac.with_suffix(".lrc")
        try:
            sym_lrc.symlink_to(outside_target)
            can_symlink = True
        except (OSError, NotImplementedError):
            can_symlink = False
        if can_symlink:
            sym_entry = await lib.register_generated(str(sym_flac), job_id="job_test1", title="Sym Track")
            await lyr._store(sym_entry["id"], lyrics_mod.make_lyrics(sym_entry["source_hash"], "tags", lyrics_mod.plain_lines("Alpha bravo charlie\nDelta echo foxtrot"), synced=False))
            res_sym = await lyr.align(sym_entry["id"])
            assert res_sym["applied"] is True
            assert res_sym["sidecar_written"] is False
            assert outside_target.read_text(encoding="utf-8") == "KEEP ME"
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_cc59_manual_align_local_whisper_only_and_bypasses_25mb_limit(tmp_path):
    flac = tmp_path / "Music" / "Big Track.flac"
    flac.parent.mkdir(parents=True, exist_ok=True)
    # Create a >25 MB file (30.6 MB) to test Important #5 (respect_size_limit=False)
    wav = make_wav(tmp_path / "src.wav", seconds=0.5)
    fake_flac(wav, flac, {})
    with open(flac, "ab") as f:
        f.write(b"\x00" * (31 * 1024 * 1024))
    assert flac.stat().st_size > 30 * 1024 * 1024

    lib = _TrackLib(flac)
    remote_calls = []

    async def remote_transcribe(**kw):
        remote_calls.append(kw)
        return {"status": "success", "text": "One two three", "provider": "remote"}

    class _AlignOnlySvc(MusicCommandsMixin):
        def __init__(self, providers, voice_provider):
            self.settings = SimpleNamespace(
                dpc_home_dir=tmp_path / "dpc",
                get=lambda s, k, f=None: f,
                get_local_transcription_enabled=lambda: True,
                get_local_transcription_max_file_size_mb=lambda: 25,
                get_local_transcription_max_duration_seconds=lambda: None,
            )
            self.llm_manager = SimpleNamespace(providers=providers, voice_provider=voice_provider)
            self.transcribe_audio = remote_transcribe
            self._lib = lib

        async def _music(self):
            return self._lib

    # Important #4: remote voice_provider or cloud openai provider MUST NOT upload audio and MUST return transcription_unavailable
    svc_remote = _AlignOnlySvc(providers={}, voice_provider="remote:peer1/whisper")
    lyr = await svc_remote._music_lyrics()
    lyr._tools = MediaTools(ffmpeg=None, ffprobe=None)
    from dpc_client_core.music.lyrics import make_lyrics, plain_lines
    await lyr._store(lib.entry["id"], make_lyrics(lib.entry["source_hash"], "tags", plain_lines("Hello world today\nSing it loud"), synced=False))
    ans_remote = await svc_remote.align_music_lyrics(track_id=lib.entry["id"])
    assert ans_remote["status"] == "error" and ans_remote["code"] == "transcription_unavailable"
    assert remote_calls == []

    svc_openai = _AlignOnlySvc(providers={"oa": SimpleNamespace(alias="oa", config={"type": "openai"})}, voice_provider="oa")
    lyr_oa = await svc_openai._music_lyrics()
    lyr_oa._tools = MediaTools(ffmpeg=None, ffprobe=None)
    ans_oa = await svc_openai.align_music_lyrics(track_id=lib.entry["id"])
    assert ans_oa["status"] == "error" and ans_oa["code"] == "transcription_unavailable"
    assert remote_calls == []

    # Important #5: local Whisper succeeds on >25 MB file (respect_size_limit=False)
    local_calls = []
    class _LocalW:
        alias = "w"
        config = {"type": "local_whisper"}
        model_name = "openai/whisper-large-v3-turbo"
        model_loaded = True
        async def transcribe(self, path, **kw):
            local_calls.append(Path(path).name)
            return {
                "text": "Hello world today Sing it loud",
                "language": "en",
                "duration": 30.0,
                "provider": "local_whisper",
                "words": _words_from_timed_text([
                    ("Hello", 1.0, 1.3), ("world", 1.4, 1.7), ("today", 1.8, 2.2),
                    ("Sing", 5.0, 5.3), ("it", 5.4, 5.6), ("loud", 5.7, 6.1),
                ]),
            }

    svc_local = _AlignOnlySvc(providers={"w": _LocalW()}, voice_provider=None)
    lyr_local = await svc_local._music_lyrics()
    lyr_local._tools = MediaTools(ffmpeg=None, ffprobe=None)
    ans_big = await svc_local.align_music_lyrics(track_id=lib.entry["id"])
    assert ans_big["status"] == "ok", ans_big
    assert ans_big["lyrics"]["source"] == "aligned"
    assert local_calls == ["Big Track.flac"]
    assert remote_calls == []


@pytest.mark.asyncio
async def test_cc59_auto_align_nonblocking_skips_queued_and_never_evicts_warm_engine(tmp_path, monkeypatch):
    from dpc_client_core.managers import card_lease
    from dpc_client_core.providers.whisper_provider import LocalWhisperProvider

    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    Settings(home)

    vocal_lyrics = "[verse]\nHello brave morning\nSing across the night\n"
    gate = asyncio.Event()
    calls = []

    class _SlowWhisper:
        alias = "w"
        config = {"type": "local_whisper"}
        model_name = "openai/whisper-large-v3-turbo"
        model_loaded = True
        async def transcribe(self, path, **kw):
            calls.append(Path(path).name)
            await gate.wait()
            return {
                "text": "Hello brave morning Sing across the night", "language": "en", "duration": 20.0,
                "provider": "local_whisper",
                "words": _words_from_timed_text([
                    ("Hello", 1.5, 1.8), ("brave", 1.85, 2.2), ("morning", 2.25, 2.7),
                    ("Sing", 5.0, 5.3), ("across", 5.35, 5.7), ("the", 5.75, 5.9), ("night", 5.95, 6.5),
                ]),
            }

    whisper = _SlowWhisper()

    class _Svc(MusicCommandsMixin):
        def __init__(self):
            self.settings = Settings(home)
            self.llm_manager = SimpleNamespace(providers={"w": whisper}, voice_provider=None)
            self._lib = None
        async def _music(self):
            return self._lib

    svc = _Svc()
    async with Rig(home, converter=_make_realistic_fake_flac(20.0)) as rig:
        svc._lib = rig.library
        svc._music_runner_instance = rig.runner
        rig.runner._auto_align = svc._music_auto_align_take

        # 1. Job enters review IMMEDIATELY without waiting for blocked Whisper; second queued job runs immediately too
        j1 = rig.store.create_job("generate_from_text", {
            "style": "indie rock", "lyrics": vocal_lyrics, "variants": 1, "duration_s": 20, "seed": 42, "engine": "acestep",
        })
        rig.runner.wake()
        d1 = await asyncio.wait_for(rig.wait_for(j1["id"], ("review",)), timeout=15.0)
        assert d1["state"] == "review"
        assert not gate.is_set()

        # Queue j2 while j1's background align is still blocked -> j2 must finish to review promptly
        j2 = rig.store.create_job("generate_from_text", {
            "style": "indie rock", "lyrics": vocal_lyrics, "variants": 1, "duration_s": 20, "seed": 43, "engine": "acestep",
        })
        rig.runner.wake()
        d2 = await asyncio.wait_for(rig.wait_for(j2["id"], ("review",)), timeout=15.0)
        assert d2["state"] == "review"
        gate.set()
        await rig.runner.wait_auto_align()

        # 2. Warm music engine + real LocalWhisperProvider on GPU -> MUST NOT evict/yield warm engine!
        yield_calls = []
        orig_yield = rig.engine._yield_card
        async def spy_yield(why):
            yield_calls.append(why)
            return await orig_yield(why)
        rig.engine.card_lease._on_yield = spy_yield
        assert rig.engine.alive() and rig.engine.card_lease.granted
        assert card_lease.queue_snapshot()["free"] is False

        real_gpu_whisper = LocalWhisperProvider("wg", {"type": "local_whisper", "model": "openai/whisper-large-v3-turbo", "device": "cuda"})
        real_gpu_whisper.model_loaded = True
        real_gpu_whisper._loaded_device = "cuda"
        svc.llm_manager.providers = {"wg": real_gpu_whisper}
        t1 = rig.store.list_takes(j1["id"])[0]
        p1 = rig.library.get_track_path(t1["library_track_id"])
        await svc._music_auto_align_take(track_id=t1["library_track_id"], path=p1, lyrics=vocal_lyrics, duration_s=20.0)
        assert yield_calls == []
        assert rig.engine.alive() and rig.engine.card_lease.granted

        # 3. Queued job present -> auto-align skips cleanly
        calls.clear()
        svc.llm_manager.providers = {"w": whisper}
        # Create a queued job while runner is paused from waking or simulate queued job
        j_queued = rig.store.create_job("generate_from_text", {
            "style": "ambient", "lyrics": None, "variants": 1, "duration_s": 20, "seed": 99, "engine": "acestep",
        })
        t2 = rig.store.list_takes(j2["id"])[0]
        p2 = rig.library.get_track_path(t2["library_track_id"])
        await svc._music_auto_align_take(track_id=t2["library_track_id"], path=p2, lyrics=vocal_lyrics, duration_s=20.0, job_id=j2["id"])
        assert calls == []
        rig.store.cancel_job(j_queued["id"])
        # Wait for cancelled/queued cleanup if needed

        # 4. Nit #1: MusicJobError from finish_job fails job cleanly rather than "internal error in the runner"
        orig_finish = rig.store.finish_job
        def boom_finish(jid):
            from dpc_client_core.music.jobs import MusicJobError
            raise MusicJobError("store_refused", "simulated finish refusal")
        monkeypatch.setattr(rig.store, "finish_job", boom_finish)
        j_err = rig.store.create_job("generate_from_text", {
            "style": "ambient", "lyrics": None, "variants": 1, "duration_s": 20, "seed": 50, "engine": "acestep",
        })
        rig.runner.wake()
        d_err = await rig.wait_for(j_err["id"], ("failed",))
        assert d_err["state"] == "failed"
        assert "simulated finish refusal" in (d_err.get("state_reason") or "")
        assert "internal error in the runner" not in (d_err.get("state_reason") or "")
        monkeypatch.setattr(rig.store, "finish_job", orig_finish)


@pytest.mark.asyncio
async def test_cc59_yue2_song_job_auto_aligns_and_embeds_syncedlyrics(tmp_path, monkeypatch):
    yue2_env(monkeypatch)
    home = tmp_path / "dpc_song"
    install_both(home)
    Settings(home)
    vocal_lyrics = ["[verse]", "Hello brave morning", "Sing across the night"]

    class _FakeW:
        alias = "w"
        config = {"type": "local_whisper"}
        model_name = "openai/whisper-large-v3-turbo"
        model_loaded = True
        async def transcribe(self, path, **kw):
            return {
                "text": "Hello brave morning Sing across the night", "language": "en", "duration": 20.0,
                "provider": "local_whisper",
                "words": _words_from_timed_text([
                    ("Hello", 1.5, 1.8), ("brave", 1.85, 2.2), ("morning", 2.25, 2.7),
                    ("Sing", 5.0, 5.3), ("across", 5.35, 5.7), ("the", 5.75, 5.9), ("night", 5.95, 6.5),
                ]),
            }

    class _Svc(MusicCommandsMixin):
        def __init__(self, lib):
            self.settings = Settings(home)
            self.llm_manager = SimpleNamespace(providers={"w": _FakeW()}, voice_provider=None)
            self._lib = lib
        async def _music(self):
            return self._lib

    async with DualRig(home) as drig:
        drig.runner._convert = _make_realistic_fake_flac(20.0)
        svc = _Svc(drig.library)
        svc._music_runner_instance = drig.runner
        drig.runner._auto_align = svc._music_auto_align_take

        # Register a dummy source track in library for generate_from_lyrics song job
        src_dir = tmp_path / "src_lib"
        src_dir.mkdir()
        wav = make_wav(tmp_path / "s.wav", seconds=0.5)
        src_flac = src_dir / "Source - Track.flac"
        fake_flac(wav, src_flac, {})
        folder = await drig.library.add_folder(str(src_dir), watch=False, scan=False)
        await drig.library.rescan_folder(folder["id"])
        await drig.library.wait_hashed()
        [src_entry] = drig.library.list_entries()

        job = drig.store.create_job(
            "generate_from_lyrics",
            {
                "engine": "yue2",
                "caption": "indie rock, male vocal",
                "lyrics_mode": "reuse",
                "lyrics": vocal_lyrics,
                "keep": {"bpm": 120},
                "reference": "description",
                "variants": 1,
                "duration_s": 30,
                "seed": 42,
                "backbone": "stock",
            },
            track_id=src_entry["id"],
            base_version_id="orig",
            source={
                "kind": "local",
                "content_hash": src_entry["content_hash"],
                "duration_s": 20.0,
                "title": "Source Track",
            },
        )
        drig.runner.wake()
        done = await drig.wait_for(job["id"], ("review", "failed"))
        assert done["state"] == "review", done.get("state_reason")
        await drig.runner.wait_auto_align()
        take = drig.store.list_takes(done["id"])[0]
        flac_path = drig.library.get_track_path(take["library_track_id"])
        assert flac_path.with_suffix(".lrc").is_file()
        from dpc_client_core.music import audio_tags as at
        tags = at.flac_read_comments(flac_path, max_block_bytes=1 << 20)
        assert "SYNCEDLYRICS" in tags
        assert parse_lrc(tags["SYNCEDLYRICS"]).lines[1]["t_start"] == pytest.approx(1.5)


# -- 5. CC #87 Fixes: Important #1 + 5 Nits ---------------------------------------------------


def _make_fake_gpu_whisper(monkeypatch, *, alias: str = "wg"):
    from dpc_client_core.providers import model_sizes
    from dpc_client_core.providers.whisper_provider import LocalWhisperProvider

    monkeypatch.setattr(model_sizes, "is_model_cached", lambda _model: True)
    p = LocalWhisperProvider(
        alias,
        {
            "type": "local_whisper",
            "model": "openai/whisper-large-v3-turbo",
            "device": "cuda",
            "idle_unload_s": 0,
        },
    )
    p.loads = 0
    p.unloads = 0
    p.ran = []

    def load():
        p.loads += 1
        p.pipeline = object()
        p.model_loaded = True
        p._note_load("cuda", 0.05)

    def unload():
        p.unloads += 1
        p.pipeline = None
        p.model_loaded = False
        return True

    async def run(path, **_kw):
        p.ran.append(Path(path).name)
        return {
            "text": "Hello brave morning Sing across the night",
            "language": "en",
            "duration": 20.0,
            "provider": "local_whisper",
            "words": _words_from_timed_text([
                ("Hello", 1.5, 1.8), ("brave", 1.85, 2.2), ("morning", 2.25, 2.7),
                ("Sing", 5.0, 5.3), ("across", 5.35, 5.7), ("the", 5.75, 5.9), ("night", 5.95, 6.5),
            ]),
        }

    monkeypatch.setattr(p, "_load_model", load)
    monkeypatch.setattr(p, "_unload_model", unload)
    monkeypatch.setattr(p, "_transcribe_loaded", run)
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_name", ["acestep", "yue2"])
async def test_cc87_important1_auto_align_runs_with_real_card_lease_after_warm_engine_idle_stop(
    tmp_path, monkeypatch, engine_name,
):
    from dpc_client_core.managers import card_lease
    from dpc_client_core.music import audio_tags as at

    home = tmp_path / f"dpc_{engine_name}"
    if engine_name == "acestep":
        free_env(monkeypatch)
        install_fake_engine(home)
        rig_ctx = Rig(home, converter=_make_realistic_fake_flac(20.0), engine_settings={"acestep_idle_stop_s": "0.2"})
    else:
        yue2_env(monkeypatch)
        install_both(home)
        rig_ctx = DualRig(home, yue2_settings={"yue2_idle_stop_s": "0.2"})

    Settings(home)
    whisper = _make_fake_gpu_whisper(monkeypatch)
    vocal_lyrics = "[verse]\nHello brave morning\nSing across the night\n"

    class _Svc(MusicCommandsMixin):
        def __init__(self, lib):
            self.settings = Settings(home)
            self.llm_manager = SimpleNamespace(providers={"wg": whisper}, voice_provider="wg")
            self._lib = lib

        async def _music(self):
            return self._lib

    async with rig_ctx as rig:
        if engine_name == "yue2":
            rig.runner._convert = _make_realistic_fake_flac(20.0)
        engine = rig.engine if engine_name == "acestep" else rig.yue2
        svc = _Svc(rig.library)
        svc._music_runner_instance = rig.runner
        rig.runner._auto_align = svc._music_auto_align_take

        yield_calls = []
        orig_yield = engine._yield_card

        async def spy_yield(why):
            yield_calls.append(why)
            return await orig_yield(why)

        engine.card_lease._on_yield = spy_yield

        params = {
            "style": "indie rock",
            "lyrics": vocal_lyrics,
            "variants": 1,
            "duration_s": 30 if engine_name == "yue2" else 20,
            "seed": 42,
            "engine": engine_name,
        }
        j1 = rig.store.create_job("generate_from_text", params)
        rig.runner.wake()
        d1 = await asyncio.wait_for(rig.wait_for(j1["id"], ("review",)), timeout=15.0)
        assert d1["state"] == "review"
        assert engine.alive() and engine.card_lease.granted
        assert yield_calls == []
        pid_1 = engine._proc.pid

        # A second job while the engine is still warm reuses the same warm child without eviction.
        j2 = rig.store.create_job("generate_from_text", {**params, "seed": 43})
        rig.runner.wake()
        d2 = await asyncio.wait_for(rig.wait_for(j2["id"], ("review",)), timeout=15.0)
        assert d2["state"] == "review"
        assert engine.alive() and engine._proc.pid == pid_1
        assert yield_calls == []

        # Once the warm engine idle-stops and frees the real CardLease, GPU Whisper auto-aligns both takes.
        await rig.runner.wait_auto_align(timeout_s=10.0)
        assert not engine.alive()
        assert (engine._last_exit or {}).get("why") == "idle_unload"
        assert yield_calls == []
        assert whisper.loads >= 1
        assert whisper.unloads == whisper.loads
        assert len(whisper.ran) == 2

        for jid in (j1["id"], j2["id"]):
            take = rig.store.list_takes(jid)[0]
            tid = take["library_track_id"]
            flac_path = rig.library.get_track_path(tid)
            assert flac_path is not None and flac_path.with_suffix(".lrc").is_file()
            got = await (await svc._music_lyrics()).get(tid)
            assert got["lyrics"]["source"] == "aligned"
            assert got["lyrics"]["lines"][1]["t_start"] == pytest.approx(1.5)
            tags = at.flac_read_comments(flac_path, max_block_bytes=1 << 20)
            assert "SYNCEDLYRICS" in tags

        snap = card_lease.queue_snapshot()
        assert snap["free"] is True and snap["waiters"] == []


@pytest.mark.asyncio
async def test_cc87_important1_auto_align_stays_out_of_way_while_card_busy_or_waited(tmp_path, monkeypatch):
    from dpc_client_core.managers import card_lease
    from tests._card_lease_helpers import BOUND, hold_open, whisper as lease_whisper

    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    Settings(home)
    gpu_w = _make_fake_gpu_whisper(monkeypatch)
    vocal_lyrics = "[verse]\nHello brave morning\nSing across the night\n"

    class _Svc(MusicCommandsMixin):
        def __init__(self, lib):
            self.settings = Settings(home)
            self.llm_manager = SimpleNamespace(providers={"wg": gpu_w}, voice_provider="wg")
            self._lib = lib

        async def _music(self):
            return self._lib

    async with Rig(home, converter=_make_realistic_fake_flac(20.0), engine_settings={"acestep_idle_stop_s": "0.15"}) as rig:
        svc = _Svc(rig.library)
        svc._music_runner_instance = rig.runner

        # 1. While engine is actively busy in_use() (uses > 0), auto-align skips and does not run/evict.
        async with rig.engine.in_use("active music job", wait_s=BOUND):
            await rig.engine.start()
            assert rig.engine.card_lease._uses
            dummy_flac = tmp_path / "dummy.flac"
            dummy_flac.write_bytes(b"fake")
            await svc._music_auto_align_take(
                track_id="trk_0000000000000001",
                path=dummy_flac,
                lyrics=vocal_lyrics,
                duration_s=20.0,
            )
            assert gpu_w.ran == []
            assert rig.engine.alive() and rig.engine.card_lease.granted

        await rig.engine.stop("ui_stop")
        assert card_lease.queue_snapshot()["free"] is True

        # 2. While another CardLease client holds an active use, auto-align stays out of the way.
        other = lease_whisper("other")
        release = asyncio.Event()
        holder = asyncio.create_task(hold_open(other.lease, "dictation", release))
        while not other.lease.granted:
            await asyncio.sleep(0.01)
        try:
            await svc._music_auto_align_take(
                track_id="trk_0000000000000001",
                path=tmp_path / "dummy.flac",
                lyrics=vocal_lyrics,
                duration_s=20.0,
            )
            assert gpu_w.ran == []
            assert other.yields == []
        finally:
            release.set()
            await asyncio.wait_for(holder, BOUND)
            other.lease.release("test")


@pytest.mark.asyncio
async def test_cc87_nit1_background_align_does_not_retry_internal_typeerror(tmp_path):
    store = MusicJobStore(tmp_path)
    store.open()
    lib = MusicLibrary(tmp_path, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    runner = MusicJobRunner(store, [], lambda: asyncio.sleep(0, result=lib), dpc_home=tmp_path)

    calls_with_bug = []

    async def broken_align(*, track_id, path, lyrics, duration_s=None, replay=None, job_id=None):
        calls_with_bug.append((track_id, job_id))
        raise TypeError("internal type error inside aligner")

    item = {"track_id": "trk_a1b2c3d4e5f60001", "path": tmp_path / "a.flac", "lyrics": "hi", "duration_s": 10.0, "replay": None}
    runner._auto_align = broken_align
    await runner._run_background_align("job_1", [item])
    assert calls_with_bug == [("trk_a1b2c3d4e5f60001", "job_1")]

    calls_no_job_id = []

    async def legacy_align(*, track_id, path, lyrics, duration_s=None, replay=None):
        calls_no_job_id.append(track_id)

    runner._auto_align = legacy_align
    await runner._run_background_align("job_2", [item])
    assert calls_no_job_id == ["trk_a1b2c3d4e5f60001"]

    calls_kwargs = []

    async def kwargs_align(**kw):
        calls_kwargs.append(kw.get("job_id"))

    runner._auto_align = kwargs_align
    await runner._run_background_align("job_3", [item])
    assert calls_kwargs == ["job_3"]


@pytest.mark.asyncio
async def test_cc87_nit2_pending_align_cleared_on_failed_or_cancelled_job(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)
    Settings(home)

    async def noop_align(**_kw):
        return None

    async with Rig(home, converter=_make_realistic_fake_flac(20.0)) as rig:
        rig.runner._auto_align = noop_align

        # 1. Unexpected exception in _complete_generation after variant 1 populated _pending_align
        async def boom_complete(_jid):
            raise RuntimeError("unexpected failure after store_take")

        monkeypatch.setattr(rig.runner, "_complete_generation", boom_complete)
        j1 = rig.store.create_job("generate_from_text", {
            "style": "indie rock", "lyrics": "[verse]\nHello brave morning\n", "variants": 1,
            "duration_s": 20, "seed": 42, "engine": "acestep",
        })
        rig.runner.wake()
        d1 = await rig.wait_for(j1["id"], ("failed",))
        assert d1["state"] == "failed"
        assert j1["id"] not in rig.runner._pending_align
        assert rig.runner._pending_align == {}

        # 2. asyncio.CancelledError raised inside run_job after variant 1 populated _pending_align
        await rig.runner.stop()

        async def cancel_complete(_jid):
            raise asyncio.CancelledError()

        monkeypatch.setattr(rig.runner, "_complete_generation", cancel_complete)
        j2 = rig.store.create_job("generate_from_text", {
            "style": "indie rock", "lyrics": "[verse]\nHello brave morning\n", "variants": 1,
            "duration_s": 20, "seed": 43, "engine": "acestep",
        })
        with pytest.raises(asyncio.CancelledError):
            await rig.runner.run_job(j2["id"])
        assert j2["id"] not in rig.runner._pending_align
        assert rig.runner._pending_align == {}


def test_cc87_nit3_bg_align_lock_recreated_on_runner_restart_across_event_loops(tmp_path):
    store = MusicJobStore(tmp_path)
    store.open()
    lib = MusicLibrary(tmp_path, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)

    async def slow_align(**_kw):
        await asyncio.sleep(0.02)

    runner = MusicJobRunner(store, [], lambda: asyncio.sleep(0, result=lib), dpc_home=tmp_path, auto_align=slow_align)
    item = {"track_id": "trk_a1b2c3d4e5f60001", "path": tmp_path / "a.flac", "lyrics": "hi", "duration_s": 10.0, "replay": None}

    async def run_phase():
        runner.start()
        try:
            await asyncio.gather(
                runner._run_background_align("job_a", [item]),
                runner._run_background_align("job_b", [item]),
            )
            return runner._bg_align_lock
        finally:
            await runner.stop()

    loop1 = asyncio.new_event_loop()
    try:
        lock1 = loop1.run_until_complete(run_phase())
    finally:
        loop1.close()

    loop2 = asyncio.new_event_loop()
    try:
        lock2 = loop2.run_until_complete(run_phase())
    finally:
        loop2.close()

    assert lock1 is not None and lock2 is not None
    assert lock2 is not lock1


@pytest.mark.asyncio
async def test_cc87_nit4_lyrics_align_respects_real_short_track_duration(tmp_path):
    dpc_home = tmp_path / "dpc"
    music_dir = tmp_path / "Music"
    music_dir.mkdir(parents=True)
    short_flac = music_dir / "Artist - Short Clip.flac"
    wav = make_wav(tmp_path / "short.wav", seconds=0.3)
    fake_flac(wav, short_flac, {})

    lib = MusicLibrary(dpc_home, tools=MediaTools(ffmpeg=None, ffprobe=None), save_delay_s=0.01)
    await lib.open()
    try:
        folder = await lib.add_folder(str(music_dir), watch=False, scan=False)
        await lib.rescan_folder(folder["id"])
        await lib.wait_hashed()
        [entry] = lib.list_entries()

        async def short_whisper(_path):
            return {
                "text": "Alpha Beta",
                "language": "en",
                "duration": 5.0,
                "words": _words_from_timed_text([
                    ("Alpha", 0.1, 0.2),
                    ("Beta", 2.0, 2.4),
                ]),
            }

        lyr = MusicLyrics(
            dpc_home,
            lib,
            lrclib_enabled=lambda: False,
            tools=MediaTools(ffmpeg=None, ffprobe=None),
            transcriber=short_whisper,
        )
        res = await lyr.align(entry["id"], lyrics_text="Alpha\nBeta\n")
        assert res["applied"] is True
        assert all(ln["t_start"] <= 0.3 for ln in res["lyrics"]["lines"])
    finally:
        await lib.stop()


@pytest.mark.asyncio
async def test_cc87_nit5_manual_align_finds_local_whisper_regardless_of_voice_provider(tmp_path):
    flac = tmp_path / "Music" / "Voice Provider Fallback.flac"
    flac.parent.mkdir(parents=True, exist_ok=True)
    wav = make_wav(tmp_path / "src.wav", seconds=1.0)
    fake_flac(wav, flac, {})
    lib = _TrackLib(flac)

    remote_calls = []

    async def remote_transcribe(**kw):
        remote_calls.append(kw)
        return {"status": "success", "text": "Hello world today", "provider": "remote"}

    local_calls = []

    class _LocalW:
        alias = "w"
        config = {"type": "local_whisper"}
        model_name = "openai/whisper-large-v3-turbo"
        model_loaded = True

        async def transcribe(self, path, **kw):
            local_calls.append(Path(path).name)
            return {
                "text": "Hello world today Sing it loud",
                "language": "en",
                "duration": 20.0,
                "provider": "local_whisper",
                "words": _words_from_timed_text([
                    ("Hello", 1.0, 1.3), ("world", 1.4, 1.7), ("today", 1.8, 2.2),
                    ("Sing", 5.0, 5.3), ("it", 5.4, 5.6), ("loud", 5.7, 6.1),
                ]),
            }

    class _AlignSvc(MusicCommandsMixin):
        def __init__(self, providers, voice_provider):
            self.settings = SimpleNamespace(
                dpc_home_dir=tmp_path / "dpc",
                get=lambda s, k, f=None: f,
                get_local_transcription_enabled=lambda: True,
                get_local_transcription_max_file_size_mb=lambda: 25,
                get_local_transcription_max_duration_seconds=lambda: None,
            )
            self.llm_manager = SimpleNamespace(providers=providers, voice_provider=voice_provider)
            self.transcribe_audio = remote_transcribe
            self._lib = lib

        async def _music(self):
            return self._lib

    from dpc_client_core.music.lyrics import make_lyrics, plain_lines

    for vp, providers in (
        ("remote:peer1/whisper", {"w": _LocalW()}),
        ("oa", {"oa": SimpleNamespace(alias="oa", config={"type": "openai"}), "w": _LocalW()}),
    ):
        local_calls.clear()
        svc = _AlignSvc(providers=providers, voice_provider=vp)
        lyr = await svc._music_lyrics()
        lyr._tools = MediaTools(ffmpeg=None, ffprobe=None)
        await lyr._store(lib.entry["id"], make_lyrics(lib.entry["source_hash"], "tags", plain_lines("Hello world today\nSing it loud"), synced=False))
        ans = await svc.align_music_lyrics(track_id=lib.entry["id"])
        assert ans["status"] == "ok", (vp, ans)
        assert ans["lyrics"]["source"] == "aligned"
        assert local_calls == ["Voice Provider Fallback.flac"]
        assert remote_calls == []

    # Still refuses with transcription_unavailable when no local_whisper exists anywhere
    svc_none = _AlignSvc(providers={"oa": SimpleNamespace(alias="oa", config={"type": "openai"})}, voice_provider="remote:peer1/whisper")
    lyr_none = await svc_none._music_lyrics()
    lyr_none._tools = MediaTools(ffmpeg=None, ffprobe=None)
    await lyr_none._store(lib.entry["id"], make_lyrics(lib.entry["source_hash"], "tags", plain_lines("Hello world today\nSing it loud"), synced=False))
    ans_none = await svc_none.align_music_lyrics(track_id=lib.entry["id"])
    assert ans_none["status"] == "error" and ans_none["code"] == "transcription_unavailable"
    assert remote_calls == []
