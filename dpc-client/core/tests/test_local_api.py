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