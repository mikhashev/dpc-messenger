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


@pytest.fixture(autouse=True)
def _node_ledger_in_tmp(tmp_path, monkeypatch):
    """Every model call leaves a usage row (ADR-041 D3), and the writer's
    default directory is this machine's own ~/.dpc/ledger. A test that drives
    `chat()` or a served peer request would otherwise write into the
    developer's real ledger, so the default is pointed at the test's tmp_path.
    A test that wants the rows hands the adapter or coordinator a ledger of
    its own.
    """
    from dpc_client_core import node_ledger

    monkeypatch.setattr(node_ledger, "ledger_dir", lambda: tmp_path / "ledger")


class ConnectedUi:
    """A UI client at the other end of `local_api`, recording what it is sent.

    It answers nothing: the web-auth approval dialog it used to answer is
    gone, and no browse path asks a question any more. `answer` survives for
    callers that still pass it and is inert.
    """

    def __init__(self, answer: str = "approve"):
        self.answer = answer
        self.has_clients = True
        self.events: list = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def service_with_ui(answer: str = "approve", **extra):
    """`ctx.dpc_service` carrying a connected UI, plus whatever else the
    caller's tool reads off the service."""
    import types

    return types.SimpleNamespace(local_api=ConnectedUi(answer), **extra)
