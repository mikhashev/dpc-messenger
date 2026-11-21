"""The lyrics chain's "transcribe" step (ADR-051 D6): an explicit action that runs DPC's
existing transcription on the track's file and stores its segments as timed lyrics,
`source: "transcribe"`. The automatic chain never runs it; the person's own lyrics are
never replaced without `overwrite_manual`.

The transcription is mocked at its boundary: `MusicLyrics`'s `transcriber`, and for the
command layer the provider object (`provider.transcribe`) and `transcribe_audio`.
No model is loaded and nothing reaches the network.
"""

import asyncio
import subprocess
import json

import httpx
import pytest

from dpc_client_core.music import commands
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from dpc_client_core.music.identity import MediaTools
from dpc_client_core.music.library import MusicLibraryError
from dpc_client_core.music.lyrics import MusicLyrics, lines_from_segments, transcript_lyrics
from dpc_client_core.providers.base import ModelNotCachedError

TRACK_ID = "trk_0123456789abcdef"

SEGMENTS = [
    {"start": 12.5, "end": 15.0, "text": " First line "},
    {"start": 15.0, "end": 18.25, "text": "Second\nline"},
    {"start": 20.0, "end": 20.0, "text": "no end"},
    {"start": 21.0, "end": 22.0, "text": "   "},
]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def refuse(self, request):
        raise AssertionError(f"a test tried to reach the network: {request.url}")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


class FakeLibrary:
    def __init__(self, path, *, source_hash="sha256:aa"):
        self.path = path
        self.entry = {
            "id": TRACK_ID, "title": path.stem if path else "web", "artist": "Band", "album": None,
            "duration_s": 215.0, "source_hash": source_hash,
            "source": {"kind": "local", "name": path.name if path else "web", "uri": "file:///x"},
        }

    def get_entry(self, track_id):
        if track_id != TRACK_ID:
            raise MusicLibraryError("unknown_track", f"no track {track_id}")
        return dict(self.entry)

    def get_track_path(self, track_id):
        return self.path


class FakeTranscriber:
    """Records each call; answers `result`, or raises `error`."""

    def __init__(self, result=None, error=None, gate=None):
        self.result = result if result is not None else {
            "text": "First line Second line", "language": "en", "duration": 215.0,
            "provider": "local_whisper", "model": "openai/whisper-large-v3-turbo", "segments": SEGMENTS}
        self.error = error
        self.gate = gate
        self.calls = []

    async def __call__(self, path):
        self.calls.append(path)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.result


class ForbiddenClient:
    async def get(self, q):
        raise AssertionError("lrclib.net was asked")


def no_probe(cmd, *, timeout):
    out = {"format": {"duration": "215.0", "tags": {}}, "streams": [{"codec_type": "audio"}]}
    return subprocess.CompletedProcess(cmd, 0, json.dumps(out).encode(), b"")


@pytest.fixture
def audio(tmp_path):
    folder = tmp_path / "Music"
    folder.mkdir()
    path = folder / "Band - Song.mp3"
    path.write_bytes(b"ID3" + b"\x00" * 64)
    return path


def make(tmp_path, library, transcriber):
    tools = MediaTools(ffmpeg=None, ffprobe="ffprobe", runner=no_probe)
    return MusicLyrics(tmp_path / "dpc", library, tools=tools, client=ForbiddenClient(),
                       lrclib_enabled=lambda: False, transcriber=transcriber)


def stored_file(tmp_path):
    return tmp_path / "dpc" / "music" / "lyrics" / f"{TRACK_ID}.json"


# -- the result → lyrics ---------------------------------------------------------------

def test_segments_become_timed_lines():
    assert lines_from_segments(SEGMENTS) == [
        {"t_start": 12.5, "t_end": 15.0, "text": "First line"},
        {"t_start": 15.0, "t_end": 18.25, "text": "Second line"},
        {"t_start": 20.0, "text": "no end"},
    ]
    # Unordered input is sorted; a segment without a start, or a NaN one, is dropped.
    assert lines_from_segments([{"start": 5, "text": "b"}, {"start": 1, "text": "a"},
                                {"start": None, "text": "x"}, {"start": float("nan"), "text": "y"},
                                "junk"]) == [{"t_start": 1.0, "text": "a"}, {"t_start": 5.0, "text": "b"}]


def test_a_result_without_words_is_no_speech():
    with pytest.raises(MusicLibraryError) as e:
        transcript_lyrics("h", {"text": "  ", "segments": []})
    assert e.value.code == "no_speech"


