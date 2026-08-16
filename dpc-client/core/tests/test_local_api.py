# dpc-client/core/tests/test_local_api.py

import asyncio
import json
import logging
from pathlib import Path
import pytest
import websockets

# Mark this test file to be run with asyncio
pytestmark = pytest.mark.asyncio

@pytest.fixture
def mock_dpc_env(tmp_path: Path, monkeypatch):
    """
    Creates a complete fake D-PC environment and uses monkeypatch
    to redirect all file access to this temporary directory.
    """
    # 1. Define the fake home directory
    fake_dpc_home = tmp_path / ".dpc"
    fake_dpc_home.mkdir()
    
    # 2. Create all necessary dummy files
    (fake_dpc_home / "node.key").write_text("fake key")
    (fake_dpc_home / "node.crt").write_text("fake cert")
    (fake_dpc_home / "node.id").write_text("dpc-node-8b066c7f3d7eb627") # The ID we will check for
    (fake_dpc_home / "providers.toml").write_text('default_provider = "dummy"')
    (fake_dpc_home / ".dpc_access").write_text("[hub]\npublic.json:name=allow")
    
    fake_context_file = fake_dpc_home / "personal.json"
    fake_context_content = {"profile": {"name": "Test User", "description" : ""}}
    fake_context_file.write_text(json.dumps(fake_context_content))

    # 3. Patch the DPC_HOME_DIR constant wherever it is imported
    monkeypatch.setattr("dpc_protocol.crypto.DPC_HOME_DIR", fake_dpc_home)
    monkeypatch.setattr("dpc_protocol.pcm_core.DPC_HOME_DIR", fake_dpc_home)
    monkeypatch.setattr("dpc_client_core.service.DPC_HOME_DIR", fake_dpc_home)

    # 4. Mock load_identity to return consistent node ID and file paths
    def mock_load_identity():
        return ("dpc-node-8b066c7f3d7eb627", fake_dpc_home / "node.key", fake_dpc_home / "node.crt")

    monkeypatch.setattr("dpc_protocol.crypto.load_identity", mock_load_identity)

    return fake_dpc_home

@pytest.mark.skip(reason="Complex integration test with identity mocking - requires refactoring")
async def test_local_api_status_command(mock_dpc_env):
    """
    A full integration test for the local API.
    """
    # --- THE CORE FIX: Import the service AFTER the patch is applied ---
    from dpc_client_core.service import CoreService

    service = CoreService()
    
    service_task = asyncio.create_task(service.start())
    await asyncio.sleep(0.2) 

    try:
        uri = "ws://127.0.0.1:9999"
        async with websockets.connect(uri) as websocket:
            await websocket.send(json.dumps({"id": "test-1", "command": "get_status"}))
            response_str = await websocket.recv()
            response = json.loads(response_str)

            assert response["id"] == "test-1"
            assert response["status"] == "OK"
            # Now this assertion should pass because the service was initialized
            # with the patched home directory.
            assert response["payload"]["node_id"] == "dpc-node-8b066c7f3d7eb627"

    finally:
        await service.stop()
        service_task.cancel()
        try:
            await service_task
        except asyncio.CancelledError:
            pass  # Expected when cancelling the task


# Commands intercepted in local_api before reaching CoreService — these are
# legitimately in ALLOWED_COMMANDS without a CoreService method (the dispatch
# loop in LocalApiServer._handle_message has explicit short-circuits for them).
# Keep in sync with local_api.py dispatch.
_LOCAL_API_HANDLED_COMMANDS = frozenset({
    "ui_log",  # frontend log relay → ui_logger, no CoreService dispatch
})


