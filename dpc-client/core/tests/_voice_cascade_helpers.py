"""A `CoreService` double for the ADR-052 S3c tests: the real received-voice cascade
(`_maybe_transcribe_voice_message`) and the real `transcribe_audio` over real Whisper providers on a
fake GPU (`tests._whisper_lease_helpers.gpu_whisper`). The cloud step and the peer step are recorded
stand-ins, so a test sees which of them ran.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest_asyncio

from dpc_client_core.service import CoreService

from tests._card_lease_helpers import BOUND, hold_open, music, until

ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32


def speaking(p):
    """`gpu_whisper`'s fake inference answers `{"text": path}`; `transcribe_audio` also reads the
    provider's name. The same fake, with the fields a real transcription has."""
    async def run(path, **kw):
        p.ran.append(path)
        return {"text": f"heard {p.alias}", "provider": "local_whisper", "language": "en",
                "duration": 3.0, "segments": []}

    p._transcribe_loaded = run
    return p


def cloud_provider(alias: str = "openai") -> SimpleNamespace:
    return SimpleNamespace(alias=alias, config={"type": "openai", "model": "whisper-1"})


def voice_service(providers: dict, priority: list, *, bound: float, fallback: bool = False):
    svc = CoreService.__new__(CoreService)
    svc.settings = SimpleNamespace(
        get=lambda section, key, default=None: "10",
        get_voice_transcription_enabled=lambda: True,
        get_voice_transcription_sender_transcribes=lambda: True,
        get_voice_transcription_provider_priority=lambda: list(priority),
        get_voice_transcription_recipient_delay_seconds=lambda: 0,
        get_voice_transcription_timeout_seconds=lambda: 240,
        get_local_transcription_enabled=lambda: True,
        get_local_transcription_max_file_size_mb=lambda: 25,
        get_local_transcription_queue_wait_s=lambda: bound,
        get_local_transcription_fallback_to_openai=lambda: fallback,
    )
    svc.llm_manager = SimpleNamespace(providers=providers)
    svc.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    svc._voice_transcription_settings = {}
    svc._voice_transcriptions = {}
    svc._transcription_locks = {}
    svc.p2p_manager = SimpleNamespace(node_id=ME, peers={})
    svc._check_transcription_capability = AsyncMock(return_value=True)
    svc._transcribe_with_openai = AsyncMock(return_value={
        "text": "the cloud heard it", "provider": "openai", "language": "en", "duration": 3.0})
    svc._transcribe_with_peer = AsyncMock(return_value=None)
    svc._peer_transcription_refusal = lambda: "no peer is connected"
    svc._get_or_create_conversation_monitor = lambda key: None
    svc._broadcast_voice_transcription = AsyncMock()
    return svc


async def received(svc, audio, transfer_id: str = "t-1", *, is_sender: bool = False) -> None:
    await CoreService._maybe_transcribe_voice_message(
        svc, transfer_id=transfer_id, node_id=PEER, file_path=audio,
        voice_metadata={"mime_type": "audio/wav"}, is_sender=is_sender,
    )


@pytest_asyncio.fixture
async def busy_music():
    """A running YuE2 job holds the card, its ETA unknown (L10), until the test sets the event."""
    over = asyncio.Event()
    yue2 = music()
    task = asyncio.create_task(hold_open(yue2.lease, "music job J", over))
    await until(lambda: yue2.lease.granted)
    yield over
    over.set()
    await asyncio.wait_for(task, BOUND)
