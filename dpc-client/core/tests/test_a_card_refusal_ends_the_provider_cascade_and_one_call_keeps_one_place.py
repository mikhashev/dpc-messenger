"""ADR-052, Confirmation test 22 (slice S3c; section 3.1, queueing condition 2, Ark #53's falsifier):
a card refusal ends the walk over GPU aliases, and one call keeps one place.

Two local Whisper aliases in `provider_priority` and a busy card: one received voice message puts
exactly one waiter in `_LEASE_LINE`, no second model load is attempted, and a cloud alias later in
the list is still tried. The walk may also move on to a configured `device: cpu` alias, which holds
no lease.

How the refusal travels (read on `e8b5ac1a`): `transcribe_audio` catches every local error and
answers a dict (`status: "error"`), so the cascade's `except Exception: continue` never sees a card
refusal. The refusal is told apart from an ordinary failure by a field of that dict,
`card_refusal`, the way `model_not_cached` already is (finding D), never by its text.

At the dictation door the refusal follows the same chain as a failure: the OpenAI fallback, then a
peer, neither of which stands in this node's line (section 3.1 condition 2: "a cloud alias later in
the list (`openai`) does not touch the card and may still be tried").

The guest's own walk over peers is not a walk over this node's GPU aliases: a peer's card refusal
reaches the guest as a bare error text (`RuntimeError(str(e))`) and the next peer, whose card is
another one, is still asked.
"""

import asyncio

import pytest

from tests._card_lease_helpers import BOUND, fast, nothing_left, until, waiting  # noqa: F401
from tests._voice_cascade_helpers import busy_music, cloud_provider, received, speaking, voice_service  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper, lease_of

pytestmark = pytest.mark.usefixtures("fast")

REFUSAL_BOUND = 0.3


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "voice.wav"
    path.write_bytes(b"RIFF....WAVEfmt fake")
    return path


async def _watch_the_line(seen: list, stop: asyncio.Event) -> None:
    while not stop.is_set():
        seen.append(tuple(waiting()))
        await asyncio.sleep(0.002)


async def _received_with_the_line_watched(svc, audio):
    seen, stop = [], asyncio.Event()
    watcher = asyncio.create_task(_watch_the_line(seen, stop))
    try:
        await asyncio.wait_for(received(svc, audio), BOUND)
    finally:
        stop.set()
        await watcher
    return seen


@pytest.mark.asyncio
async def test_two_gpu_aliases_and_a_busy_card_one_waiter_no_second_load_and_the_cloud_is_tried(
        monkeypatch, audio, busy_music):
    w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
    w2 = speaking(gpu_whisper(monkeypatch, alias="w2"))
    providers = {"w1": w1, "w2": w2, "openai": cloud_provider()}
    svc = voice_service(providers, ["w1", "w2", "openai"], bound=REFUSAL_BOUND, fallback=False)

    seen = await _received_with_the_line_watched(svc, audio)

    assert ("whisper:w1",) in seen, "the first GPU alias waited in the line"
    assert max(len(line) for line in seen) == 1, "one call keeps one place"
    assert not any("whisper:w2" in line for line in seen), "the second GPU alias never queued"
    assert w1.loads == 0 and w2.loads == 0, "no model load was attempted"
    assert lease_of(w2) is None, "the second GPU alias was never asked for the card"
    svc._transcribe_with_openai.assert_awaited_once()
    stored = svc._voice_transcriptions["t-1"]
    assert stored["text"] == "the cloud heard it" and stored["provider"] == "openai"
    await nothing_left()


@pytest.mark.asyncio
async def test_after_a_card_refusal_a_configured_cpu_alias_is_still_tried(monkeypatch, audio, busy_music):
    w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
    w2 = speaking(gpu_whisper(monkeypatch, alias="w2"))
    on_cpu = speaking(gpu_whisper(monkeypatch, alias="small_cpu", device="cpu"))
    providers = {"w1": w1, "w2": w2, "small_cpu": on_cpu}
    svc = voice_service(providers, ["w1", "w2", "small_cpu"], bound=REFUSAL_BOUND, fallback=False)

    seen = await _received_with_the_line_watched(svc, audio)

    assert max(len(line) for line in seen) == 1
    assert w2.loads == 0 and lease_of(w2) is None
    assert on_cpu.loads == 1 and lease_of(on_cpu) is None, "the CPU alias ran, with no lease"
    assert svc._voice_transcriptions["t-1"]["text"] == "heard small_cpu"
    await nothing_left()


