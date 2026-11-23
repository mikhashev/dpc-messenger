"""ADR-052 S3 follow-up 2, Johnny #24-3: the recorder's length is the fallback for the estimate.

`card_ask` reads the length with ffprobe, then soundfile; soundfile does not read WebM, the
recorder's default, so on a machine without ffprobe a dictation had no length, no `expected_s`, and a
waiting dictation had no estimate. L10 says the duration is "ffprobe's, else the recorder's declared
one". The probe wins; the recorder's `duration_seconds` is used only when the probe reads none. The
dictation door takes it from the UI's `transcribe_audio` command, the received-voice cascade from the
message's `voice_metadata`.

Also pinned here (L13 point 2): a peer whose own `expected_s` is unknown is refused at once on a
blocked card, also with no door bound, and since S3 follow-up 3 (Johnny #30-4, Mike #17) on a free
card too; the lease line never reads an unknown as "no work".
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dpc_client_core.managers import card_lease
from dpc_client_core.managers.card_lease import PEER_TRIP_S, LeaseRefusedForPeer
from dpc_client_core.managers.llama_server_supervisor import serving_a_peer
from dpc_client_core.providers import whisper_provider as wp
from dpc_client_core.service import CoreService

from tests._card_lease_helpers import BOUND, fast, hold_open, holding, until, waiting, whisper  # noqa: F401
from tests._voice_cascade_helpers import speaking, voice_service
from tests._whisper_lease_helpers import gpu_whisper

pytestmark = pytest.mark.usefixtures("fast")


class _Estimates:
    """A provider double that records the length each estimate was asked for."""

    def __init__(self):
        self.lengths = []

    def expected_seconds(self, audio_s):
        self.lengths.append(audio_s)
        return None if audio_s is None else audio_s / 10


class TestTheAsk:
    def test_with_no_readable_length_the_recorders_is_used(self, monkeypatch):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: None)
        provider = _Estimates()
        ask = wp.card_ask(provider, "dictation", "x.webm", bound_s=600, declared_s=12.0)
        assert provider.lengths == [12.0] and ask["expected_s"] == 1.2

    def test_the_probe_wins_over_the_recorder(self, monkeypatch):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: 45.0)
        provider = _Estimates()
        wp.card_ask(provider, "dictation", "x.webm", declared_s=12.0)
        assert provider.lengths == [45.0]

    @pytest.mark.parametrize("declared", [None, 0, -3, "nonsense", True, float("nan")])
    def test_an_unusable_recorder_length_stays_unknown(self, monkeypatch, declared):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: None)
        provider = _Estimates()
        ask = wp.card_ask(provider, "dictation", "x.webm", declared_s=declared)
        assert provider.lengths == [None] and ask["expected_s"] is None


class _RecordingWhisper:
    def __init__(self):
        self.alias = "w"
        self.config = {"type": "local_whisper"}
        self.model_name = "openai/whisper-large-v3-turbo"
        self.lengths = []

    def expected_seconds(self, audio_s):
        self.lengths.append(audio_s)
        return 3.0

    async def transcribe(self, path, **kw):
        return {"text": "hello", "language": "en", "duration": 3.0, "provider": "local_whisper",
                "segments": []}


@pytest.fixture
def webm(tmp_path):
    path = tmp_path / "dictation.webm"
    path.write_bytes(b"\x1aE\xdf\xa3 not a real webm")
    return path


def _dictation_service(provider):
    settings = MagicMock()
    settings.get.return_value = "10"
    settings.get_local_transcription_queue_wait_s.return_value = 600
    return SimpleNamespace(settings=settings, llm_manager=SimpleNamespace(providers={"w": provider}))


class TestTheDoors:
    @pytest.mark.asyncio
    async def test_the_dictation_door_passes_the_uis_length(self, monkeypatch, webm):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: None)
        provider = _RecordingWhisper()
        await CoreService.transcribe_audio(_dictation_service(provider), file_path=str(webm),
                                           provider_alias="w", duration_seconds=12.0)
        assert provider.lengths == [12.0]

    @pytest.mark.asyncio
    async def test_at_the_dictation_door_the_probe_still_wins(self, monkeypatch, webm):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: 45.0)
        provider = _RecordingWhisper()
        await CoreService.transcribe_audio(_dictation_service(provider), file_path=str(webm),
                                           provider_alias="w", duration_seconds=12.0)
        assert provider.lengths == [45.0]

    @pytest.mark.asyncio
    async def test_a_received_voice_message_passes_its_declared_length(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wp, "audio_seconds", lambda path: None)
        p = speaking(gpu_whisper(monkeypatch, "w1"))
        lengths = []
        real = p.expected_seconds
        monkeypatch.setattr(p, "expected_seconds", lambda audio_s, *a, **kw: lengths.append(audio_s) or real(audio_s))
        svc = voice_service({"w1": p}, ["w1"], bound=600)
        audio = tmp_path / "voice.webm"
        audio.write_bytes(b"\x1aE\xdf\xa3 not a real webm")
        await asyncio.wait_for(CoreService._maybe_transcribe_voice_message(
            svc, transfer_id="t-1", node_id="dpc-node-" + "b" * 32, file_path=audio,
            voice_metadata={"mime_type": "audio/webm", "duration_seconds": 12.0}, is_sender=True), BOUND)
        assert lengths == [12.0]
        assert p.ran, "the message was transcribed"


class TestAPeerWhoseOwnTimeIsUnknown:
    @pytest.mark.asyncio
    async def test_is_refused_at_once_on_a_blocked_card_even_with_no_door_bound(self):
        w, p = whisper(eta=5), whisper("small")
        release = asyncio.Event()
        dictation = asyncio.create_task(hold_open(w.lease, "dictation", release))
        await until(lambda: w.lease._uses)
        started = time.monotonic()
        with pytest.raises(LeaseRefusedForPeer):
            with serving_a_peer("node-guest"):
                await asyncio.wait_for(p.lease.acquire("peer node-guest", wait_s=None, expected_s=None), BOUND)
        assert time.monotonic() - started < 0.5 and waiting() == []
        release.set()
        await asyncio.wait_for(dictation, BOUND)

    @pytest.mark.asyncio
    async def test_is_refused_on_a_free_card_too(self):
        # S3 follow-up 3, Johnny #30-4: a trip that cannot be shown to fit 120 s is refused (Mike #17).
        p = whisper("small")
        with pytest.raises(LeaseRefusedForPeer):
            with serving_a_peer("node-guest"):
                await asyncio.wait_for(p.lease.acquire("peer node-guest", wait_s=None, expected_s=None), BOUND)
        assert holding() == [] and waiting() == []

    def test_its_bound_is_the_whole_trip_never_the_trip_less_nothing(self):
        p = whisper("small")
        with serving_a_peer("node-guest"):
            unknown = card_lease._LeaseWaiter(p.lease, "peer", None, False, None)
            known = card_lease._LeaseWaiter(p.lease, "peer", None, False, 19.0)
        assert unknown.expected_s is None and unknown.wait_s == PEER_TRIP_S
        assert known.wait_s == PEER_TRIP_S - 19.0
