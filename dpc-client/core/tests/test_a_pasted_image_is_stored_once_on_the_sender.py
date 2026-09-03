"""
A pasted group image is stored once on the sender.

send_group_image writes the record and seeds a dedup key at send time; the later
FILE_COMPLETE echo (FileCompleteHandler, upload direction) must find that key and
skip its own add_message. Observed live 2026-09-03: the image path seeded
`group_image_ui:` while the handler checked `group_file_ui:`, so one paste became
two records (an empty-text one and a "Sent screenshot" one).
"""

import base64
import io
import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
from PIL import Image

from dpc_client_core.service import CoreService
from dpc_client_core.managers.file_transfer_manager import (
    FileTransfer, TransferStatus, group_file_ui_key,
)
from dpc_client_core.message_handlers.file_complete_handler import FileCompleteHandler


def _png_data_url() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _make_self(tmp_path):
    """Minimal fake CoreService `self`, same shape as test_group_voice_local_first."""
    s = MagicMock()
    s.settings.get = lambda *args: args[-1]

    group = MagicMock()
    group.members = ["dpc-node-self", "dpc-node-peer"]
    s.group_manager.get_group = MagicMock(return_value=group)

    monitor = MagicMock()
    monitor._get_conversation_dir = MagicMock(return_value=tmp_path)
    monitor.add_message = MagicMock()
    s._get_or_create_conversation_monitor = MagicMock(return_value=monitor)

    s.p2p_manager.node_id = "dpc-node-self"
    s.p2p_manager.get_display_name = MagicMock(return_value="Me")
    s.p2p_coordinator.get_connected_peers = MagicMock(return_value=["dpc-node-peer"])
    s.file_transfer_manager.send_file = AsyncMock(return_value="tid-1")
    s.file_transfer_manager.handle_file_complete = AsyncMock()
    s.file_transfer_manager.active_transfers = {}
    s.firewall.rules = {}
    s.local_api.broadcast_event = AsyncMock()
    s._processed_message_ids = set()
    return s, monitor


def _upload_transfer(result, group_id: str) -> FileTransfer:
    """The FileTransfer send_file would have registered for this image."""
    path = Path(result["file_path"])
    return FileTransfer(
        transfer_id="tid-1",
        filename=path.name,
        size_bytes=result["size_bytes"],
        hash="sha256:x",
        mime_type=result["mime_type"],
        chunk_size=65536,
        node_id="dpc-node-peer",
        direction="upload",
        status=TransferStatus.COMPLETED,
        chunks_received=set(),
        total_chunks=1,
        file_path=path,
        image_metadata={"dimensions": {}, "thumbnail_base64": "", "text": ""},
        group_id=group_id,
    )


@pytest.mark.asyncio
async def test_a_pasted_image_seeds_the_key_the_handler_checks(tmp_path):
    s, monitor = _make_self(tmp_path)

    result = await CoreService.send_group_image(s, "group-test", _png_data_url(), "shot.png")

    assert result["status"] == "success"
    monitor.add_message.assert_called_once()
    transfer = _upload_transfer(result, "group-test")
    expected = group_file_ui_key(transfer.group_id, transfer.filename)
    assert s._processed_message_ids == {expected}


@pytest.mark.asyncio
async def test_a_pasted_image_is_stored_once_after_the_file_complete_echo(tmp_path):
    s, monitor = _make_self(tmp_path)

    result = await CoreService.send_group_image(s, "group-test", _png_data_url(), "shot.png")
    assert result["status"] == "success"

    transfer = _upload_transfer(result, "group-test")
    s.file_transfer_manager.active_transfers["tid-1"] = transfer
    await FileCompleteHandler(s).handle("dpc-node-peer", {"transfer_id": "tid-1"})

    # One record for the paste: the send-time one. The echo must not add a second.
    assert monitor.add_message.call_count == 1
    group_events = [
        c.args[1] for c in s.local_api.broadcast_event.await_args_list
        if c.args[0] == "group_file_received"
    ]
    assert len(group_events) == 1


@pytest.mark.asyncio
async def test_a_renamed_image_file_still_seeds_the_on_disk_name(tmp_path):
    # A second paste with the same requested name lands on disk as shot_1.png;
    # the handler keys on that on-disk name, so the seed must too.
    s, monitor = _make_self(tmp_path)
    (tmp_path / "files" / "screenshots").mkdir(parents=True)
    (tmp_path / "files" / "screenshots" / "shot.png").write_bytes(b"taken")

    result = await CoreService.send_group_image(s, "group-test", _png_data_url(), "shot.png")

    assert result["status"] == "success"
    assert Path(result["file_path"]).name == "shot_1.png"
    transfer = _upload_transfer(result, "group-test")
    assert group_file_ui_key(transfer.group_id, transfer.filename) in s._processed_message_ids
