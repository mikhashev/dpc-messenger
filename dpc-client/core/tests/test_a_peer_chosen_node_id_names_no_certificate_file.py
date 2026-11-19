"""A node id a peer chose names no certificate file unless it is one `generate_node_id` writes.

A file description's author, a vote's or a message's signer, and the node a
CERT_REQUEST asks about are all the peer's to choose, and each becomes
`peers/<node_id>.crt`. `dpc-node-<victim>/../dpc-node-<attacker>` loaded the
attacker's own certificate and the description came back `verified`, on
Windows outright and elsewhere wherever a directory of the first name
exists; an upper-case copy of an id names the same file on Windows and macOS
volumes (found in review by a fresh subagent, 2026-11-20). The shared check
is `dpc_protocol.crypto.is_valid_node_id`, in `verify_signature` itself (see
dpc-protocol's test_verify_signature_refuses_a_node_id_that_is_not_a_key_hash)
and at every other place that builds the path; here the callers.
"""

from types import SimpleNamespace

import pytest

from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.message_signing import FILE_PREIMAGE_VERSION, file_content_hash
from dpc_client_core.message_handlers import certificate_handler
from dpc_client_core.message_handlers.certificate_handler import CertificateRequestHandler
from dpc_client_core.p2p_manager import P2PManager, load_persisted_peer_certificate
from dpc_client_core.signing import FILE_REJECTED, FILE_VERIFIED, file_description_verdict
from tests.test_a_file_description_is_signed_by_its_author_and_checked_on_arrival import (  # noqa: F401
    A,
    _certificate_pem,
    _fields,
    _key,
    _signed,
    identities,
)
from tests.test_a_group_file_crosses_a_full_mesh_once import GROUP

VICTIM = "dpc-node-" + "1" * 32


def _signed_by_a_as(identities, author):
    """A's real signature over a description that names `author`, a path to A's certificate."""
    fields = _fields(sender_node_id=author)
    return {"file_preimage_version": FILE_PREIMAGE_VERSION, **fields,
            "file_signature": CommitSigner(A, _key(A)).sign_commit(
                file_content_hash(conversation_id=GROUP, **fields))}


def test_a_traversing_author_id_is_rejected_not_verified(identities):
    identities.know(A)
    (identities.peers / VICTIM).mkdir()  # so `..` resolves on every OS, not only on Windows
    for forged in (f"{VICTIM}/../{A}", f"{VICTIM}\\..\\{A}"):
        desc = _signed_by_a_as(identities, forged)
        assert file_description_verdict(desc, GROUP) == FILE_REJECTED, forged


def test_an_upper_case_author_id_is_rejected(identities):
    identities.know(A)
    upper = "dpc-node-" + A[len("dpc-node-"):].upper()
    assert file_description_verdict(_signed_by_a_as(identities, upper), GROUP) == FILE_REJECTED


def test_a_real_author_id_still_verifies(identities):
    identities.know(A)
    assert file_description_verdict(_signed(identities), GROUP) == FILE_VERIFIED


@pytest.mark.asyncio
@pytest.mark.parametrize("asked", ["../node", "../peers/" + A, A.upper(), A + "/../" + A, "", None])
async def test_a_cert_request_for_a_path_reads_nothing(tmp_path, monkeypatch, asked):
    monkeypatch.setattr(certificate_handler, "DPC_HOME_DIR", tmp_path)
    (tmp_path / "node.crt").write_text("PEM-ME", encoding="utf-8")
    (tmp_path / "peers").mkdir()
    (tmp_path / "peers" / f"{A}.crt").write_text("PEM-A", encoding="utf-8")
    sent = []

    async def _send(node_id, message):
        sent.append(message)

    service = SimpleNamespace(p2p_manager=SimpleNamespace(
        node_id="dpc-node-" + "e" * 32, send_message_to_peer=_send))

    await CertificateRequestHandler(service).handle("dpc-node-" + "f" * 32, {"node_id": asked})

    assert sent == []


def test_the_certificate_store_takes_and_gives_nothing_under_a_malformed_id(tmp_path):
    peers = tmp_path / "peers"
    pem = _certificate_pem(A).decode()
    manager = P2PManager.__new__(P2PManager)

    assert manager._persist_peer_certificate(f"{VICTIM}/../{A}", pem, peers) is False
    assert not peers.exists() or not any(peers.iterdir())
    # A certificate whose key does not hash to the id is refused either way;
    # an A made of letters is not a key hash, so the real-key case is the protocol test's.
    assert load_persisted_peer_certificate(f"{VICTIM}/../{A}", peers) is None
    assert load_persisted_peer_certificate(A.upper(), peers) is None
