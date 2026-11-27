"""ADR-052 slice S3a: the four doors to local Whisper pass `purpose`, their bound and `expected_s`.

L4 and the S3 row: the owner's dictation (`service.transcribe_audio`, which Telegram reaches too)
passes "dictation" and the dictation bound `[local_transcription] queue_wait_s`; music Transcribe
passes "music Transcribe" and no bound (it waits until cancelled); the agent tool passes "agent
tool" and the tool's timeout less the margin, capped by the turn hold's deadline; a peer's request
passes "peer <node>", no bound (the lease line gives it what is left of the guest's 120 s, L13) and
runs inside `serving_a_peer`. Each passes the provider's own estimate for the file. Since S3b the
provider also receives each door's `on_wait` (and the peer's `check`): see
`test_whisper_holds_the_card_lease_and_the_doors_wait_through_it.py`.

Fake providers; no model, no GPU.
"""

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.service import CoreService
from tests.test_p2p_coordinator import ProvedConnection

ESTIMATE_S = 7.5


class RecordingWhisper:
    """A local_whisper provider that records what each call asked for the card."""

    def __init__(self):
        self.alias = "w"
        self.config = {"type": "local_whisper"}
        self.model_name = "openai/whisper-large-v3-turbo"
        self.language = "auto"
        self.asks = []
        self.peer_seen = []
        self.estimated = []
        self.extras = []

    def expected_seconds(self, audio_s):
        self.estimated.append(audio_s)
        return ESTIMATE_S

    def is_model_loaded(self):
        return True

    async def transcribe(self, path, release_if_loaded_here=False, language=None, task=None, *,
                         purpose=None, wait_s=None, expected_s=None, on_wait=None, check=None,
                         with_detail=False):
        self.asks.append({"purpose": purpose, "wait_s": wait_s, "expected_s": expected_s})
        self.extras.append({"on_wait": on_wait, "check": check})
        self.peer_seen.append(base_mod.peer_being_served())
        return {"text": "hello", "language": "en", "duration": 3.0, "provider": "local_whisper",
                "segments": []}


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "voice.wav"
    path.write_bytes(b"RIFF....WAVEfmt fake")
    return path


@pytest.mark.asyncio
async def test_the_owners_dictation_passes_its_purpose_the_dictation_bound_and_the_estimate(audio):
    provider = RecordingWhisper()
    settings = MagicMock()
    settings.get.return_value = "10"
    settings.get_local_transcription_queue_wait_s.return_value = 600
    svc = SimpleNamespace(settings=settings, llm_manager=SimpleNamespace(providers={"w": provider}))

    result = await CoreService.transcribe_audio(svc, file_path=str(audio), provider_alias="w")

    assert result["text"] == "hello"
    assert provider.asks == [{"purpose": "dictation", "wait_s": 600.0, "expected_s": ESTIMATE_S}]


@pytest.mark.asyncio
async def test_music_transcribe_passes_its_purpose_and_waits_until_cancelled(tmp_path, audio):
    from dpc_client_core.music.commands import MusicCommandsMixin

    provider = RecordingWhisper()
    commands = MusicCommandsMixin.__new__(MusicCommandsMixin)
    commands.settings = SimpleNamespace(
        get=lambda section, key, fallback=None: fallback,
        get_local_transcription_max_file_size_mb=lambda: 25,
    )
    await commands._music_transcribe_with_whisper(provider, Path(audio))

    assert provider.asks == [{"purpose": "music Transcribe", "wait_s": None, "expected_s": ESTIMATE_S}]


def _tool_ctx(agent_root, provider):
    from dpc_client_core.dpc_agent.tools.registry import ToolContext

    return ToolContext(agent_root=agent_root,
                       dpc_service=SimpleNamespace(llm_manager=SimpleNamespace(providers={"w": provider})))


@pytest.mark.asyncio
async def test_the_agent_tool_without_a_turn_hold_waits_at_most_queue_wait_s(tmp_path):
    """Mike 2026-11-23: the cap is queue_wait_s (600 by default), below the tool's
    own 1800 s timeout less the margin (1790), which was the wait before."""
    from dpc_client_core.dpc_agent.tools.transcribe import _TOOL_TIMEOUT_S, transcribe_audio_file

    (tmp_path / "voice.wav").write_bytes(b"RIFF....WAVEfmt fake")
    provider = RecordingWhisper()
    await transcribe_audio_file(_tool_ctx(tmp_path, provider), "voice.wav")

    assert _TOOL_TIMEOUT_S == 1800
    # Without Settings the door reads the default queue_wait_s, 600 s, which is below
    # the timeout less the margin, max(10 s, estimate) = 10 s here (L4).
    assert provider.asks == [{"purpose": "agent tool", "wait_s": 600.0, "expected_s": ESTIMATE_S}]


@pytest.mark.asyncio
async def test_inside_a_turn_the_tool_waits_no_longer_than_the_turn_holds_deadline(tmp_path, monkeypatch):
    from dpc_client_core.dpc_agent.tools.transcribe import transcribe_audio_file

    (tmp_path / "voice.wav").write_bytes(b"RIFF....WAVEfmt fake")
    provider = RecordingWhisper()
    now = [1000.0]
    monkeypatch.setattr(base_mod, "_swap_clock", lambda: now[0])
    hold = SimpleNamespace(live=True, accepted_at=900.0, wait_s=300.0)
    with base_mod.carrying_card_hold(hold):
        await transcribe_audio_file(_tool_ctx(tmp_path, provider), "voice.wav")

    # The hold accepted at 900 s with 300 s to wait gives up at 1200 s: 200 s are left.
    assert provider.asks[0]["wait_s"] == 200.0


@pytest.mark.asyncio
async def test_a_peers_request_passes_its_purpose_no_bound_and_runs_as_served_to_that_peer():
    from dpc_client_core.p2p_coordinator import P2PCoordinator

    provider = RecordingWhisper()
    service = MagicMock()
    service.p2p_manager.peers = {"node-guest": ProvedConnection("node-guest")}
    service.p2p_manager.send_message_to_peer = AsyncMock()
    service.llm_manager.providers = {"w": provider}
    service.firewall.can_request_transcription.return_value = True
    coordinator = P2PCoordinator(service)
    audio = base64.b64encode(b"RIFF....WAVEfmt fake").decode()

    await coordinator.handle_transcription_request("node-guest", "req-1", audio, "audio/wav", provider="w")

    assert provider.asks == [{"purpose": "peer node-guest", "wait_s": None, "expected_s": ESTIMATE_S}]
    assert provider.peer_seen == ["node-guest"]
    assert base_mod.peer_being_served() is None
