"""A group image whose VL pass refuses must still say so and still wake the agent.

Measured on two nodes, 2026-09-09: a node with no working vision provider raised
inside `_describe_image_for_agents`, and `send_group_image` swallowed it. Two things
followed, and both are tested here.

1. The caller was told `{"status": "success"}` and nothing else — the person who
   ticked "describe for agents (VL)" saw no error, no "no description", nothing.
   The image send genuinely did succeed, so the outcome of the description rides
   alongside success in `vl_description_status` with the reason in `warnings`.

2. `_handle_group_agent_mentions` was gated on the description, so an @mention in
   the caption reached nobody: the tagged agent did not merely go without a
   picture, it never learned the message existed. The description is an
   enrichment, not a precondition for routing a mention.
"""

import base64
import io

import pytest
from unittest.mock import AsyncMock, MagicMock

from dpc_client_core.service import CoreService


def _png_data_url() -> str:
    """A real 4x4 PNG — the method opens the file with PIL and reads its size."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _make_self(tmp_path):
    """Minimal fake CoreService `self` for send_group_image."""
    s = MagicMock()

    group = MagicMock()
    group.members = ["dpc-node-self", "dpc-node-peer"]
    s.group_manager.get_group = MagicMock(return_value=group)

    s.firewall.rules = {}

    monitor = MagicMock()
    monitor._get_conversation_dir = MagicMock(return_value=tmp_path)
    monitor.add_message = MagicMock()
    monitor.get_last_msg_index = MagicMock(return_value=1)
    s._get_or_create_conversation_monitor = MagicMock(return_value=monitor)

    s.p2p_manager.node_id = "dpc-node-self"
    s.p2p_manager.get_display_name = MagicMock(return_value="Mike")
    s.p2p_coordinator.get_connected_peers = MagicMock(return_value=[])
    s.file_transfer_manager.send_file = AsyncMock(return_value="tid-1")
    s.local_api.broadcast_event = AsyncMock()
    s._processed_message_ids = set()

    s._handle_group_agent_mentions = AsyncMock()
    return s, monitor


@pytest.mark.asyncio
async def test_a_refused_vl_description_is_reported_to_the_caller(tmp_path):
    s, _ = _make_self(tmp_path)
    s._describe_image_for_agents = AsyncMock(
        side_effect=RuntimeError("no vision-capable provider configured")
    )

    result = await CoreService.send_group_image(
        s, "group-test", _png_data_url(), "shot.png", "@ark what is this?",
        describe_for_agents=True,
    )

    # The image itself was sent, so this is not an error return...
    assert result["status"] == "success"
    # ...but the caller can tell "described" from "refused", and why.
    assert result["vl_description_status"] == "failed"
    assert any("no vision-capable provider configured" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_a_vl_provider_that_answers_with_nothing_counts_as_refused(tmp_path):
    s, _ = _make_self(tmp_path)
    s._describe_image_for_agents = AsyncMock(return_value="")

    result = await CoreService.send_group_image(
        s, "group-test", _png_data_url(), "shot.png", "caption",
        describe_for_agents=True,
    )

    assert result["status"] == "success"
    assert result["vl_description_status"] == "failed"
    assert result["warnings"]


@pytest.mark.asyncio
async def test_a_produced_vl_description_is_reported_and_routed_once(tmp_path):
    s, _ = _make_self(tmp_path)
    s._describe_image_for_agents = AsyncMock(return_value="a terminal with a red error")

    result = await CoreService.send_group_image(
        s, "group-test", _png_data_url(), "shot.png", "@ark what is this?",
        describe_for_agents=True,
    )

    assert result["status"] == "success"
    assert result["vl_description_status"] == "produced"
    assert "warnings" not in result

    # Exactly one fan-out — send_group_image does not go through send_group_message,
    # so making the routing unconditional must not double it.
    s._handle_group_agent_mentions.assert_awaited_once()
    _group_id, routed_text, _sender = s._handle_group_agent_mentions.await_args.args
    assert "@ark what is this?" in routed_text
    assert "a terminal with a red error" in routed_text


@pytest.mark.asyncio
async def test_a_mention_in_the_caption_reaches_agents_when_the_vl_pass_fails(tmp_path):
    s, _ = _make_self(tmp_path)
    s._describe_image_for_agents = AsyncMock(
        side_effect=RuntimeError("no vision-capable provider configured")
    )

    await CoreService.send_group_image(
        s, "group-test", _png_data_url(), "shot.png", "@ark what is this?",
        describe_for_agents=True,
    )

    s._handle_group_agent_mentions.assert_awaited_once()
    group_id, routed_text, sender = s._handle_group_agent_mentions.await_args.args
    assert group_id == "group-test"
    assert routed_text == "@ark what is this?"   # the bare caption, no [VL] block
    assert "[VL]" not in routed_text
    assert sender == "Mike"


@pytest.mark.asyncio
async def test_a_caption_mention_routes_even_when_no_description_was_asked_for(tmp_path):
    s, _ = _make_self(tmp_path)
    s._describe_image_for_agents = AsyncMock(return_value="unused")

    result = await CoreService.send_group_image(
        s, "group-test", _png_data_url(), "shot.png", "@ark look at this",
        describe_for_agents=False,
    )

    assert result["vl_description_status"] == "not_requested"
    assert "warnings" not in result
    s._describe_image_for_agents.assert_not_awaited()
    s._handle_group_agent_mentions.assert_awaited_once()
    assert s._handle_group_agent_mentions.await_args.args[1] == "@ark look at this"


@pytest.mark.asyncio
async def test_an_image_with_no_caption_and_no_description_routes_nothing(tmp_path):
    s, _ = _make_self(tmp_path)
    s._describe_image_for_agents = AsyncMock(side_effect=RuntimeError("no provider"))

    await CoreService.send_group_image(
        s, "group-test", _png_data_url(), "shot.png", "",
        describe_for_agents=True,
    )

    s._handle_group_agent_mentions.assert_not_awaited()
