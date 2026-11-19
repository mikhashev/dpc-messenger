"""The P2P voice send holds a recording to the same limits the paperclip does.

Zcode, review of 7b3c7196: `send_voice_message` read `file_transfer.max_size_mb`
by hand, so NaN (json.loads accepts it) switched the firewall cap off, and a
"nan" `voice_messages.max_duration_seconds` switched the length limit off. Both
now go through the paperclip's parsing (`file_transfer_cap_mb`, a finite
positive length or AGENT_CHAT_VOICE_DEFAULT_MAX_SECONDS).
"""

import base64
from types import SimpleNamespace

import pytest

from dpc_client_core import service as service_mod
from dpc_client_core.service import CoreService

PEER = "dpc-node-peer"


def _service(rules, settings):
    def get(section, key, default=None):
        return settings.get((section, key), default)

    return SimpleNamespace(
        p2p_coordinator=SimpleNamespace(get_connected_peers=lambda: {PEER}),
        settings=SimpleNamespace(get=get),
        firewall=SimpleNamespace(rules=rules),
    )


async def _send(svc, size_bytes=2 * 1024 * 1024, duration=1.0):
    audio = base64.b64encode(b"\x00" * size_bytes).decode()
    return await CoreService.send_voice_message(svc, PEER, audio, duration, "audio/webm")


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [float("nan"), float("inf"), True, "big", 0])
async def test_an_invalid_firewall_cap_is_the_default_not_no_cap(cap, monkeypatch):
    monkeypatch.setattr(service_mod, "AGENT_CHAT_FILE_DEFAULT_MAX_MB", 1)
    svc = _service({"file_transfer": {"max_size_mb": cap}}, {("voice_messages", "max_size_mb"): "50"})

    with pytest.raises(ValueError, match=r"File too large \(2\.00 MB > 1 MB limit, file_transfer\.max_size_mb\)"):
        await _send(svc)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["nan", "inf", "0", "long"])
async def test_an_invalid_length_limit_is_the_default_not_no_limit(limit):
    svc = _service({}, {("voice_messages", "max_duration_seconds"): limit})

    with pytest.raises(ValueError, match="too long"):
        await _send(svc, size_bytes=10, duration=service_mod.AGENT_CHAT_VOICE_DEFAULT_MAX_SECONDS + 1)
