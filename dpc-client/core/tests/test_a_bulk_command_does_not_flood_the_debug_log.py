"""One file transfer must not bury the DEBUG log under itself.

Mike, 2026-08-24, on a transfer of 131 211 chunks: two lines per chunk, one
from `CoreService.on_p2p_message_received` and one from `MessageRouter`,
about a quarter of a million lines for a single file — and nothing else in
the log survives that.

The traffic is not silenced, because «did the chunks arrive at all» is what
a trace is for. The first is always logged and then one in `BULK_TRACE_EVERY`
carries a running count, while the layer above stays quiet; the receiving
handler keeps its own progress line every tenth chunk.

What must not change is the work: throttling a log line may never throttle
the handler.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dpc_client_core.message_router import (  # noqa: E402
    BULK_COMMANDS,
    BULK_TRACE_EVERY,
    MessageRouter,
)
from dpc_client_core.message_handlers import MessageHandler  # noqa: E402


class _CountingHandler(MessageHandler):
    def __init__(self, command: str) -> None:
        super().__init__(service=None)
        self._command = command
        self.calls = 0

    @property
    def command_name(self) -> str:
        return self._command

    async def handle(self, sender_node_id, payload):
        self.calls += 1
        return None


def _router_with(command: str):
    router = MessageRouter()
    handler = _CountingHandler(command)
    router.register_handler(handler)
    return router, handler


def _routing_lines(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.name == "dpc_client_core.message_router" and r.getMessage().startswith("Routing")
    ]


@pytest.mark.asyncio
async def test_a_thousand_chunks_do_not_write_a_thousand_lines(caplog):
    router, handler = _router_with("FILE_CHUNK")
    total = BULK_TRACE_EVERY * 2

    with caplog.at_level(logging.DEBUG, logger="dpc_client_core.message_router"):
        for _ in range(total):
            await router.route_message("dpc-node-abc", {"command": "FILE_CHUNK", "payload": {}})

    lines = _routing_lines(caplog)
    assert handler.calls == total, "the handler must run for every message"
    assert len(lines) == 3, f"expected first + two heartbeats, got {len(lines)}: {lines}"


@pytest.mark.asyncio
async def test_the_first_bulk_message_is_always_traced(caplog):
    """Otherwise silence means both «flowing fine» and «nothing arrived»."""
    router, _ = _router_with("FILE_CHUNK")

    with caplog.at_level(logging.DEBUG, logger="dpc_client_core.message_router"):
        await router.route_message("dpc-node-abc", {"command": "FILE_CHUNK", "payload": {}})

    lines = _routing_lines(caplog)
    assert len(lines) == 1
    assert "FILE_CHUNK" in lines[0]


@pytest.mark.asyncio
async def test_the_heartbeat_carries_the_count(caplog):
    router, _ = _router_with("FILE_CHUNK")

    with caplog.at_level(logging.DEBUG, logger="dpc_client_core.message_router"):
        for _ in range(BULK_TRACE_EVERY):
            await router.route_message("dpc-node-abc", {"command": "FILE_CHUNK", "payload": {}})

    lines = _routing_lines(caplog)
    assert str(BULK_TRACE_EVERY) in lines[-1], (
        f"the heartbeat must say how many passed, got {lines[-1]!r}"
    )


@pytest.mark.asyncio
async def test_an_ordinary_command_is_still_traced_every_time(caplog):
    """The throttle is for named bulk commands, not for everything."""
    assert "SEND_TEXT" not in BULK_COMMANDS
    router, _ = _router_with("SEND_TEXT")

    with caplog.at_level(logging.DEBUG, logger="dpc_client_core.message_router"):
        for _ in range(5):
            await router.route_message("dpc-node-abc", {"command": "SEND_TEXT", "payload": {}})

    assert len(_routing_lines(caplog)) == 5


@pytest.mark.asyncio
async def test_the_service_layer_stays_quiet_for_a_bulk_command(caplog):
    """The second of the two floods, called on the real method.

    A fake `self` rather than a CoreService: the method only needs the
    router, and standing up the service would test the fixture instead.
    """
    from dpc_client_core.service import CoreService

    class _FakeService:
        def __init__(self) -> None:
            self.message_router, self.handler = _router_with("FILE_CHUNK")
            self.message_router.register_handler(_CountingHandler("SEND_TEXT"))

    fake = _FakeService()

    def received_lines():
        return [
            r.getMessage() for r in caplog.records
            if r.name == "dpc_client_core.service"
            and r.getMessage().startswith("Received message")
        ]

    with caplog.at_level(logging.DEBUG):
        await CoreService.on_p2p_message_received(
            fake, "dpc-node-abc", {"command": "FILE_CHUNK", "payload": {}}
        )
        assert received_lines() == [], "a bulk command must not be announced here"

        await CoreService.on_p2p_message_received(
            fake, "dpc-node-abc", {"command": "SEND_TEXT", "payload": {}}
        )
        assert len(received_lines()) == 1, "an ordinary command still is"
