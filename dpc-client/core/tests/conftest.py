"""Shared test constants and fixtures. Override TEST_DOMAIN via DPC_TEST_DOMAIN."""
import os

import pytest

TEST_DOMAIN = os.environ.get("DPC_TEST_DOMAIN", "wikipedia.org")
TEST_DOMAIN_WWW = f"www.{TEST_DOMAIN}"
TEST_DOMAIN_URL = f"https://{TEST_DOMAIN}"


@pytest.fixture(scope="session")
def _test_signing_key():
    """One RSA key for the whole session; generating it per test costs seconds."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signing_identity(_test_signing_key, monkeypatch):
    """A signing key belonging to the test rather than to the machine.

    ConversationMonitor signs with ~/.dpc/node.key when it exists. On a
    developer box it does and on a CI runner it does not, so tests about
    signatures passed here and exercised nothing there: with nothing signed,
    every record took the branch that has no check in it. Two of them failed on
    the runner for exactly that reason and were read as flaky.
    """
    from dpc_protocol.commit_integrity import CommitSigner
    from dpc_client_core.conversation_monitor import ConversationMonitor

    signer = CommitSigner("dpc-node-" + "a" * 32, _test_signing_key)
    monkeypatch.setattr(ConversationMonitor, "_get_signer", lambda self: signer)
    return signer
