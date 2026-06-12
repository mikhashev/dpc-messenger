"""GROUP-HISTORY-UI-DOUBLE-LOAD: the disk fast-path of get_conversation_history
must expose a stable `message_id` (mirrored from the on-disk `id`), like the
monitor path already does. Without it the two frontend history loaders derive
different ids for the same message and the merge-dedup keeps both → every group
message renders twice."""

import json

import pytest

from dpc_client_core.service import CoreService


class FakeGroupManager:
    def __init__(self, conv_dir):
        self._conv_dir = conv_dir

    def _get_conversation_dir(self, conversation_id):
        return self._conv_dir


def make_service():
    svc = CoreService.__new__(CoreService)
    svc.conversation_monitors = {}
    svc.llm_manager = None
    return svc


def _write_history(conv_dir, messages):
    conv_dir.mkdir(parents=True, exist_ok=True)
    with open(conv_dir / "history.json", "w", encoding="utf-8") as f:
        json.dump({"messages": messages}, f)


@pytest.mark.asyncio
async def test_group_disk_path_exposes_message_id(tmp_path):
    conv_dir = tmp_path / "group-abc-newtestgroup"
    _write_history(conv_dir, [
        {"id": "uuid-1", "role": "user", "content": "Привет"},
        {"id": "hex16id", "role": "assistant", "content": "Принято"},
    ])
    svc = make_service()
    svc.group_manager = FakeGroupManager(conv_dir)

    result = await svc.get_conversation_history("group-abc")

    assert result["status"] == "success"
    assert [m["message_id"] for m in result["messages"]] == ["uuid-1", "hex16id"]


@pytest.mark.asyncio
async def test_group_disk_path_preserves_existing_message_id(tmp_path):
    conv_dir = tmp_path / "group-xyz-g"
    _write_history(conv_dir, [
        {"id": "disk-id", "message_id": "already-set", "role": "user", "content": "x"},
    ])
    svc = make_service()
    svc.group_manager = FakeGroupManager(conv_dir)

    result = await svc.get_conversation_history("group-xyz")

    assert result["messages"][0]["message_id"] == "already-set"