@pytest.mark.asyncio
async def test_timestamps_become_lrc_lines_stored_as_transcribe(tmp_path, audio):
    transcriber = FakeTranscriber()
    svc = make(tmp_path, FakeLibrary(audio), transcriber)
    answer = await svc.transcribe(TRACK_ID)
    assert transcriber.calls == [audio]
    rec = answer["lyrics"]
    assert answer["applied"] is True and answer["candidate"] is None
    assert rec["source"] == "transcribe" and "synced" not in rec and rec["language"] == "en"
    assert rec["lines"][0] == {"t_start": 12.5, "t_end": 15.0, "text": "First line"}
    assert answer["lrc"].splitlines() == [
        "[la:en]",
        "[00:12.50] First line <00:15.00>",
        "[00:15.00] Second line <00:18.25>",
        "[00:20.00] no end",
    ]
    assert answer["transcription"] == {
        "provider": "local_whisper", "model": "openai/whisper-large-v3-turbo", "language": "en",
        "duration_s": 215.0, "segments": 3, "synced": True}
    on_disk = json.loads(stored_file(tmp_path).read_text(encoding="utf-8"))
    assert on_disk == rec
    # The person's folder is never written.
    assert sorted(p.name for p in audio.parent.iterdir()) == ["Band - Song.mp3"]
    again = await svc.get(TRACK_ID)
    assert again["from_store"] is True and again["lyrics"]["source"] == "transcribe"
    assert again["transcribe"]["state"] == "done" and again["transcribe"]["applied"] is True


@pytest.mark.asyncio
async def test_no_timestamps_give_unsynced_lines(tmp_path, audio):
    transcriber = FakeTranscriber({"text": "one\ntwo", "language": "unknown", "duration": 0,
                                   "provider": "openai"})
    svc = make(tmp_path, FakeLibrary(audio), transcriber)
    answer = await svc.transcribe(TRACK_ID)
    rec = answer["lyrics"]
    assert rec["synced"] is False and rec["language"] is None
    assert rec["lines"] == [{"t_start": None, "text": "one"}, {"t_start": None, "text": "two"}]
    assert answer["transcription"]["synced"] is False and answer["transcription"]["segments"] == 0
    assert answer["transcription"]["model"] is None and answer["transcription"]["provider"] == "openai"
    assert answer["lrc"] == "one\ntwo"


# -- the person's own lyrics -------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["manual", "edited"])
async def test_the_manual_guard_refuses_without_overwrite_manual(tmp_path, audio, prior):
    transcriber = FakeTranscriber()
    svc = make(tmp_path, FakeLibrary(audio), transcriber)
    await svc.save_manual(TRACK_ID, "[00:01.00] mine")
    if prior == "edited":
        # Lyrics from the chain the person then edited: source manual with edited, as
        # save_manual writes over chain lyrics; a tags record marked edited also counts.
        record = json.loads(stored_file(tmp_path).read_text(encoding="utf-8"))
        record.update(source="tags", edited=True)
        stored_file(tmp_path).write_text(json.dumps(record), encoding="utf-8")
    before = stored_file(tmp_path).read_bytes()
    with pytest.raises(MusicLibraryError) as e:
        await svc.transcribe(TRACK_ID)
    assert e.value.code == "overwrite_manual_required"
    assert transcriber.calls == []  # refused before anything runs
    assert stored_file(tmp_path).read_bytes() == before
    with pytest.raises(MusicLibraryError):
        await svc.transcribe(TRACK_ID, overwrite_manual=False)
    answer = await svc.transcribe(TRACK_ID, overwrite_manual=True)
    assert answer["applied"] is True and answer["lyrics"]["source"] == "transcribe"
    assert len(transcriber.calls) == 1


@pytest.mark.asyncio
async def test_chain_lyrics_are_replaced_by_the_explicit_click(tmp_path, audio):
    audio.with_suffix(".lrc").write_text("[00:01.00] from the sidecar", encoding="utf-8")
    svc = make(tmp_path, FakeLibrary(audio), FakeTranscriber())
    assert (await svc.get(TRACK_ID))["lyrics"]["source"] == "lrc"
    answer = await svc.transcribe(TRACK_ID)
    assert answer["applied"] is True and answer["lyrics"]["source"] == "transcribe"


