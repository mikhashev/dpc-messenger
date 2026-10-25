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


@pytest.fixture(autouse=True)
def _fresh_process_registry():
    """The process registry is module state (ADR-046). A test that begins the
    backend's stop would make every later spawn refuse, and open entries of one
    test's children would be read as the next one's. Each test starts with an
    empty in-memory registry; none touches `~/.dpc/process_registry.json`, which
    only `start_persistent()` (run_service) ever writes."""
    from dpc_client_core import process_registry

    process_registry.reset_registry()
    yield
    process_registry.reset_registry()


@pytest.fixture(autouse=True)
def _no_card_claims_cross_tests():
    """The card-holder registry is module state (ADR-044 point 4). A test whose
    fake launch returns without a child leaves its claim behind, and the next
    test's start would be refused by an alias that only existed in the last one."""
    from dpc_client_core.managers import llama_server_supervisor

    llama_server_supervisor._CARD_CLAIMS.clear()
    yield
    llama_server_supervisor._CARD_CLAIMS.clear()


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


@pytest.fixture(autouse=True)
def _camoufox_binary_counts_as_present(monkeypatch):
    """browser.py refuses to launch while the Camoufox browser binary is not
    on disk (2026-10-21). CI installs the package and never the binary, and a
    developer box usually has it, so every test that stubs `Camoufox` would
    pass here and refuse there. The answer is fixed to "present"; the tests of
    the refusal itself set it back."""
    from dpc_client_core import camoufox_binary

    monkeypatch.setattr(camoufox_binary, "missing_reason", lambda: None)
