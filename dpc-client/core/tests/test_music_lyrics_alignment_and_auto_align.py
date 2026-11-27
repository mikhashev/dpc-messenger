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
        "Leading line one\n"
        "Anchored early\n"
        "Middle unmatched A\n"
        "Middle unmatched B\n"
        "Anchored late\n"
        "Trailing line A\n"
        "Trailing line B\n"
    )
    whisper_words = _words_from_timed_text([
        ("anchored", 10.0, 10.5),
        ("early", 10.6, 11.0),
        ("anchored", 25.0, 25.5),
        ("late", 25.6, 26.0),
    ])

    res = lyrics_align.align_lyrics(known, whisper_words, duration_s=40.0)
    assert res.sufficient is True
    t = [ln["t_start"] for ln in res.lines]
    # All timestamps finite and within [0, 40.0], strictly monotonic for sung lines
    assert all(0.0 <= x <= 40.0 for x in t)
    assert all(t[i] <= t[i + 1] for i in range(len(t) - 1))
    assert t[1] < t[2] == pytest.approx(10.0, abs=0.01)
    assert t[2] < t[3] < t[4] < t[5] == pytest.approx(25.0, abs=0.01)
    assert t[5] < t[6] < t[7] <= 40.0

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

    # .lrc sidecar written beside the audio file and readable back
    sidecar = flac.with_suffix(".lrc")
    assert sidecar.is_file()
    reparsed = parse_lrc(sidecar.read_text(encoding="utf-8"))
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

    # 3a. ACE-Step vocal take: auto-aligned on review with source="aligned" and .lrc sidecar
    async with Rig(home) as rig:
        rig.runner._auto_align = cmd_svc._music_auto_align_take
        job = rig.store.create_job("generate_from_text", {
            "style": "indie rock", "lyrics": vocal_lyrics, "variants": 1, "duration_s": 20, "seed": 42, "engine": "acestep",
        })
        rig.runner.wake()
        done = await rig.wait_for(job["id"], ("review",))
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
        assert d3["state"] == "review"
        assert len(aligned_calls) == before_calls

        # 3d. auto_align_lyrics = false skips Whisper completely
        cmd_svc.settings._config.set("music", "auto_align_lyrics", "false")
        j4 = rig.store.create_job("generate_from_text", {
            "style": "pop", "lyrics": vocal_lyrics, "variants": 1, "duration_s": 20, "seed": 45, "engine": "acestep",
        })
        rig.runner.wake()
        d4 = await rig.wait_for(j4["id"], ("review",))
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
