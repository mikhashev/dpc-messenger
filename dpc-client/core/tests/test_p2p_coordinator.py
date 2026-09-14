"""Tests for P2PCoordinator extracted methods (Phase C Step 5).

Covers: handle_inference_request, handle_transcription_request,
handle_get_providers_request, handle_providers_response,
send_file, accept_file_transfer, cancel_file_transfer,
request_inference_from_peer.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.p2p_coordinator import P2PCoordinator


class ProvedConnection:
    """What `p2p_manager.peers[peer_id]` is on the tier that proves a key: a
    wrapper naming `direct_tls` (p2p_manager.py:62). A request arrives over a
    connection, and since ADR-041 D2 the host serves peer inference only where
    that connection proved the sender — so the fixture has to hold one."""

    connection_type = "direct_tls"

    def __init__(self, node_id: str):
        self.node_id = node_id


def make_coordinator(peers=None):
    """Create a P2PCoordinator with mocked service.

    `peers` is what `p2p_manager.peers` holds; the default is the two peers
    these tests call on, each on a proved connection. Pass `{}` for a node
    nobody is connected to.
    """
    service = MagicMock()
    service.p2p_manager = MagicMock()
    service.p2p_manager.peers = (
        {"peer-1": ProvedConnection("peer-1"), "peer-2": ProvedConnection("peer-2")}
        if peers is None else peers
    )
    service.p2p_manager.node_id = "dpc-node-test123"
    service.p2p_manager.send_message_to_peer = AsyncMock()
    service.hub_client = MagicMock()
    service.firewall = MagicMock()
    # The host designates what it serves; without this the coordinator refuses
    # (D4-0). A MagicMock attribute would be truthy and hide that rule.
    service.firewall.compute_serving_alias = "ollama_local"
    # Nothing declared, so a served call is the v1 gift. A MagicMock here would
    # stand in for an AppliedTariff and make every row refuse itself.
    service.firewall.tariff_for.return_value = None
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
async def test_a_peer_naming_a_model_is_still_served_by_the_designated_alias():
    """Replaces test_inference_request_finds_provider_by_model (ADR-040 D4-0).

    That test asserted the old contract — the model chose the alias, via
    find_provider_by_model — which is the mechanism that let a peer point this
    node at whichever provider it liked. The model no longer selects a
    provider; the host's serving alias does, and the model stays what the
    firewall gates on.
    """
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.query = AsyncMock(return_value={"response": "ok", "model": "llama3"})

    await coord.handle_inference_request("peer-1", "req-1", "hello", model="llama3")

    svc.llm_manager.find_provider_by_model.assert_not_called()
    assert svc.llm_manager.query.call_args[1]["provider_alias"] == "ollama_local"


@pytest.mark.asyncio
async def test_the_alias_a_peer_names_goes_to_the_gate_and_never_to_the_router():
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.query = AsyncMock(return_value={"response": "ok", "model": "m"})

    await coord.handle_inference_request("peer-1", "req-1", "hi", provider="deepseek_pro")

    # The gate is handed the alias — before D4-0 it was handed only the model.
    assert svc.firewall.can_request_inference.call_args[1]["provider"] == "deepseek_pro"
    # And whatever the peer named, the router uses the host's own alias.
    assert svc.llm_manager.query.call_args[1]["provider_alias"] == "ollama_local"


@pytest.mark.asyncio
async def test_a_node_that_designates_no_alias_serves_nobody():
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.firewall.compute_serving_alias = None
    svc.llm_manager.query = AsyncMock(return_value={"response": "ok"})

    await coord.handle_inference_request("peer-1", "req-1", "hello")

    svc.llm_manager.query.assert_not_called()
    msg = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert "serving alias" in msg["payload"]["error"]


def _designate(svc, alias: str, provider_type: str):
    """The host designates `alias`, and the registry knows its provider type.

    `make_coordinator` leaves `llm_manager.providers` a MagicMock, whose
    `config` is not a dict, so the door reads no type there. These tests need
    the type read, because the type is what is under test.
    """
    svc.firewall.compute_serving_alias = alias
    svc.llm_manager.providers = {alias: SimpleNamespace(config={"type": provider_type})}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["remote_peer", "dpc_agent"])
async def test_the_peer_door_refuses_to_serve_an_alias_that_is_itself_remote(tmp_path, caplog, provider_type):
    """ADR-041 D7 part 1 on the P2P door: what is shared is not shared onward.

    The gateway has refused a `remote_peer` / `dpc_agent` serving alias since
    `1ebe8441`; this door read `serving_local[0]` and served it without a look
    at its type, so B could designate A's model and C would reach A through
    B under B's name. Refused before the router, with no usage row: nothing
    ran.
    """
    coord, svc = make_coordinator()
    coord._ledger = NodeLedger(tmp_path / "ledger")
    svc.firewall.can_request_inference.return_value = True
    _designate(svc, "relay", provider_type)
    svc.llm_manager.query = AsyncMock(return_value={"response": "ok", "model": "m"})

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.p2p_coordinator"):
        await coord.handle_inference_request("peer-1", "req-1", "hello")

    svc.llm_manager.query.assert_not_called()
    msg = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    error = msg["payload"]["error"]
    assert "relay" in error and provider_type in error
    assert "shared onward" in error and "ADR-041 D7" in error
    assert list(coord._ledger.rows()) == []
    warned = [r for r in caplog.records if r.levelno == logging.WARNING and "shared onward" in r.getMessage()]
    assert len(warned) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["ollama", "llamacpp_server"])
async def test_a_local_alias_whose_type_is_known_is_still_served_and_written_down(tmp_path, provider_type):
    """The regression guard for the check above: a card type passes it."""
    coord, svc = make_coordinator()
    coord._ledger = NodeLedger(tmp_path / "ledger")
    svc.firewall.can_request_inference.return_value = True
    _designate(svc, "ollama_local", provider_type)
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "ok", "model": "gemma3:27b", "prompt_tokens": 12, "response_tokens": 3,
    })

    await coord.handle_inference_request("peer-1", "req-1", "hello")

    svc.llm_manager.query.assert_called_once()
    assert svc.llm_manager.query.call_args[1]["provider_alias"] == "ollama_local"
    msg = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert msg["payload"].get("error") is None and msg["payload"]["response"] == "ok"
    (row,) = list(coord._ledger.rows())
    assert row["alias"] == "ollama_local" and row["caller"] == "peer-1" and row["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_a_served_peer_request_is_written_down(caplog):
    """It belonged to no agent, so it appeared in no cost series at all."""
    import logging
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.llm_manager.query = AsyncMock(return_value={
        "response": "ok", "model": "gemma3:27b",
        "prompt_tokens": 1200, "response_tokens": 300,
    })

    with caplog.at_level(logging.INFO, logger="dpc_client_core.p2p_coordinator"):
        await coord.handle_inference_request("peer-1", "req-1", "hello")

    line = [r.getMessage() for r in caplog.records if "Peer inference served" in r.getMessage()]
    assert len(line) == 1
    assert "peer-1" in line[0] and "ollama_local" in line[0]
    # The `_est` suffix said whose numbers these are, and said it of every call
    # (Ark and Johnny, review of 2026-08-18). Since the engine's own counts win
    # wherever it reported any, the source is a field rather than a suffix — and
    # a door that reported none is still named, which is what the suffix was for.
    assert "prompt_tokens=1200" in line[0]
    assert "response_tokens=300" in line[0]
    assert "counts=ours" in line[0]


@pytest.mark.asyncio
async def test_two_peer_requests_do_not_generate_at_once():
    """One semaphore on the shared alias; the second waits rather than
    doubling the load on a card that holds one model."""
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True

    concurrent = 0
    peak = 0

    async def slow_query(*a, **kw):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1
        return {"response": "ok", "model": "m"}

    svc.llm_manager.query = AsyncMock(side_effect=slow_query)

    await asyncio.gather(
        coord.handle_inference_request("peer-1", "req-1", "a"),
        coord.handle_inference_request("peer-2", "req-2", "b"),
    )

    assert peak == 1


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
    coord, svc = make_coordinator(peers={})

    with pytest.raises(ConnectionError):
        await coord.request_inference_from_peer("peer-1", "hello")


@pytest.mark.asyncio
async def test_request_inference_timeout():
    coord, svc = make_coordinator()
    svc.p2p_manager.peers = {"peer-1": MagicMock()}

    with pytest.raises(TimeoutError):
        await coord.request_inference_from_peer("peer-1", "hello", timeout=0.1)


@pytest.mark.asyncio
async def test_only_the_designated_alias_is_offered_to_a_peer():
    """We advertised every provider we had, paid ones included (ADR-040 D4-0).

    Serving and advertising have to agree: a peer that is offered an alias will
    ask for it, and the gate now refuses anything but the serving alias — so an
    offer of `deepseek_pro` is an invitation to be denied, and worse, it tells a
    stranger which paid accounts this node holds.
    """
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.firewall.can_request_transcription.return_value = False
    svc.firewall.compute_serving_alias = "ollama_local"

    infos = {
        "ollama_local": {"alias": "ollama_local", "model": "gemma3:27b", "type": "ollama"},
        "deepseek_pro": {"alias": "deepseek_pro", "model": "deepseek-v4-pro", "type": "deepseek"},
    }
    svc.llm_manager.providers = {k: MagicMock() for k in infos}
    svc.build_p2p_provider_info = MagicMock(side_effect=lambda alias, provider: infos[alias])

    await coord.handle_get_providers_request("peer-1")

    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    offered = [p["alias"] for p in sent["payload"]["providers"]]
    assert offered == ["ollama_local"]


@pytest.mark.asyncio
async def test_a_node_with_no_designated_alias_offers_no_compute():
    coord, svc = make_coordinator()
    svc.firewall.can_request_inference.return_value = True
    svc.firewall.can_request_transcription.return_value = False
    svc.firewall.compute_serving_alias = None

    info = {"alias": "ollama_local", "model": "gemma3:27b", "type": "ollama"}
    svc.llm_manager.providers = {"ollama_local": MagicMock()}
    svc.build_p2p_provider_info = MagicMock(return_value=info)

    await coord.handle_get_providers_request("peer-1")

    sent = svc.p2p_manager.send_message_to_peer.call_args[0][1]
    assert sent["payload"]["providers"] == []
