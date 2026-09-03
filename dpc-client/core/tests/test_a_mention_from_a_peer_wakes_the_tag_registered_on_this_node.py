"""A mention arriving from a peer wakes the tag registered on this node — and says so.

Live 2026-09-03 20:45:54: a GROUP_TEXT «@CC_mike привет» from a peer left only
«Saved 2 messages» in the log. The send path in `service.py` logs the mention set
at DEBUG and every woken tag at INFO; the peer path in `group_handler.py` called
`external_agents_to_wake` and broadcast in silence, so whether the event fired was
unobservable. It also passed neither `mention_all` nor `sender_name`, so `@all`
from a peer woke no external agent and a bridge's own tag was not excluded.

These tests run the handler's `_handle_agent_mentions` itself, on a fake service
shaped like the one in `test_a_mention_wakes_the_node_that_registered_the_name`.
The helper is not under test here — the wiring is.
"""
import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dpc_client_core.message_handlers.group_handler import GroupTextHandler

HANDLER_LOGGER = "dpc_client_core.message_handlers.GroupTextHandler"


def _handler(agents=("ext:CC_mike",)):
    svc = SimpleNamespace(
        group_manager=SimpleNamespace(
            get_group=lambda gid: SimpleNamespace(agents={"node-me": list(agents)})),
        p2p_manager=SimpleNamespace(node_id="node-me"),
        local_api=SimpleNamespace(broadcast_event=AsyncMock()),
        _get_default_agent_id=lambda: "agent_001",
        _get_agent_display_name=lambda aid: {"agent_001": "Ubu"}.get(aid, aid),
        get_cc_display_name=lambda: "CC",
    )
    return GroupTextHandler(svc), svc.local_api.broadcast_event


async def _mention(handler, text, sender_name="Ark", is_agent=False):
    payload = {"message_id": "m1", "text": text, "sender_name": sender_name}
    if is_agent:
        payload["is_agent"] = True
    await handler._handle_agent_mentions("g1", payload, text, sender_name, "node-peer")


@pytest.mark.asyncio
async def test_the_registered_tag_is_woken_by_its_own_name():
    handler, broadcast = _handler()
    await _mention(handler, "@CC_mike привет")
    assert broadcast.await_count == 1, f"broadcast {broadcast.await_count} times"
    event, payload = broadcast.await_args.args
    assert event == "cc_group_mention"
    assert payload["agent_tag"] == "cc_mike"
    assert payload["sender_node_id"] == "node-peer"


@pytest.mark.asyncio
async def test_at_all_from_a_human_peer_wakes_the_registered_tag():
    """The send path computes `mention_all`; the peer path never passed it."""
    handler, broadcast = _handler()
    await _mention(handler, "@all статус?")
    tags = [c.args[1]["agent_tag"] for c in broadcast.await_args_list]
    assert tags == ["cc_mike"], f"@all from a peer woke {tags}"


@pytest.mark.asyncio
async def test_a_bridge_is_not_woken_by_its_own_message():
    """The tag is the sender: a bridge answering `@CC_mike` must not wake itself."""
    handler, broadcast = _handler()
    await _mention(handler, "@CC_mike done", sender_name="CC_mike")
    assert broadcast.await_count == 0, "a bridge was woken by its own message"


@pytest.mark.asyncio
async def test_an_agent_message_wakes_nobody():
    handler, broadcast = _handler()
    await _mention(handler, "@CC_mike @all ping", sender_name="Ubu", is_agent=True)
    assert broadcast.await_count == 0


@pytest.mark.asyncio
async def test_the_peer_path_says_what_it_did(caplog):
    """The live defect: the wake is now observable in the log, at both levels."""
    caplog.set_level(logging.DEBUG, logger=HANDLER_LOGGER)
    handler, _ = _handler()
    await _mention(handler, "@CC_mike привет")
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG
             and "peer path" in r.getMessage() and "cc_mike" in r.getMessage()]
    assert debug, "the mention set was not logged at DEBUG"
    assert "g1" in debug[0].getMessage() and "Ark" in debug[0].getMessage()
    info = [r for r in caplog.records if r.levelno == logging.INFO
            and "cc_group_mention" in r.getMessage()]
    assert len(info) == 1, f"expected one INFO wake line, got {len(info)}"
    assert "@cc_mike" in info[0].getMessage() and "from peer Ark" in info[0].getMessage()
    assert "g1" in info[0].getMessage()
