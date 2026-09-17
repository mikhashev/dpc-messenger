"""When local transcription fails, a permitted peer is asked before giving up.

Observed by CC_linux on the Linux node, 2026-09-07 15:19:27-36: the local
Whisper failed to load, the chain tried OpenAI, found no such provider, and
ended — while a connected Windows node had announced a working Whisper twenty
minutes earlier and the whole node-to-node transcription path was built and
live. The remote path existed only as an explicit choice of alias
("remote:node:alias"), never as a step of the fallback.

Mike's call, 2026-09-07: make the peer a step of the chain when the firewall
allows it. The audio leaves the machine, so the permission is the whole guard,
and it is a permission this node did not have: letting a peer use our Whisper
(`can_request_transcription`) is the opposite direction and says nothing about
whether our own microphone may travel.

Three conditions decide, and the refusal has to name which one failed —
otherwise one silent chain is replaced by another.
"""

import base64
import json
from types import SimpleNamespace

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.service import CoreService

ME = "dpc-node-" + "a" * 32
FRIEND = "dpc-node-" + "b" * 32
STRANGER = "dpc-node-" + "c" * 32
GROUPMATE = "dpc-node-" + "d" * 32


def _firewall(tmp_path, **transcription):
    rules = {
        "node_groups": {"studio": [GROUPMATE]},
        "transcription": {
            # Serving is deliberately wide open here: it must not, on its own,
            # let this node's own audio leave.
            "enabled": True,
            "allow_nodes": [FRIEND, STRANGER, GROUPMATE],
            **transcription,
        },
    }
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    return ContextFirewall(path)


def _voice(alias="local_whisper_large"):
    return {
        "alias": alias,
        "model": "openai/whisper-large-v3",
        "type": "local_whisper",
        "supports_vision": False,
        "supports_voice": True,
        "context_window": None,
    }


def _mute(alias="ollama_text"):
    return {
        "alias": alias,
        "model": "llama3.1:8b",
        "type": "ollama",
        "supports_vision": False,
        "supports_voice": False,
        "context_window": 16384,
    }


def _service(firewall, connected, providers, asked=None):
    async def _request(peer_id, audio_base64, mime_type, model=None, provider=None,
                       language="auto", task="transcribe", timeout=120.0):
        if asked is not None:
            asked.append((peer_id, provider, audio_base64))
        return {
            "text": "the peer heard it",
            "language": "en",
            "duration_seconds": 3.0,
            "provider": provider,
        }

    service = SimpleNamespace(
        firewall=firewall,
        p2p_manager=SimpleNamespace(node_id=ME, peers={p: object() for p in connected}),
        peer_metadata={p: {"providers": v} for p, v in providers.items()},
        _request_transcription_from_peer=_request,
    )
    # The two siblings _transcribe_with_peer calls on itself, bound to this
    # double: a stand-in that omits them would exercise a chain the real object
    # does not have.
    service._peer_transcription_candidates = (
        lambda: CoreService._peer_transcription_candidates(service)
    )
    service._peer_transcription_refusal = (
        lambda: CoreService._peer_transcription_refusal(service)
    )
    return service


def _candidates(service):
    return CoreService._peer_transcription_candidates(service)


def _refusal(service):
    return CoreService._peer_transcription_refusal(service)


def test_a_permitted_connected_peer_that_advertises_voice_is_the_candidate(tmp_path):
    fw = _firewall(tmp_path, send_to_nodes=[FRIEND])
    service = _service(fw, [FRIEND], {FRIEND: [_mute(), _voice()]})

    assert _candidates(service) == [(FRIEND, "local_whisper_large")]


def test_serving_permission_does_not_let_this_nodes_audio_leave(tmp_path):
    """The wide-open allow_nodes above must not be read as consent to send."""
    fw = _firewall(tmp_path)  # no send_to_* at all
    service = _service(fw, [FRIEND], {FRIEND: [_voice()]})

    assert fw.can_request_transcription(FRIEND) is True
    assert fw.can_send_audio_to(FRIEND) is False
    assert _candidates(service) == []
    assert "none permitted to receive this" in _refusal(service)


