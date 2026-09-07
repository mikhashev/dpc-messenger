"""A voice message's transcription reaches the sender's own record too.

Observed by Mike on the pair, 2026-09-07: the receiving node shows the text under
the player, the sending node shows nothing — and still shows nothing after a
restart.

One message becomes one transfer per recipient, and every join in the system
was keyed on `transfer_id`. The sender's own group record is written before the
fan-out (ADR-032 Part A local-first echo) and therefore has no transfer id at
all, so the text it transcribed itself could never be matched to the message it
came from. The file name is minted once and travels with the offer, so it is the
key that names the message rather than one of its transfers.

1:1 was never affected: there the sender's bubble is built from the transfer, so
it has an id.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.message_handlers.voice_transcription_handler import (
    VoiceTranscriptionHandler,
)

GROUP = "group-b88b65076b85"
PEER = "dpc-node-" + "b" * 32
VOICE = "voice_2026-09-07_14-08-22.wav"


def _monitor(attachment):
    return SimpleNamespace(
        message_history=[{"role": "user", "attachments": [attachment]}],
        message_buffer=[],
        full_conversation=[],
    )


def _handler(monitor):
    events = []

    async def _broadcast(event, payload):
        events.append((event, payload))

    service = SimpleNamespace(
        conversation_monitors={GROUP: monitor},
        _voice_transcriptions={},
        local_api=SimpleNamespace(broadcast_event=_broadcast),
        p2p_manager=SimpleNamespace(node_id="dpc-node-" + "a" * 32),
        # The relay to the rest of the group is a different concern; this file
        # is about the join between a transcription and the message it belongs to.
        _processed_message_ids={"vt:transfer-for-the-other-node"},
    )
    return VoiceTranscriptionHandler(service), events


def _payload(**over):
    payload = {
        "transfer_id": "transfer-for-the-other-node",
        "group_id": GROUP,
        "filename": VOICE,
        "transcription_text": "проверка связи",
        "provider": "whisper",
        "transcriber_node_id": PEER,
    }
    payload.update(over)
    return payload


@pytest.mark.asyncio
async def test_the_senders_own_record_gets_the_text_although_it_has_no_transfer_id():
    """The local-first echo writes filename and file_path, never a transfer id."""
    attachment = {"type": "voice", "filename": VOICE, "file_path": "C:/x/" + VOICE}
    monitor = _monitor(attachment)
    handler, _events = _handler(monitor)

    await handler.handle(PEER, _payload())

    assert attachment["transcription"]["text"] == "проверка связи"


@pytest.mark.asyncio
async def test_a_record_that_does_carry_the_transfer_id_still_matches():
    """The receiver's own attachment is built from the transfer and has one."""
    attachment = {"type": "voice", "filename": "something-else.wav",
                  "transfer_id": "transfer-for-the-other-node"}
    monitor = _monitor(attachment)
    handler, _events = _handler(monitor)

    await handler.handle(PEER, _payload())

    assert "transcription" in attachment


@pytest.mark.asyncio
async def test_another_voice_message_is_not_given_this_text():
    """Two recordings in one conversation must not collect each other's words."""
    attachment = {"type": "voice", "filename": "voice_2026-09-07_15-00-00.wav"}
    monitor = _monitor(attachment)
    handler, _events = _handler(monitor)

    await handler.handle(PEER, _payload())

    assert "transcription" not in attachment


@pytest.mark.asyncio
async def test_a_transcription_without_a_filename_falls_back_to_the_transfer_id():
    """A peer on the older build sends no file name; nothing may break for it."""
    attachment = {"type": "voice", "filename": VOICE,
                  "transfer_id": "transfer-for-the-other-node"}
    monitor = _monitor(attachment)
    handler, _events = _handler(monitor)

    await handler.handle(PEER, _payload(filename=None))

    assert "transcription" in attachment


# --- and after a restart, from the sender's own store -----------------------


@pytest.mark.asyncio
async def test_the_history_the_ui_reloads_carries_the_senders_own_text(monkeypatch):
    """The gap survived a restart: the merge on reload keyed on transfer_id too.

    A real monitor, because the double for this one grew an attribute per run —
    which is the signal that it had stopped resembling what production passes.
    """
    from dpc_client_core.service import CoreService
    from dpc_client_core.conversation_monitor import ConversationMonitor

    monkeypatch.setattr(
        ConversationMonitor, "persist_history", property(lambda self: False)
    )
    monitor = ConversationMonitor(
        conversation_id=GROUP,
        participants=[{"node_id": "dpc-node-" + "a" * 32, "name": "Mike", "context": "local"}],
        llm_manager=None,
    )
    attachment = {"type": "voice", "filename": VOICE, "file_path": "C:/x/" + VOICE}
    monitor.add_message("user", "Sent voice message", attachments=[attachment])

    svc = CoreService.__new__(CoreService)
    svc.conversation_monitors = {GROUP: monitor}
    svc._group_agent_context = {}
    svc._voice_transcriptions = {
        "transfer-for-a-recipient": {
            "text": "проверка связи", "filename": VOICE,
            "provider": "whisper", "transcriber_node_id": "us", "success": True,
        }
    }

    result = await CoreService.get_conversation_history(svc, GROUP)

    served = result["messages"][0]["attachments"][0]
    assert served["transcription"]["text"] == "проверка связи"
    assert monitor.message_history[0]["attachments"][0].get("transcription") is None, (
        "the stored record was mutated in place"
    )


@pytest.mark.asyncio
async def test_the_sender_records_the_file_name_with_its_own_transcription(tmp_path):
    """The join key has to be written where the sender's text is made.

    Everything above joins on `filename`; this is the one place that puts it
    there, and without it the sender's own record stays wordless however good
    the matching is.
    """
    import asyncio as _asyncio
    from dpc_client_core.service import CoreService

    audio = tmp_path / VOICE
    audio.write_bytes(b"RIFF....WAVE")

    svc = CoreService.__new__(CoreService)
    svc.settings = SimpleNamespace(
        get_voice_transcription_enabled=lambda: True,
        get_voice_transcription_sender_transcribes=lambda: True,
        get_voice_transcription_provider_priority=lambda: ["whisper_local"],
    )
    svc._voice_transcription_settings = {}
    svc._voice_transcriptions = {}
    svc._transcription_locks = {}
    svc.p2p_manager = SimpleNamespace(node_id="dpc-node-" + "a" * 32)
    svc._check_transcription_capability = lambda check_model_loaded=False: _done(True)
    svc.transcribe_audio = lambda audio_base64, mime_type, alias: _done(
        {"text": "проверка связи", "provider": alias, "confidence": 0.9, "language": "ru"}
    )
    svc._get_or_create_conversation_monitor = lambda cid: None
    svc._broadcast_voice_transcription = lambda *a, **kw: _done(None)
    svc.local_api = SimpleNamespace(broadcast_event=lambda *a, **kw: _done(None))

    async def _run():
        await CoreService._maybe_transcribe_voice_message(
            svc,
            transfer_id="t-1",
            node_id=PEER,
            file_path=audio,
            voice_metadata={"mime_type": "audio/wav"},
            is_sender=True,
        )

    await _run()

    stored = svc._voice_transcriptions["t-1"]
    assert stored["text"] == "проверка связи"
    assert stored["filename"] == VOICE, "the sender's text cannot be joined to its message"


def _done(value):
    fut = __import__("asyncio").get_event_loop().create_future()
    fut.set_result(value)
    return fut
