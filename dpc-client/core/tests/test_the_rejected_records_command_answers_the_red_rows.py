"""The `get_rejected_records` command answers the chat's red rows.

`local_api.ALLOWED_COMMANDS` gained the command beside
`get_conversation_history`, and `CoreService.get_rejected_records` reads the
rejected-records store for the asked conversation — the store the live and the
merge path keep. An agent tool never reads it; only the chat does.
"""

import asyncio
import json
from datetime import datetime, timezone

import pytest

from dpc_client_core import rejected_records

pytestmark = pytest.mark.asyncio

GROUP = "group-cmd"
OTHER = "group-other"


@pytest.fixture
def store(tmp_path):
    installed = rejected_records.RejectedRecordStore(
        tmp_path / "rejected-records" / rejected_records.FILE_NAME)
    rejected_records.set_store(installed)
    yield installed
    rejected_records.set_store(None)


def _refusal(conversation_id, record_id):
    return dict(
        conversation_id=conversation_id,
        reason="invalid_signature",
        arrived="live",
        transport_node_id="dpc-node-" + "b" * 32,
        record_id=record_id,
        claimed_sender_node_id="dpc-node-" + "a" * 32,
        text="the refused text",
    )


async def test_the_command_answers_the_stores_rows_for_the_asked_conversation(store):
    from dpc_client_core.service import CoreService

    when = datetime(2026, 11, 28, tzinfo=timezone.utc)
    clock = lambda: when  # noqa: E731 - one fixed moment for every row
    store.record(**_refusal(GROUP, "m-1"), now=clock)
    store.record(**_refusal(GROUP, "m-2"), now=clock)
    store.record(**_refusal(OTHER, "m-other"), now=clock)

    # The method reads the store and nothing else on the service.
    service = CoreService.__new__(CoreService)
    answer = await service.get_rejected_records(GROUP)

    assert answer["status"] == "success"
    assert answer["conversation_id"] == GROUP
    assert [row["record_id"] for row in answer["records"]] == ["m-1", "m-2"]

    empty = await service.get_rejected_records("group-not-held")
    assert empty["records"] == []
    assert empty["conversation_id"] == "group-not-held"


async def test_the_command_travels_the_dispatcher_to_the_core(store):
    """The allowlist is the door: without the entry the dispatcher refuses the
    command however right the method is (test_local_api.py's guard checks the
    reverse direction)."""
    from dpc_client_core.local_api import LocalApiServer

    class FakeCore:
        async def get_rejected_records(self, conversation_id=None):
            rows = rejected_records.get_store().for_conversation(conversation_id)
            return {"status": "success", "conversation_id": conversation_id,
                    "records": rows}

    _refused = _refusal(GROUP, "m-1")
    store.record(**_refused, now=lambda: datetime(2026, 11, 28, tzinfo=timezone.utc))

    server = LocalApiServer(FakeCore(), port=0)
    server._auth_token = "token"

    class _FakeWS:
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

    ws = _FakeWS([
        json.dumps({"command": "auth", "token": "token"}),
        json.dumps({"id": "red", "command": "get_rejected_records",
                    "payload": {"conversation_id": GROUP}}),
    ])
    await server._handler(ws)
    await asyncio.sleep(0)

    answer = [m for m in ws.sent if m.get("id") == "red"]
    assert answer, "the command was dispatched, not refused at the allowlist"
    assert answer[0]["status"] == "OK"
    assert answer[0]["payload"]["status"] == "success"
    assert [row["record_id"] for row in answer[0]["payload"]["records"]] == ["m-1"]
    assert answer[0]["payload"]["records"][0]["reason"] == "invalid_signature"
