"""ChatHistoryResponseHandler used to log `total_count` from the payload.

The new-member history push built in `service.py` never sets `total_count`,
so a push of 45 records used to log "Received 0 messages" — the field
defaulted to 0 and was trusted as if it were the real length. The fix logs
`len(messages)`, the value that is always true of what arrived, and appends
`total_count` only when the sender provided one and it disagrees.
"""

import logging

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.message_handlers.chat_history_handlers import (
    ChatHistoryResponseHandler,
    HistoryRequestRegistry,
)

GROUP = "group-970e5c7006a0"
ALICE = "dpc-node-" + "a" * 32
BOB = "dpc-node-" + "b" * 32


def _msg(mid, ts):
    return {
        "id": mid,
        "role": "user",
        "sender_node_id": ALICE,
        "sender_name": "alice",
        "content": f"content-{mid}",
        "timestamp": ts,
    }


class _Monitor:
    """Only what the handler touches: import_history."""

    def __init__(self):
        self.imported = None

    def import_history(self, messages):
        self.imported = messages


class _LocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, data):
        self.events.append((name, data))


class _Service:
    def __init__(self, monitor):
        self.conversation_monitors = {GROUP: monitor}
        self.local_api = _LocalApi()
        self.history_requests = HistoryRequestRegistry()
        # No knowledge_service needed: the monitor is already present.


@pytest.mark.asyncio
async def test_a_push_without_total_count_logs_its_real_length(caplog):
    """The new-member push shape: no `total_count` key at all."""
    monitor = _Monitor()
    service = _Service(monitor)
    service.history_requests.note(ALICE, GROUP, "r-1")

    messages = [_msg("1", "2026-09-23T00:00:00+00:00"),
                _msg("2", "2026-09-23T00:01:00+00:00"),
                _msg("3", "2026-09-23T00:02:00+00:00")]

    with caplog.at_level(logging.INFO):
        await ChatHistoryResponseHandler(service).handle(ALICE, {
            "conversation_id": GROUP,
            "request_id": "r-1",
            "messages": messages,
            # total_count deliberately absent, as service.py's push leaves it.
        })

    assert monitor.imported == messages
    received_lines = [r.message for r in caplog.records if "Received" in r.message]
    assert any("Received 3 messages" in line for line in received_lines), received_lines
    assert not any("Received 0 messages" in line for line in received_lines), received_lines


@pytest.mark.asyncio
async def test_a_response_with_a_matching_total_count_logs_plainly(caplog):
    monitor = _Monitor()
    service = _Service(monitor)
    service.history_requests.note(ALICE, GROUP, "r-2")

    messages = [_msg("1", "2026-09-23T00:00:00+00:00")]

    with caplog.at_level(logging.INFO):
        await ChatHistoryResponseHandler(service).handle(ALICE, {
            "conversation_id": GROUP,
            "request_id": "r-2",
            "messages": messages,
            "total_count": 1,
        })

    received_lines = [r.message for r in caplog.records if "Received" in r.message]
    assert any("Received 1 messages" in line for line in received_lines), received_lines
    assert not any("total_count" in line for line in received_lines), received_lines


@pytest.mark.asyncio
async def test_a_response_with_a_disagreeing_total_count_names_both(caplog):
    """The disagreement is worth knowing, so it is appended rather than hidden."""
    monitor = _Monitor()
    service = _Service(monitor)
    service.history_requests.note(ALICE, GROUP, "r-3")

    messages = [_msg("1", "2026-09-23T00:00:00+00:00")]

    with caplog.at_level(logging.INFO):
        await ChatHistoryResponseHandler(service).handle(ALICE, {
            "conversation_id": GROUP,
            "request_id": "r-3",
            "messages": messages,
            "total_count": 60,
        })

    received_lines = [r.message for r in caplog.records if "Received" in r.message]
    assert any("Received 1 messages" in line and "total_count 60" in line for line in received_lines), received_lines
