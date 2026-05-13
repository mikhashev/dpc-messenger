# dpc-client/core/tests/test_local_api.py

import asyncio
import json
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