class _FakeWS:
    """Feeds frames to the handler and records what it answers."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.remote_address = ("127.0.0.1", 0)
        self.sent = []

    async def recv(self):
        return self._frames.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self, *args, **kwargs):
        pass


async def test_a_slow_command_does_not_hold_back_the_next_one():
    """A marked handler is dispatched off the read loop.

    Without the mark the loop awaits it inline and the second command cannot
    even start until the first returns — which is the whole defect: one
    extraction stalled every request behind it for half a minute.
    """
    from dpc_client_core.local_api import LocalApiServer, slow_command

    release = asyncio.Event()
    finished = []

    class FakeCore:
        @slow_command
        async def end_conversation_session(self, conversation_id=None):
            await release.wait()
            finished.append("slow")
            return {"status": "success"}

        async def get_conversation_history(self, conversation_id=None):
            finished.append("fast")
            return {"status": "success", "messages": []}

    server = LocalApiServer(FakeCore(), port=0)
    server._auth_token = "token"
    ws = _FakeWS([
        json.dumps({"command": "auth", "token": "token"}),
        json.dumps({"id": "slow", "command": "end_conversation_session",
                    "payload": {"conversation_id": "g"}}),
        json.dumps({"id": "fast", "command": "get_conversation_history",
                    "payload": {"conversation_id": "g"}}),
    ])

    handler = asyncio.create_task(server._handler(ws))
    await asyncio.sleep(0.05)
    assert finished == ["fast"], "the fast command must answer while the slow one is still running"

    release.set()
    await handler
    await asyncio.sleep(0.05)
    assert finished == ["fast", "slow"]

    answered = [m["id"] for m in ws.sent if m.get("id") in {"slow", "fast"}]
    assert answered == ["fast", "slow"], "each command must still get its own answer"


async def test_a_slow_handler_that_raises_answers_with_the_same_error_shape():
    from dpc_client_core.local_api import LocalApiServer, slow_command

    class FakeCore:
        @slow_command
        async def end_conversation_session(self, conversation_id=None):
            raise RuntimeError("extraction failed")

    server = LocalApiServer(FakeCore(), port=0)
    server._auth_token = "token"
    ws = _FakeWS([
        json.dumps({"command": "auth", "token": "token"}),
        json.dumps({"id": "slow", "command": "end_conversation_session",
                    "payload": {"conversation_id": "g"}}),
    ])

    await server._handler(ws)
    await asyncio.sleep(0.05)

    answer = [m for m in ws.sent if m.get("id") == "slow"]
    assert answer, "a failing slow command must still answer"
    assert answer[0]["status"] == "ERROR"
    assert answer[0]["payload"]["message"] == "extraction failed"


def test_allowed_commands_have_matching_coreservice_methods():
    """GRAFEMA-AUDIT regression guard: every entry in ALLOWED_COMMANDS must map to
    a method on CoreService (or be on the local_api short-circuit list). Dead
    allowlist entries are silent drift — a command accepted by the dispatcher
    that immediately fails with AttributeError.

    Direction-of-travel: ALLOWED_COMMANDS → CoreService. The reverse (every
    CoreService public method must be in the allowlist) is intentionally NOT
    checked — CoreService has internal helpers and methods that should stay
    out of the UI surface.
    """
    from dpc_client_core.local_api import ALLOWED_COMMANDS
    from dpc_client_core.service import CoreService

    missing = sorted(
        cmd for cmd in ALLOWED_COMMANDS
        if cmd not in _LOCAL_API_HANDLED_COMMANDS and not hasattr(CoreService, cmd)
    )
    assert not missing, (
        f"{len(missing)} ALLOWED_COMMANDS entries have no matching CoreService method "
        f"and are not on the local_api short-circuit list: {missing}"
    )

# --- H2 Step 0: an error under an OK envelope must at least be said out loud ---
# The envelope itself is unchanged on purpose (that is Step 1 and needs a window
# with a human at the screen). These three watch the line, and the third one is
# what stops the other two from passing vacuously: a handler that succeeded must
# produce no warning at all, or the log fills with noise and nobody reads it.

_UNDER_OK = "answered an error under an OK envelope"


async def test_an_inline_command_answering_error_is_logged_and_still_stamped_ok(caplog):
    from dpc_client_core.local_api import LocalApiServer

    class FakeCore:
        async def get_conversation_history(self, conversation_id=None):
            return {"status": "error", "message": "Agent not found: local_ai"}

    server = LocalApiServer(FakeCore(), port=0)
    server._auth_token = "token"
    ws = _FakeWS([
        json.dumps({"command": "auth", "token": "token"}),
        json.dumps({"id": "inline", "command": "get_conversation_history",
                    "payload": {"conversation_id": "local_ai"}}),
    ])

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.local_api"):
        await server._handler(ws)

    said = [r.getMessage() for r in caplog.records if _UNDER_OK in r.getMessage()]
    assert said, "the inline path is the one almost every command takes"
    assert "get_conversation_history" in said[0]
    assert "Agent not found: local_ai" in said[0]

    answer = [m for m in ws.sent if m.get("id") == "inline"]
    assert answer[0]["status"] == "OK", "Step 0 changes nothing the interface can see"
    assert answer[0]["payload"]["status"] == "error"


async def test_a_slow_command_answering_error_is_logged_too(caplog):
    from dpc_client_core.local_api import LocalApiServer, slow_command

    class FakeCore:
        @slow_command
        async def end_conversation_session(self, conversation_id=None):
            return {"status": "error", "message": "extraction already running"}

    server = LocalApiServer(FakeCore(), port=0)
    server._auth_token = "token"
    ws = _FakeWS([
        json.dumps({"command": "auth", "token": "token"}),
        json.dumps({"id": "slow", "command": "end_conversation_session",
                    "payload": {"conversation_id": "g"}}),
    ])

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.local_api"):
        await server._handler(ws)
        await asyncio.sleep(0.05)

    said = [r.getMessage() for r in caplog.records if _UNDER_OK in r.getMessage()]
    assert said, "the two envelope sites must not drift apart"
    assert "end_conversation_session" in said[0]

    answer = [m for m in ws.sent if m.get("id") == "slow"]
    assert answer[0]["status"] == "OK"


async def test_a_command_that_succeeded_says_nothing(caplog):
    from dpc_client_core.local_api import LocalApiServer

    class FakeCore:
        async def get_conversation_history(self, conversation_id=None):
            return {"status": "success", "messages": []}

    server = LocalApiServer(FakeCore(), port=0)
    server._auth_token = "token"
    ws = _FakeWS([
        json.dumps({"command": "auth", "token": "token"}),
        json.dumps({"id": "ok", "command": "get_conversation_history",
                    "payload": {"conversation_id": "g"}}),
    ])

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.local_api"):
        await server._handler(ws)

    assert not [r for r in caplog.records if _UNDER_OK in r.getMessage()]


async def test_a_handler_that_answers_for_itself_is_covered_too(caplog):
    """The third dispatch branch: @sends_own_response builds its own envelope.

    execute_ai_query is one of the two, and it is the Local AI Chat path, so
    leaving it out would mean the next two records are accepted on a path where
    an error under OK is still silent.
    """
    from dpc_client_core.local_api import LocalApiServer

    server = LocalApiServer(object(), port=0)
    ws = _FakeWS([])
    server._clients.add(ws)

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.local_api"):
        await server.send_response_to_all(
            "q", "execute_ai_query", "OK",
            {"status": "error", "message": "no provider for that alias"},
        )

    said = [r.getMessage() for r in caplog.records if _UNDER_OK in r.getMessage()]
    assert said, "a self-answering handler must not be the one place that stays quiet"
    assert "execute_ai_query" in said[0]
    assert ws.sent[0]["status"] == "OK"


async def test_an_honest_error_envelope_is_not_called_a_lie(caplog):
    """A command that answers ERROR is reporting correctly — say nothing.

    Without this the envelope argument is decoration: the check would fire on
    the one shape that is already honest, and the log would stop meaning
    'something failed silently'.
    """
    from dpc_client_core.local_api import LocalApiServer

    server = LocalApiServer(object(), port=0)
    ws = _FakeWS([])
    server._clients.add(ws)

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.local_api"):
        await server.send_response_to_all(
            "q", "execute_ai_query", "ERROR",
            {"status": "error", "message": "no provider for that alias"},
        )

    assert not [r for r in caplog.records if _UNDER_OK in r.getMessage()]
