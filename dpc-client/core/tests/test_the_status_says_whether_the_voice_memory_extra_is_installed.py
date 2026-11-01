"""`get_status` carries `voice_memory_installed`, the one fact the UI toggles for
voice and memory need to say that the optional `voice-memory` extra is missing.

The check is `find_spec` over the packages the two features import, so asking it
never loads torch; the same function drives the startup warning.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from dpc_client_core.model_download_service import VOICE_MEMORY_IMPORTS, voice_memory_installed
from dpc_client_core.service import CoreService

_real_find_spec = importlib.util.find_spec


def _status_stub() -> SimpleNamespace:
    """The narrowest object `get_status` can run against."""
    return SimpleNamespace(
        hub_client=SimpleNamespace(websocket=None),
        p2p_manager=SimpleNamespace(
            peers={},
            node_id="dpc-node-0000000000000000",
            get_external_ips=lambda: [],
            peer_cache=SimpleNamespace(get_all_peers=lambda: []),
        ),
        peer_metadata={},
        llm_manager=SimpleNamespace(get_active_model_name=lambda: "m"),
        _get_local_ips=lambda: [],
        settings=SimpleNamespace(get_p2p_listen_port=lambda: 8888),
        _external_ip=None,
        _is_global_ipv6=lambda ip: False,
        connection_status=SimpleNamespace(
            get_operation_mode=lambda: SimpleNamespace(value="online"),
            get_status_message=lambda: "ok",
            get_available_features=lambda: [],
        ),
        connection_orchestrator=None,
    )


def _without(*names):
    def fake(name, *a, **k):
        if name in names:
            return None
        return _real_find_spec(name, *a, **k)
    return fake


@pytest.mark.asyncio
async def test_the_status_is_true_when_every_package_can_be_found(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object())
    status = await CoreService.get_status(_status_stub())
    assert status["voice_memory_installed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", VOICE_MEMORY_IMPORTS)
async def test_the_status_is_false_when_any_one_package_is_missing(monkeypatch, missing):
    monkeypatch.setattr(importlib.util, "find_spec", _without(missing))
    status = await CoreService.get_status(_status_stub())
    assert status["voice_memory_installed"] is False


def test_asking_does_not_import_torch(monkeypatch):
    import sys
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    voice_memory_installed()
    assert "torch" not in sys.modules
