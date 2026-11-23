"""ADR-052 slice S3b, the doors (section 3.1, section 4): each door's wait for the card reaches the
person who waits. The agent tool says it through `emit_progress`; the owner's dictation (and the
Telegram paths, which come through `service.transcribe_audio`) through the event
`transcription_waiting_for_card`; the player's Transcribe through its run record's `card_wait`. The
Whisper preload is a background call: it never waits, `LeaseSkipped` when the card is not free, and
the model stays lazy.

A real Whisper provider on a fake GPU behind a busy fake music lease.
"""

import asyncio
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from dpc_client_core.managers.card_lease import LeaseSkipped
from dpc_client_core.service import CoreService

from tests._card_lease_helpers import BOUND, fast, hold_open, music, nothing_left, until, waiting  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of

pytestmark = pytest.mark.usefixtures("fast")

QUEUED_LINE = "transcription queued behind yue2.cpp (music job J), time unknown"


@pytest_asyncio.fixture
async def busy_music():
    over = asyncio.Event()
    yue2 = music()
    task = asyncio.create_task(hold_open(yue2.lease, "music job J", over))
    await until(lambda: yue2.lease.granted)
    yield over
    over.set()
    await asyncio.wait_for(task, BOUND)


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "voice.wav"
    path.write_bytes(b"RIFF....WAVEfmt fake")
    return path


@pytest.mark.asyncio
async def test_the_agent_tool_says_its_wait_through_its_progress(tmp_path, monkeypatch, busy_music):
    from dpc_client_core.dpc_agent.tools.registry import ToolContext
    from dpc_client_core.dpc_agent.tools.transcribe import transcribe_audio_file

    (tmp_path / "voice.wav").write_bytes(b"RIFF....WAVEfmt fake")
    p = gpu_whisper(monkeypatch, alias="w")
    progress = []
    ctx = ToolContext(agent_root=tmp_path, emit_progress_fn=progress.append,
                      dpc_service=SimpleNamespace(llm_manager=SimpleNamespace(providers={"w": p})))
    tool = asyncio.create_task(transcribe_audio_file(ctx, "voice.wav"))
    await until(lambda: any(line.startswith("Waiting for the card") for line in progress))
    assert f"Waiting for the card: {QUEUED_LINE}" in progress
    busy_music.set()
    answer = await asyncio.wait_for(tool, BOUND)
    assert "voice.wav" in answer and p.loads == 1


@pytest.mark.asyncio
async def test_the_dictation_says_its_wait_as_an_event(audio, monkeypatch, busy_music):
    p = gpu_whisper(monkeypatch, alias="w")

    async def run(path, **kw):
        return {"text": "hello", "language": "en", "duration": 3.0, "provider": "local_whisper", "segments": []}

    monkeypatch.setattr(p, "_transcribe_loaded", run)
    settings = MagicMock()
    settings.get.return_value = "10"
    settings.get_local_transcription_queue_wait_s.return_value = BOUND
    local_api = SimpleNamespace(broadcast_event=AsyncMock())
    svc = SimpleNamespace(settings=settings, llm_manager=SimpleNamespace(providers={"w": p}), local_api=local_api)
    svc._tell_the_card_wait = types.MethodType(CoreService._tell_the_card_wait, svc)

    call = asyncio.create_task(CoreService.transcribe_audio(svc, file_path=str(audio), provider_alias="w"))
    await until(lambda: local_api.broadcast_event.await_count >= 1)
    event, payload = local_api.broadcast_event.await_args.args
    assert event == "transcription_waiting_for_card"
    assert payload["purpose"] == "dictation" and payload["text"] == QUEUED_LINE
    assert payload["wait"]["known"] is False and payload["wait"]["behind"] == ["yue2.cpp (music job J)"]
    busy_music.set()
    result = await asyncio.wait_for(call, BOUND)
    assert result["text"] == "hello" and p.loads == 1


@pytest.mark.asyncio
async def test_the_players_transcribe_shows_its_wait_in_the_run_record(tmp_path, monkeypatch, busy_music):
    from dpc_client_core.music.commands import MusicCommandsMixin
    from tests.test_transcribe_turns_a_tracks_vocals_into_lyrics_only_when_asked import (
        TRACK_ID,
        FakeLibrary,
        make,
    )

    folder = tmp_path / "Music"
    folder.mkdir()
    track = folder / "Band - Song.mp3"
    track.write_bytes(b"ID3" + b"\x00" * 64)
    p = gpu_whisper(monkeypatch, alias="w")

    async def run(path, **kw):
        return {"text": "la la", "language": "en", "duration": 3.0,
                "segments": [{"start": 0.0, "end": 3.0, "text": "la la"}]}

    monkeypatch.setattr(p, "_transcribe_loaded", run)
    commands = MusicCommandsMixin.__new__(MusicCommandsMixin)
    commands.settings = SimpleNamespace(get=lambda section, key, fallback=None: fallback,
                                        get_local_transcription_max_file_size_mb=lambda: 25)
    lyrics = make(tmp_path, FakeLibrary(track), lambda path: commands._music_transcribe_with_whisper(p, path))

    click = asyncio.create_task(lyrics.transcribe(TRACK_ID))
    await until(lambda: (lyrics.transcribe_run(TRACK_ID) or {}).get("card_wait"))
    run_record = lyrics.transcribe_run(TRACK_ID)
    assert run_record["state"] == "running"
    assert run_record["card_wait"] == {"text": QUEUED_LINE, "behind": ["yue2.cpp (music job J)"],
                                       "wait_s": None, "known": False}
    busy_music.set()
    answer = await asyncio.wait_for(click, BOUND)
    assert answer["applied"] is True
    assert "card_wait" not in lyrics.transcribe_run(TRACK_ID), "the finished run replaces the record"


@pytest.mark.asyncio
async def test_the_preload_never_waits_and_stays_lazy_when_the_card_is_held(monkeypatch, busy_music):
    p = gpu_whisper(monkeypatch)
    with pytest.raises(LeaseSkipped):
        await asyncio.wait_for(p.ensure_model_loaded(), BOUND)
    assert p.loads == 0 and not p.model_loaded and waiting() == []
    assert not lease_of(p).granted


@pytest.mark.asyncio
async def test_the_preload_on_a_free_card_leaves_a_warm_grant(monkeypatch):
    p = gpu_whisper(monkeypatch)
    await asyncio.wait_for(p.ensure_model_loaded(), BOUND)
    assert p.loads == 1 and p.model_loaded and lease_of(p).granted and lease_of(p).state == "running"
    assert (await asyncio.wait_for(lease_of(p).give_up_for("qwen"), BOUND)) == "let_go"
    await nothing_left()
