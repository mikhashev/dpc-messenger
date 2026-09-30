"""The event that announces a Telegram attachment must name the record it announces.

The backend stores the message (`telegram-voice-<id>`, Telegram's send time) and
then broadcasts an event; the UI used to mint an id and a time of its own, so
opening the chat drew the message twice, once with an empty transcription box.
The stored transcription is also `{text, provider}`, the shape the player reads.
"""
import asyncio
import datetime as dt
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.coordinators.telegram_coordinator import TelegramBridge

SENT_AT = dt.datetime(2026, 9, 30, 9, 36, 9)
SENT_AT_ISO = "2026-09-30T09:36:09+00:00"


def _bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    service = MagicMock()
    service.settings.get.return_value = "{}"
    service.local_api.broadcast_event = AsyncMock()
    service.transcribe_audio = AsyncMock(return_value={"text": "hello there", "provider": "whisper_local"})
    monitor = MagicMock()
    monitor.on_message = AsyncMock()
    telegram = MagicMock()
    telegram.is_allowed.return_value = True
    telegram.transcription_enabled = True
    telegram.send_message = AsyncMock()
    bridge = TelegramBridge(service, telegram)
    service.conversation_monitors = {"telegram-429727247": monitor}
    return bridge, service, monitor


def _update(**media):
    message = types.SimpleNamespace(
        message_id=77,
        chat_id=429727247,
        date=SENT_AT,
        caption=None,
        from_user=types.SimpleNamespace(full_name="Mike", username="mike"),
        **media,
    )
    return types.SimpleNamespace(message=message, update_id=1)


def _voice_run(tmp_path, monkeypatch):
    bridge, service, monitor = _bridge(tmp_path, monkeypatch)
    service.llm_manager.voice_provider = "whisper"

    async def fetch(bot, file_id, path, message):
        Path(path).write_bytes(b"ogg")
        return object()

    bridge._fetch_voice_with_retry = fetch
    update = _update(voice=types.SimpleNamespace(duration=3, file_size=3, file_id="f"))
    asyncio.run(bridge.handle_voice_message(update, None))
    stored = monitor.on_message.call_args.args[0]
    event = service.local_api.broadcast_event.call_args.args
    return stored, event


def test_the_voice_event_carries_the_id_and_time_of_the_stored_message(tmp_path, monkeypatch):
    stored, (name, payload) = _voice_run(tmp_path, monkeypatch)

    assert name == "telegram_voice_received"
    assert payload["message_id"] == stored.message_id == "telegram-voice-77"
    assert payload["timestamp"] == stored.timestamp == SENT_AT_ISO


def test_the_stored_transcription_has_the_shape_the_player_reads(tmp_path, monkeypatch):
    stored, (_, payload) = _voice_run(tmp_path, monkeypatch)

    transcription = stored.attachments[0]["transcription"]
    assert transcription == {"text": "hello there", "provider": "whisper_local"}
    assert payload["transcription"] == "hello there"
    assert payload["transcription_provider"] == "whisper_local"


def test_a_voice_message_without_a_transcription_stores_none(tmp_path, monkeypatch):
    bridge, service, monitor = _bridge(tmp_path, monkeypatch)
    service.llm_manager.voice_provider = None

    async def fetch(bot, file_id, path, message):
        Path(path).write_bytes(b"ogg")
        return object()

    bridge._fetch_voice_with_retry = fetch
    update = _update(voice=types.SimpleNamespace(duration=3, file_size=3, file_id="f"))
    asyncio.run(bridge.handle_voice_message(update, None))

    assert monitor.on_message.call_args.args[0].attachments[0]["transcription"] is None


def test_old_history_with_a_bare_string_transcription_still_reaches_the_model():
    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.message_history = [
        {"role": "user", "attachments": [{"type": "voice", "transcription": "old bare text"}]},
        {"role": "user", "attachments": [{"type": "voice", "transcription": {"text": "new shape", "provider": "p"}}]},
    ]

    out = monitor._extract_transcriptions_from_history()

    assert "old bare text" in out
    assert "new shape" in out
