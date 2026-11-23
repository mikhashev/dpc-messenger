"""ADR-052, Confirmation test 23 (slice S3c; section 3.1, queueing condition 3, Ark #53): a received
voice message waiting for the card behind music is dropped the moment the sender's transcript
arrives. The cascade passes the lease a `check` that raises when `transfer_id` is in
`_voice_transcriptions`; the wait ends, nothing loads, no second transcript is stored, and neither
the OpenAI fallback nor a peer is asked (the transcript is there: nothing is left to do).
"""

import asyncio

import pytest

from tests._card_lease_helpers import BOUND, fast, nothing_left, until, waiting  # noqa: F401
from tests._voice_cascade_helpers import busy_music, cloud_provider, received, speaking, voice_service  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "voice.wav"
    path.write_bytes(b"RIFF....WAVEfmt fake")
    return path


pytestmark = pytest.mark.usefixtures("fast")


@pytest.mark.asyncio
async def test_the_peers_transcript_ends_the_wait_and_nothing_loads(monkeypatch, audio, busy_music):
    w = speaking(gpu_whisper(monkeypatch, alias="w"))
    svc = voice_service({"w": w, "openai": cloud_provider()}, ["w"], bound=BOUND, fallback=True)

    message = asyncio.create_task(received(svc, audio))
    await until(lambda: waiting() == ["whisper:w"])

    the_senders = {"text": "the sender's own text", "success": True, "provider": "whisper"}
    svc._voice_transcriptions["t-1"] = the_senders
    await until(lambda: waiting() == [], bound=1.0)
    await asyncio.wait_for(message, BOUND)

    assert w.loads == 0, "nothing loads"
    assert svc._voice_transcriptions["t-1"] is the_senders, "no second transcript is stored"
    svc._transcribe_with_openai.assert_not_awaited()
    svc._transcribe_with_peer.assert_not_awaited()
    svc._broadcast_voice_transcription.assert_not_awaited()
    assert not [c for c in svc.local_api.broadcast_event.await_args_list
                if c.args and c.args[0] == "voice_transcription_complete"]
    await nothing_left()


@pytest.mark.asyncio
async def test_without_the_peers_transcript_the_wait_goes_on_and_the_message_is_transcribed(
        monkeypatch, audio, busy_music):
    """The check raises only for this transfer: another message's transcript does not end the wait."""
    w = speaking(gpu_whisper(monkeypatch, alias="w"))
    svc = voice_service({"w": w}, ["w"], bound=BOUND)

    message = asyncio.create_task(received(svc, audio))
    await until(lambda: waiting() == ["whisper:w"])
    svc._voice_transcriptions["another-transfer"] = {"text": "unrelated", "success": True}
    await asyncio.sleep(0.05)
    assert waiting() == ["whisper:w"]

    busy_music.set()
    await asyncio.wait_for(message, BOUND)
    assert w.loads == 1 and svc._voice_transcriptions["t-1"]["text"] == "heard w"
    await nothing_left()