@pytest.mark.asyncio
async def test_own_lyrics_saved_during_the_run_are_kept(tmp_path, audio):
    gate = asyncio.Event()
    transcriber = FakeTranscriber(gate=gate)
    svc = make(tmp_path, FakeLibrary(audio), transcriber)
    task = asyncio.create_task(svc.transcribe(TRACK_ID))
    async def _called():
        while not transcriber.calls:
            await asyncio.sleep(0)
    await asyncio.wait_for(_called(), 5)
    # While it runs: the player still reads the track's lyrics, sees the run, and a
    # second click is refused.
    during = await svc.get(TRACK_ID)
    assert during["transcribe"]["state"] == "running"
    assert [s for s in during["chain"] if s["step"] == "transcribe"][0]["state"] == "running"
    with pytest.raises(MusicLibraryError) as e:
        await svc.transcribe(TRACK_ID)
    assert e.value.code == "transcribe_running"
    await svc.save_manual(TRACK_ID, "[00:02.00] typed meanwhile")
    gate.set()
    answer = await task
    assert answer["applied"] is False and answer["lyrics"]["source"] == "manual"
    assert answer["candidate"]["source"] == "transcribe" and "kept" in answer
    assert json.loads(stored_file(tmp_path).read_text(encoding="utf-8"))["source"] == "manual"


# -- never in the automatic chain ------------------------------------------------------

@pytest.mark.asyncio
async def test_the_automatic_chain_never_calls_transcribe(tmp_path, audio):
    transcriber = FakeTranscriber(error=AssertionError("the automatic chain transcribed"))
    svc = make(tmp_path, FakeLibrary(audio), transcriber)
    for refresh in (False, True, False):
        got = await svc.get(TRACK_ID, refresh=refresh)
        step = [s for s in got["chain"] if s["step"] == "transcribe"][0]
        assert step["state"] == "not_run" and "Transcribe" in step["detail"]
        assert got["lyrics"] is None and got["transcribe"] is None
    assert transcriber.calls == []
    assert not stored_file(tmp_path).exists()


@pytest.mark.asyncio
async def test_the_chain_reports_done_for_stored_transcribed_lyrics(tmp_path, audio):
    transcriber = FakeTranscriber()
    svc = make(tmp_path, FakeLibrary(audio), transcriber)
    await svc.transcribe(TRACK_ID)
    refreshed = await svc.get(TRACK_ID, refresh=True)
    step = [s for s in refreshed["chain"] if s["step"] == "transcribe"][0]
    assert step["state"] == "done"
    assert refreshed["lyrics"]["source"] == "transcribe"
    assert len(transcriber.calls) == 1  # the refresh did not transcribe again


# -- errors -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_transcription_error_is_surfaced_and_remembered(tmp_path, audio):
    transcriber = FakeTranscriber(error=RuntimeError("Voice message too long for available GPU VRAM (215s)"))
    svc = make(tmp_path, FakeLibrary(audio), transcriber)
    with pytest.raises(MusicLibraryError) as e:
        await svc.transcribe(TRACK_ID)
    assert e.value.code == "transcription_failed" and "GPU VRAM" in e.value.message
    got = await svc.get(TRACK_ID)
    assert got["transcribe"]["state"] == "error" and "GPU VRAM" in got["transcribe"]["message"]
    assert not stored_file(tmp_path).exists()
    # The run is over, so the next click is allowed.
    transcriber.error = None
    assert (await svc.transcribe(TRACK_ID))["applied"] is True


@pytest.mark.asyncio
async def test_without_a_file_or_a_transcriber_it_is_refused(tmp_path, audio):
    with pytest.raises(MusicLibraryError) as e:
        await make(tmp_path, FakeLibrary(None), FakeTranscriber()).transcribe(TRACK_ID)
    assert e.value.code == "no_file"
    with pytest.raises(MusicLibraryError) as e:
        await make(tmp_path, FakeLibrary(audio), None).transcribe(TRACK_ID)
    assert e.value.code == "transcription_unavailable"
    with pytest.raises(MusicLibraryError) as e:
        await make(tmp_path, FakeLibrary(audio), FakeTranscriber()).transcribe("trk_ffffffffffffffff")
    assert e.value.code == "unknown_track"


@pytest.mark.asyncio
async def test_no_words_is_an_error_not_empty_lyrics(tmp_path, audio):
    svc = make(tmp_path, FakeLibrary(audio), FakeTranscriber({"text": "", "segments": [], "provider": "local_whisper"}))
    with pytest.raises(MusicLibraryError) as e:
        await svc.transcribe(TRACK_ID)
    assert e.value.code == "no_speech"
    assert not stored_file(tmp_path).exists()