def test_a_group_may_carry_the_permission(tmp_path):
    fw = _firewall(tmp_path, send_to_groups=["studio"])
    service = _service(fw, [GROUPMATE], {GROUPMATE: [_voice("whisper_here")]})

    assert _candidates(service) == [(GROUPMATE, "whisper_here")]


def test_a_peer_that_is_not_connected_is_not_a_candidate(tmp_path):
    fw = _firewall(tmp_path, send_to_nodes=[FRIEND])
    service = _service(fw, [], {FRIEND: [_voice()]})

    assert _candidates(service) == []
    assert _refusal(service) == "no peer is connected"


def test_a_permitted_peer_with_no_voice_provider_is_not_asked(tmp_path):
    fw = _firewall(tmp_path, send_to_nodes=[FRIEND])
    service = _service(fw, [FRIEND], {FRIEND: [_mute()]})

    assert _candidates(service) == []
    assert "none advertising a transcription provider" in _refusal(service)


def test_the_written_order_of_send_to_nodes_is_the_order_asked(tmp_path):
    """A list a person typed in order is a preference, not a set."""
    fw = _firewall(tmp_path, send_to_nodes=[STRANGER, FRIEND])
    service = _service(fw, [FRIEND, STRANGER],
                       {FRIEND: [_voice("f")], STRANGER: [_voice("s")]})

    assert _candidates(service) == [(STRANGER, "s"), (FRIEND, "f")]


@pytest.mark.asyncio
async def test_the_audio_reaches_the_peer_and_the_text_comes_back(tmp_path):
    asked = []
    fw = _firewall(tmp_path, send_to_nodes=[FRIEND])
    service = _service(fw, [FRIEND], {FRIEND: [_voice()]}, asked=asked)

    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")

    result = await CoreService._transcribe_with_peer(service, audio, "audio/wav")

    assert result["text"] == "the peer heard it"
    assert result["remote_node_id"] == FRIEND
    assert result["provider"] == "remote_local_whisper_large"
    assert len(asked) == 1
    peer_id, provider, audio_base64 = asked[0]
    assert peer_id == FRIEND
    assert provider == "local_whisper_large"
    assert base64.b64decode(audio_base64) == b"RIFF....WAVEfmt "


@pytest.mark.asyncio
async def test_no_candidate_returns_none_rather_than_raising(tmp_path):
    """The caller still owes the user the original local failure."""
    fw = _firewall(tmp_path)
    service = _service(fw, [FRIEND], {FRIEND: [_voice()]})
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"x")

    assert await CoreService._transcribe_with_peer(service, audio, "audio/wav") is None


@pytest.mark.asyncio
async def test_a_peer_that_fails_is_followed_by_the_next_one(tmp_path):
    calls = []

    async def _request(peer_id, audio_base64, mime_type, model=None, provider=None,
                       language="auto", task="transcribe", timeout=120.0):
        calls.append(peer_id)
        if peer_id == STRANGER:
            raise RuntimeError("peer refused")
        return {
            "text": "second peer answered",
            "language": "en",
            "duration_seconds": 1.0,
            "provider": provider,
        }

    fw = _firewall(tmp_path, send_to_nodes=[STRANGER, FRIEND])
    service = _service(fw, [FRIEND, STRANGER],
                       {FRIEND: [_voice("f")], STRANGER: [_voice("s")]})
    service._request_transcription_from_peer = _request

    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"x")

    result = await CoreService._transcribe_with_peer(service, audio, "audio/wav")

    assert calls == [STRANGER, FRIEND]
    assert result["text"] == "second peer answered"


def test_the_two_new_lists_are_validated_as_lists(tmp_path):
    valid, errors = ContextFirewall.validate_config(
        {"transcription": {"send_to_nodes": "dpc-node-x", "send_to_groups": {}}}
    )
    assert valid is False
    assert any("send_to_nodes" in e for e in errors)
    assert any("send_to_groups" in e for e in errors)
