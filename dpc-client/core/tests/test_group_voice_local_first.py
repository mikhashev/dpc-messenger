"""
ADR-032 Task 003 — local-first voice publishing.

Red #2: sending a group voice message with no connected peers must still echo to the
sender's UI and write to group history immediately (decoupled from FILE_COMPLETE), and
must pre-seed the file_complete_handler dedup key so the later echo doesn't duplicate.
"""

import base64
import pytest
from unittest.mock import AsyncMock, MagicMock

from dpc_client_core.service import CoreService


def _make_self(tmp_path):
    """Minimal fake CoreService `self` for send_group_voice_message."""
    s = MagicMock()
    # settings.get(section, key, default) -> default
    s.settings.get = lambda *args: args[-1]

    group = MagicMock()
    group.members = ["dpc-node-self", "dpc-node-peer"]
    s.group_manager.get_group = MagicMock(return_value=group)

    monitor = MagicMock()
    files_dir = tmp_path  # _get_conversation_dir() / "files" is created under here
    monitor._get_conversation_dir = MagicMock(return_value=files_dir)
    monitor.add_message = MagicMock()
    s._get_or_create_conversation_monitor = MagicMock(return_value=monitor)

    s.p2p_manager.node_id = "dpc-node-self"
    s.p2p_coordinator.get_connected_peers = MagicMock(return_value=[])  # nobody online
    s.file_transfer_manager.send_file = AsyncMock(return_value="tid-1")
    s.local_api.broadcast_event = AsyncMock()
    s._processed_message_ids = set()
    return s, monitor


@pytest.mark.asyncio
async def test_group_voice_echoes_and_persists_with_no_peers(tmp_path):
    s, monitor = _make_self(tmp_path)
    audio_b64 = base64.b64encode(b"\x00" * 2048).decode()

    result = await CoreService.send_group_voice_message(
        s, "group-test", audio_b64, 1.5, "audio/webm"
    )

    # No peers were online -> nothing delivered, but the send still "succeeds".
    assert result["status"] == "success"
    assert result["transfer_ids"] == []
    s.file_transfer_manager.send_file.assert_not_called()

    # Red #2 fix: sender still got the echo + history on send.
    s.local_api.broadcast_event.assert_awaited_once()
    event_name, payload = s.local_api.broadcast_event.await_args.args
    assert event_name == "group_file_received"
    assert payload["group_id"] == "group-test"
    assert payload["attachments"][0]["type"] == "voice"

    monitor.add_message.assert_called_once()
    role = monitor.add_message.call_args.args[0]
    assert role == "user"

    # Dedup key pre-seeded so file_complete_handler's upload echo will skip.
    keys = [k for k in s._processed_message_ids if k.startswith("group_file_ui:group-test:")]
    assert len(keys) == 1


@pytest.mark.asyncio
async def test_group_voice_with_peer_echoes_once_and_pre_seeds_dedup(tmp_path):
    # With a connected peer the message is delivered AND echoed exactly once at send;
    # the dedup key is set so file_complete_handler:125 skips its later upload echo.
    s, monitor = _make_self(tmp_path)
    s.p2p_coordinator.get_connected_peers = MagicMock(return_value=["dpc-node-peer"])
    audio_b64 = base64.b64encode(b"\x00" * 1024).decode()

    result = await CoreService.send_group_voice_message(
        s, "group-test", audio_b64, 1.0, "audio/webm"
    )

    assert result["status"] == "success"
    assert result["transfer_ids"] == ["tid-1"]
    s.file_transfer_manager.send_file.assert_awaited_once()
    s.local_api.broadcast_event.assert_awaited_once()
    monitor.add_message.assert_called_once()
    keys = [k for k in s._processed_message_ids if k.startswith("group_file_ui:group-test:")]
    assert len(keys) == 1
