"""ADR-052 S3a/S3b review, Ark #20-7b: one audio length per call.

The door's `card_ask` measures the length once (ffprobe, then soundfile) to make `expected_s`; the call
itself measures nothing again (the decoded samples give the running length, as before; the old
`_get_audio_duration` had no caller and was removed in S3 follow-up 4, Ark #40-3). With a length the door
already has, `card_ask` probes nothing.
"""

import pytest

from dpc_client_core.providers import whisper_provider as wp

from tests._card_lease_helpers import fast  # noqa: F401
from tests._whisper_lease_helpers import gpu_whisper

pytestmark = pytest.mark.usefixtures("fast")


@pytest.fixture
def probes(monkeypatch):
    seen = {"audio_seconds": 0}

    def counted_audio_seconds(path):
        seen["audio_seconds"] += 1
        return 30.0

    monkeypatch.setattr(wp, "audio_seconds", counted_audio_seconds)
    return seen


@pytest.mark.asyncio
async def test_one_probe_for_a_door_and_its_transcription(monkeypatch, probes):
    p = gpu_whisper(monkeypatch)
    ask = wp.card_ask(p, "dictation", "a.wav")
    await p.transcribe("a.wav", **ask)
    assert probes == {"audio_seconds": 1}


def test_a_length_the_door_has_is_not_probed_again(monkeypatch, probes):
    p = gpu_whisper(monkeypatch)
    ask = wp.card_ask(p, "dictation", "a.wav", audio_s=45.0)
    assert probes["audio_seconds"] == 0
    assert ask["expected_s"] == p.expected_seconds(45.0)