@pytest.mark.asyncio
async def test_an_alias_that_is_not_configured_reaches_the_card_too_and_is_skipped(monkeypatch, audio, busy_music):
    """`transcribe_audio` turns an alias it does not know into "the first local Whisper" (its
    auto-detect), so after a refusal such a name would queue on the same card again."""
    w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
    providers = {"w1": w1, "cloud": cloud_provider("cloud")}
    svc = voice_service(providers, ["w1", "whisper-medium", "cloud"], bound=REFUSAL_BOUND, fallback=False)

    seen = await _received_with_the_line_watched(svc, audio)

    assert max(len(line) for line in seen) == 1
    assert sum(1 for i, line in enumerate(seen) if line and (i == 0 or not seen[i - 1])) == 1, \
        "w1 entered the line once"
    svc._transcribe_with_openai.assert_awaited_once()
    assert svc._voice_transcriptions["t-1"]["text"] == "the cloud heard it"
    await nothing_left()


@pytest.mark.asyncio
async def test_the_dictation_door_names_a_card_refusal_and_not_an_ordinary_failure(monkeypatch, audio, busy_music):
    w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
    svc = voice_service({"w1": w1}, [], bound=REFUSAL_BOUND, fallback=False)

    refused = await asyncio.wait_for(svc.transcribe_audio(file_path=str(audio), provider_alias="w1"), BOUND)

    assert refused["status"] == "error"
    assert refused["card_refusal"] == "LeaseWaitedTooLong"
    assert w1.loads == 0
    await nothing_left()


@pytest.mark.asyncio
async def test_an_ordinary_local_failure_carries_no_card_refusal(monkeypatch, audio):
    broken = speaking(gpu_whisper(monkeypatch, alias="broken", load_fails=True))
    svc = voice_service({"broken": broken}, [], bound=REFUSAL_BOUND, fallback=False)

    failed = await asyncio.wait_for(svc.transcribe_audio(file_path=str(audio), provider_alias="broken"), BOUND)

    assert failed["status"] == "error" and broken.loads == 1
    assert "card_refusal" not in failed
    await nothing_left()


@pytest.mark.asyncio
async def test_at_the_dictation_door_a_card_refusal_still_falls_to_the_cloud(monkeypatch, audio, busy_music):
    """Section 3.1 condition 2: the cloud does not touch the card. The fallback the owner turned on
    runs after a refusal as after a failure."""
    w1 = speaking(gpu_whisper(monkeypatch, alias="w1"))
    svc = voice_service({"w1": w1, "openai": cloud_provider()}, [], bound=REFUSAL_BOUND, fallback=True)

    result = await asyncio.wait_for(svc.transcribe_audio(file_path=str(audio), provider_alias="w1"), BOUND)

    assert result["status"] == "success" and result["text"] == "the cloud heard it"
    assert "yue2" in (result["fallback_reason"] or ""), "the refusal, naming the holder, is the reason"
    assert w1.loads == 0
    await nothing_left()


@pytest.mark.asyncio
async def test_a_peers_card_refusal_is_followed_by_the_next_peer(tmp_path):
    """The guest's walk is over other nodes' cards. A host answers a card refusal as `error=str(e)`
    (`p2p_coordinator.py`), the guest's handler turns it into `RuntimeError(error)`, and the next
    peer is asked, as `test_a_peer_that_fails_is_followed_by_the_next_one` pins for any failure."""
    from dpc_client_core.service import CoreService
    from tests.test_a_peer_is_the_last_step_of_the_transcription_chain import (
        FRIEND,
        STRANGER,
        _firewall,
        _service,
        _voice,
    )

    calls = []

    async def _request(peer_id, audio_base64, mime_type, model=None, provider=None,
                       language="auto", task="transcribe", timeout=120.0):
        calls.append(peer_id)
        if peer_id == STRANGER:
            raise RuntimeError("this node's GPU is busy with another job right now, try again later")
        return {"text": "second peer answered", "language": "en", "duration_seconds": 1.0,
                "provider": provider}

    service = _service(_firewall(tmp_path, send_to_nodes=[STRANGER, FRIEND]), [FRIEND, STRANGER],
                       {FRIEND: [_voice("f")], STRANGER: [_voice("s")]})
    service._request_transcription_from_peer = _request
    voice = tmp_path / "voice.wav"
    voice.write_bytes(b"x")

    result = await CoreService._transcribe_with_peer(service, voice, "audio/wav")

    assert calls == [STRANGER, FRIEND]
    assert result["text"] == "second peer answered"