# -- the local_api door ----------------------------------------------------------------

class _Settings:
    def __init__(self, home, local_enabled=True, local_max_mb=25):
        self.dpc_home_dir = home
        self.local_enabled = local_enabled
        self.local_max_mb = local_max_mb

    def get(self, section, key, fallback=None):
        return fallback

    def get_local_transcription_enabled(self):
        return self.local_enabled

    def get_local_transcription_max_file_size_mb(self):
        return self.local_max_mb


class FakeWhisper:
    def __init__(self, alias="local_whisper_large", result=None, error=None):
        self.alias = alias
        self.config = {"type": "local_whisper"}
        self.model_name = "openai/whisper-large-v3-turbo"
        self.result = result if result is not None else {
            "text": "First line", "language": "en", "duration": 215.0, "provider": "local_whisper",
            "segments": [{"start": 1.0, "end": 2.0, "text": "First line"}]}
        self.error = error
        self.calls = []

    async def transcribe(self, path, **kw):
        self.calls.append(path)
        if self.error is not None:
            raise self.error
        return self.result


class FakeCloud:
    def __init__(self):
        self.config = {"type": "openai_compatible"}


class FakeLlm:
    def __init__(self, providers, voice_provider=None):
        self.providers = providers
        self.voice_provider = voice_provider


class FakeDownloads:
    def __init__(self):
        self.asked = []

    async def maybe_emit_required(self, model_name, **kw):
        self.asked.append((model_name, kw))
        return True


class _Service(MusicCommandsMixin):
    def __init__(self, home, providers=None, voice_provider=None, transcribe_audio=None, **settings):
        self.settings = _Settings(home, **settings)
        self.llm_manager = FakeLlm(providers or {}, voice_provider)
        self.model_download_service = FakeDownloads()
        if transcribe_audio is not None:
            self.transcribe_audio = transcribe_audio


def test_the_transcribe_command_is_slow_a_coroutine_and_in_the_allowlist():
    from dpc_client_core.local_api import ALLOWED_COMMANDS

    assert "transcribe_music_lyrics" in MUSIC_COMMANDS and "transcribe_music_lyrics" in ALLOWED_COMMANDS
    assert asyncio.iscoroutinefunction(MusicCommandsMixin.transcribe_music_lyrics)
    assert getattr(MusicCommandsMixin.transcribe_music_lyrics, "dpc_slow", False)


@pytest.mark.asyncio
async def test_the_command_passes_overwrite_manual_only_when_it_is_true(tmp_path, monkeypatch):
    seen = []

    class FakeLyrics:
        async def transcribe(self, track_id, **kw):
            seen.append(kw)
            if not kw["overwrite_manual"]:
                raise MusicLibraryError("overwrite_manual_required", "needs confirmation")
            return {"lyrics": None, "applied": True, "candidate": None}

    service = _Service(tmp_path)

    async def fake_lyrics():
        return FakeLyrics()
    monkeypatch.setattr(service, "_music_lyrics", fake_lyrics)
    refused = await service.transcribe_music_lyrics(track_id=TRACK_ID)
    assert refused == {"status": "error", "code": "overwrite_manual_required", "message": "needs confirmation"}
    assert (await service.transcribe_music_lyrics(track_id=TRACK_ID, overwrite_manual="yes"))["status"] == "error"
    assert (await service.transcribe_music_lyrics(track_id=TRACK_ID, overwrite_manual=True))["status"] == "ok"
    assert [kw["overwrite_manual"] for kw in seen] == [False, False, True]


@pytest.mark.asyncio
async def test_a_local_whisper_provider_is_called_with_the_file_and_keeps_its_segments(tmp_path, audio):
    whisper = FakeWhisper()
    service = _Service(tmp_path, providers={"cloud": FakeCloud(), "local_whisper_large": whisper})
    result = await service._music_transcribe_audio(audio)
    assert whisper.calls == [str(audio)]
    assert result["segments"] == [{"start": 1.0, "end": 2.0, "text": "First line"}]
    assert result["model"] == "openai/whisper-large-v3-turbo" and result["provider"] == "local_whisper"


