"""A remote inference answer that nobody is waiting for any more (ADR-040 D4-0).

`RemoteInferenceResponseHandler` resolved the pending future when it found one
and returned in silence when it did not. Silence is the wrong answer here: the
host generated the tokens and paid for them, and the requester's three ceilings
(240 s, 180 s, 60 s) are all shorter than the host's own 900 s budget, so the
drop is not an edge case — it is what happens whenever the work outlives the
requester's patience.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from unittest.mock import MagicMock

from dpc_client_core.message_handlers.inference_handler import RemoteInferenceResponseHandler


def make_handler():
    service = MagicMock()
    service._pending_inference_requests = {}
    return RemoteInferenceResponseHandler(service), service


@pytest.mark.asyncio
async def test_an_answer_for_a_request_nobody_awaits_is_recorded(caplog):
    handler, svc = make_handler()

    with caplog.at_level(logging.WARNING):
        await handler.handle("peer-1", {
            "request_id": "req-gone", "status": "success", "response": "12 chars ok",
        })

    lines = [r.getMessage() for r in caplog.records if "req-gone" in r.getMessage()]
    assert len(lines) == 1
    assert "peer-1" in lines[0]


@pytest.mark.asyncio
async def test_an_answer_for_a_future_already_settled_is_recorded(caplog):
    handler, svc = make_handler()
    future = asyncio.get_running_loop().create_future()
    future.set_result({"response": "the first one"})
    svc._pending_inference_requests["req-late"] = future

    with caplog.at_level(logging.WARNING):
        await handler.handle("peer-1", {
            "request_id": "req-late", "status": "success", "response": "the second one",
        })

    lines = [r.getMessage() for r in caplog.records if "req-late" in r.getMessage()]
    assert len(lines) == 1


@pytest.mark.asyncio
async def test_an_answer_that_is_awaited_still_resolves_quietly(caplog):
    handler, svc = make_handler()
    future = asyncio.get_running_loop().create_future()
    svc._pending_inference_requests["req-live"] = future

    with caplog.at_level(logging.WARNING):
        await handler.handle("peer-1", {
            "request_id": "req-live", "status": "success", "response": "answer",
        })

    assert future.result()["response"] == "answer"
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
