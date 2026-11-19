"""A certificate one neighbour withheld is asked for again by the next message that needs it.

`pending_certificate_requests` keeps the message path from asking for the same
certificate once per message. It was a plain set, emptied only by an arriving
certificate, and the file path (ADR-047 phase 1, `ask_for_certificate`) added
the author of every unverified file offer to it too. A neighbour that withheld
one certificate, or a file offer naming an author nobody answered for, then
left that node in the set for good, and no message from it ever asked again:
its messages stayed `unverified` (found in review by a fresh subagent,
2026-11-20). Now the file path keeps its own asks, and an entry of the
message path's expires after the answer wait.
"""

from types import SimpleNamespace

import pytest

from dpc_client_core.managers.file_transfer_manager import FileTransferManager
from dpc_client_core.message_handlers.certificate_handler import (
    CERT_REQUEST_TIMEOUT_S,
    PendingCertificateRequests,
)
from dpc_client_core.message_handlers.session_handler import VoteNewSessionHandler

ME = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32
CAROL = "dpc-node-" + "c" * 32


def _service(pending):
    sent = []

    async def _send(node_id, message):
        sent.append((node_id, message["command"], message["payload"]))

    service = SimpleNamespace(
        p2p_manager=SimpleNamespace(node_id=ME, peers={BOB: object()}, send_message_to_peer=_send),
        pending_certificate_requests=pending,
    )
    service.sent = sent
    return service


def _asks(service):
    return [s for s in service.sent if s[1] == "CERT_REQUEST"]


@pytest.mark.asyncio
async def test_a_file_offers_ask_does_not_keep_a_message_from_asking():
    service = _service(PendingCertificateRequests())
    manager = FileTransferManager.__new__(FileTransferManager)
    manager.p2p_manager = service.p2p_manager
    manager.service = service
    manager.file_requests = SimpleNamespace(answer_timeout_s=10.0)
    manager._certificates_asked = {}
    manager._certificate_arrivals = {}

    await manager.ask_for_certificate(CAROL, BOB)  # a file offer; BOB never answers
    assert CAROL not in service.pending_certificate_requests

    await VoteNewSessionHandler(service)._ask_for_certificate(CAROL, BOB)  # then a message

    assert len(_asks(service)) == 2, "the message asked for itself"


@pytest.mark.asyncio
async def test_a_withheld_certificate_is_asked_for_again_once_the_ask_expires():
    clock = [100.0]
    service = _service(PendingCertificateRequests(clock=lambda: clock[0]))
    handler = VoteNewSessionHandler(service)

    await handler._ask_for_certificate(CAROL, BOB)
    await handler._ask_for_certificate(CAROL, BOB)
    assert len(_asks(service)) == 1, "still one ask per node while it is fresh"

    clock[0] += CERT_REQUEST_TIMEOUT_S  # BOB withheld it
    await handler._ask_for_certificate(CAROL, BOB)

    assert len(_asks(service)) == 2


def test_the_pending_asks_are_bounded_and_read_as_a_set():
    clock = [0.0]
    pending = PendingCertificateRequests(max_entries=3, clock=lambda: clock[0])
    for i in range(5):
        pending.add(f"dpc-node-{i:032x}")
        clock[0] += 0.1

    assert len(pending) == 3
    assert f"dpc-node-{0:032x}" not in pending and f"dpc-node-{4:032x}" in pending
    pending.discard(f"dpc-node-{4:032x}")
    assert f"dpc-node-{4:032x}" not in pending
    clock[0] += CERT_REQUEST_TIMEOUT_S
    assert len(pending) == 0 and list(pending) == []
