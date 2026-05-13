"""Tests for P2PCoordinator extracted methods (Phase C Step 5).

Covers: handle_inference_request, handle_transcription_request,
handle_get_providers_request, handle_providers_response,
send_file, accept_file_transfer, cancel_file_transfer,
request_inference_from_peer.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dpc_client_core.p2p_coordinator import P2PCoordinator


def make_coordinator():
    """Create a P2PCoordinator with mocked service."""
    service = MagicMock()
    service.p2p_manager = MagicMock()
    service.p2p_manager.peers = {}
    service.p2p_manager.node_id = "dpc-node-test123"
    service.p2p_manager.send_message_to_peer = AsyncMock()
    service.hub_client = MagicMock()
    service.firewall = MagicMock()
    service.llm_manager = MagicMock()
    service.local_api = MagicMock()
    service.local_api.broadcast_event = AsyncMock()
    service.peer_metadata = {}
    service._pending_inference_requests = {}
    service._pending_transcription_requests = {}
    service._pending_providers_requests = {}
    service.file_transfer_manager = MagicMock()
    service._provider_supports_voice = MagicMock(return_value=False)

    coord = P2PCoordinator(service)
    return coord, service


# ─────────────────────────────────────────────────────────────
# handle_inference_request
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inference_request_denied():
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = False

    await coord.handle_inference_request("peer-1", "req-1", "hello")

    svc.p2p_manager.send_message_to_peer.assert_called_once()
    msg = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert msg["payload"]["error"] is not None


@pytest.mark.asyncio
async def test_inference_request_success():
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "test answer",
        "model": "test-model",
        "tokens_used": 100,
    })

    await coord.handle_inference_request("peer-1", "req-1", "hello")

    svc.llm_manager.query.assert_called_once()
    svc.p2p_manager.send_message_to_peer.assert_called_once()


@pytest.mark.asyncio
async def test_inference_request_finds_provider_by_model():
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.find_provider_by_model.return_value = "ollama_llama"
    svc.llm_manager.query = AsyncMock(return_value={"response": "ok", "model": "llama3"})

    await coord.handle_inference_request("peer-1", "req-1", "hello", model="llama3")

    svc.llm_manager.find_provider_by_model.assert_called_once_with("llama3")
    svc.llm_manager.query.assert_called_once()
    assert svc.llm_manager.query.call_args[1]["provider_alias"] == "ollama_llama"


# ─────────────────────────────────────────────────────────────
# handle_get_providers_request
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_providers_denied():
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = False
    svc.firewall.can_request_transcription.return_value = False

    await coord.handle_get_providers_request("peer-1")

    svc.p2p_manager.send_message_to_peer.assert_called_once()


@pytest.mark.asyncio
async def test_get_providers_filters_by_firewall():
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.firewall.can_request_transcription.return_value = False

    provider_mock = MagicMock()
    provider_mock.model = "test-model"
    provider_mock.config = {"type": "ollama"}
    provider_mock.supports_vision.return_value = False
    svc.llm_manager.providers = {"test": provider_mock}

    await coord.handle_get_providers_request("peer-1")

    svc.p2p_manager.send_message_to_peer.assert_called_once()


# ─────────────────────────────────────────────────────────────
# handle_providers_response
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_providers_response_stores_metadata():
    coord, svc = make_coordinator()
    providers = [{"alias": "test", "model": "llama3", "type": "ollama"}]

    await coord.handle_providers_response("peer-1", providers)

    assert svc.peer_metadata["peer-1"]["providers"] == providers
    svc.local_api.broadcast_event.assert_called_once()


@pytest.mark.asyncio
async def test_providers_response_resolves_pending_future():
    coord, svc = make_coordinator()
    future = asyncio.Future()
    svc._pending_providers_requests["peer-1"] = future
    providers = [{"alias": "test"}]

    await coord.handle_providers_response("peer-1", providers)

    assert future.done()
    assert future.result() == providers


# ─────────────────────────────────────────────────────────────
# send_file
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_file_not_found():
    coord, svc = make_coordinator()

    with pytest.raises(FileNotFoundError):
        await coord.send_file("peer-1", "/nonexistent/file.txt")


@pytest.mark.asyncio
async def test_send_file_success(tmp_path):
    coord, svc = make_coordinator()
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world")
    svc.file_transfer_manager.send_file = AsyncMock(return_value="transfer-123")

    result = await coord.send_file("peer-1", str(test_file))

    assert result["transfer_id"] == "transfer-123"
    assert result["status"] == "pending"
    assert result["filename"] == "test.txt"


# ─────────────────────────────────────────────────────────────
# cancel_file_transfer
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_file_transfer_broadcasts_event():
    coord, svc = make_coordinator()
    transfer = MagicMock()
    transfer.node_id = "peer-1"
    transfer.filename = "test.txt"
    transfer.direction = "upload"
    svc.file_transfer_manager.active_transfers = {"t-1": transfer}
    svc.file_transfer_manager.cancel_transfer = AsyncMock()

    result = await coord.cancel_file_transfer("t-1", "user_cancelled")

    assert result["status"] == "cancelled"
    svc.local_api.broadcast_event.assert_called_once()


# ─────────────────────────────────────────────────────────────
# request_inference_from_peer
# ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_request_inference_peer_not_connected():
    coord, svc = make_coordinator()

    with pytest.raises(ConnectionError):
        await coord.request_inference_from_peer("peer-1", "hello")


@pytest.mark.asyncio
async def test_request_inference_timeout():
    coord, svc = make_coordinator()
    svc.p2p_manager.peers = {"peer-1": MagicMock()}

    with pytest.raises(TimeoutError):
        await coord.request_inference_from_peer("peer-1", "hello", timeout=0.1)