@pytest.mark.asyncio
async def test_the_voice_provider_choice_is_respected(tmp_path, audio):
    calls = []

    async def transcribe_audio(**kw):
        calls.append(kw)
        return {"status": "success", "text": "a\nb", "provider": "openai", "language": "unknown", "duration": 0}

    whisper = FakeWhisper()
    service = _Service(tmp_path, providers={"local_whisper_large": whisper, "cloud": FakeCloud()},
                       voice_provider="cloud", transcribe_audio=transcribe_audio)
    result = await service._music_transcribe_audio(audio)
    assert whisper.calls == [] and calls == [{"file_path": str(audio), "provider_alias": "cloud"}]
    assert result["segments"] == [] and result["text"] == "a\nb" and result["model"] is None

    # "local:<alias>" names the local Whisper.
    service = _Service(tmp_path, providers={"local_whisper_large": whisper, "cloud": FakeCloud()},
                       voice_provider="local:local_whisper_large", transcribe_audio=transcribe_audio)
    await service._music_transcribe_audio(audio)
    assert whisper.calls == [str(audio)]

    # Local transcription off and no voice provider: the voice path's own choice.
    calls.clear()
    service = _Service(tmp_path, providers={"local_whisper_large": FakeWhisper()}, local_enabled=False,
                       transcribe_audio=transcribe_audio)
    await service._music_transcribe_audio(audio)
    assert calls == [{"file_path": str(audio), "provider_alias": None}]


@pytest.mark.asyncio
async def test_the_voice_path_error_and_the_size_limit_are_surfaced(tmp_path, audio):
    async def failing(**kw):
        return {"status": "error", "error": "Audio too large (12.00 MB > 10 MB limit)"}

    service = _Service(tmp_path, transcribe_audio=failing, local_enabled=False)
    with pytest.raises(MusicLibraryError) as e:
        await service._music_transcribe_audio(audio)
    assert e.value.code == "transcription_failed" and "10 MB limit" in e.value.message

    whisper = FakeWhisper()
    service = _Service(tmp_path, providers={"w": whisper}, local_max_mb=0)
    with pytest.raises(MusicLibraryError) as e:
        await service._music_transcribe_audio(audio)
    assert e.value.code == "too_large" and "max_file_size_mb" in e.value.message
    assert whisper.calls == []

    with pytest.raises(MusicLibraryError) as e:
        await _Service(tmp_path)._music_transcribe_audio(audio)
    assert e.value.code == "transcription_unavailable"


@pytest.mark.asyncio
async def test_a_whisper_failure_and_a_missing_model_are_surfaced(tmp_path, audio):
    service = _Service(tmp_path, providers={"w": FakeWhisper(error=RuntimeError("Local Whisper transcription failed: boom"))})
    with pytest.raises(MusicLibraryError) as e:
        await service._music_transcribe_audio(audio)
    assert e.value.code == "transcription_failed" and "boom" in e.value.message

    missing = ModelNotCachedError("openai/whisper-large-v3-turbo", "/cache", revision="main")
    wrapped = RuntimeError("load failed")
    wrapped.__cause__ = missing
    for error in (missing, wrapped):
        service = _Service(tmp_path, providers={"w": FakeWhisper(alias="w", error=error)})
        with pytest.raises(MusicLibraryError) as e:
            await service._music_transcribe_audio(audio)
        assert e.value.code == "model_not_downloaded"
        assert service.model_download_service.asked == [(
            "openai/whisper-large-v3-turbo",
            {"revision": "main", "purpose": "Lyrics transcription",
             "consequence_if_declined": "Lyrics will not be transcribed.", "provider_alias": "w"})]


@pytest.mark.asyncio
async def test_the_command_round_trips_over_a_real_library(tmp_path, monkeypatch):
    def init(original):
        def wrapped(self, home, **kw):
            kw.setdefault("tools", MediaTools(ffmpeg=None, ffprobe=None))
            kw.setdefault("save_delay_s", 0.01)
            original(self, home, **kw)
        return wrapped
    monkeypatch.setattr(commands.MusicLibrary, "__init__", init(commands.MusicLibrary.__init__))
    music = tmp_path / "Music"
    music.mkdir()
    (music / "Band - Song.mp3").write_bytes(b"audio bytes")
    whisper = FakeWhisper()
    service = _Service(tmp_path / "dpc", providers={"w": whisper})
    try:
        folder = (await service.add_music_folder(path=str(music)))["folder"]
        await service.rescan_music_folder(folder_id=folder["id"])
        await service._music_library_instance.wait_hashed()
        [entry] = (await service.list_music_library())["entries"]
        lyr = await service._music_lyrics()
        lyr._tools = MediaTools(ffmpeg=None, ffprobe=None)
        lyr.client = ForbiddenClient()

        got = await service.get_music_lyrics(track_id=entry["id"], refresh=True)
        assert got["status"] == "ok" and whisper.calls == []
        done = await service.transcribe_music_lyrics(track_id=entry["id"])
        assert done["status"] == "ok" and done["applied"] is True
        assert done["lyrics"]["source"] == "transcribe"
        assert done["lyrics"]["lines"] == [{"t_start": 1.0, "t_end": 2.0, "text": "First line"}]
        assert done["transcription"]["model"] == "openai/whisper-large-v3-turbo"
        assert len(whisper.calls) == 1 and whisper.calls[0].endswith("Band - Song.mp3")

        whisper.error = RuntimeError("CUDA out of memory")
        await service.save_music_lyrics(track_id=entry["id"], lrc_text="[00:03.00] mine")
        refused = await service.transcribe_music_lyrics(track_id=entry["id"])
        assert refused["status"] == "error" and refused["code"] == "overwrite_manual_required"
        failed = await service.transcribe_music_lyrics(track_id=entry["id"], overwrite_manual=True)
        assert failed["status"] == "error" and failed["code"] == "transcription_failed"
        assert "CUDA out of memory" in failed["message"]
        after = await service.get_music_lyrics(track_id=entry["id"])
        assert after["lyrics"]["source"] == "manual" and after["transcribe"]["state"] == "error"
    finally:
        await service.stop_music_library()
    assert sorted(p.name for p in music.iterdir()) == ["Band - Song.mp3"]


# -- the stored record names what transcribed it (Mike, 2026-11-20) ---------------------

@pytest.mark.asyncio
async def test_the_stored_record_keeps_the_provider_and_model(tmp_path, audio):
    svc = make(tmp_path, FakeLibrary(audio), FakeTranscriber())
    answer = await svc.transcribe(TRACK_ID)
    assert answer["lyrics"]["transcription"] == {"provider": "local_whisper",
                                                 "model": "openai/whisper-large-v3-turbo"}
    on_disk = json.loads(stored_file(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["transcription"] == answer["lyrics"]["transcription"]
    # Read back through a fresh instance: the record validates and round-trips.
    again = await make(tmp_path, FakeLibrary(audio), FakeTranscriber()).get(TRACK_ID)
    assert again["from_store"] is True and again["lyrics"] == on_disk


@pytest.mark.asyncio
async def test_a_provider_that_names_no_model_is_stored_as_null(tmp_path, audio):
    svc = make(tmp_path, FakeLibrary(audio), FakeTranscriber({"text": "one", "provider": "openai"}))
    answer = await svc.transcribe(TRACK_ID)
    assert answer["lyrics"]["transcription"] == {"provider": "openai", "model": None}


@pytest.mark.asyncio
async def test_a_record_written_before_the_key_still_loads(tmp_path, audio):
    old = {"schema": "mp.lyrics/1", "track_hash": "sha256:aa", "source": "transcribe", "language": "en",
           "lines": [{"t_start": 1.0, "t_end": 2.0, "text": "old"}], "updated_at": "2026-11-19T00:00:00.000Z"}
    path = stored_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(old), encoding="utf-8")
    got = await make(tmp_path, FakeLibrary(audio), FakeTranscriber()).get(TRACK_ID)
    assert got["from_store"] is True and got["lyrics"] == old


def test_the_schema_holds_the_transcription_key_to_its_shape():
    from dpc_client_core.music import schemas
    base = {"schema": "mp.lyrics/1", "track_hash": "h", "source": "transcribe",
            "lines": [{"t_start": 0.0, "text": "a"}]}
    schemas.validate_lyrics(base)
    schemas.validate_lyrics({**base, "transcription": {"provider": "local_whisper", "model": None}})
    for bad in ({**base, "source": "manual", "transcription": {"provider": "p", "model": "m"}},
                {**base, "transcription": {"provider": "p"}},
                {**base, "transcription": {"provider": "p", "model": "m", "device": "cuda"}},
                {**base, "transcription": {"provider": 3, "model": "m"}},
                {**base, "transcription": "local_whisper"}):
        with pytest.raises(schemas.SchemaError):
            schemas.validate_lyrics(bad)
