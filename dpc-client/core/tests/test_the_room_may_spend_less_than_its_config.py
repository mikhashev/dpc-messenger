"""The room-scoped effort knob may always spend less, and never more.

Two halves of one rule, and each half was broken in the opposite direction:

* The header has offered five positions since it was built — off, low, medium,
  high, max — and the validator behind it knew four. `off` came back as
  `{"status": "error"}`, which today's envelope reports as OK, so the header
  drew the choice as taken and nothing was stored. The one setting that spends
  *less* was the only one a room could not choose.

* On the provider side the opposite: a level chosen in a room switched thinking
  back **on** for an alias whose own configuration says `think: false`. That
  flag is written by whoever owns the alias, usually because thinking ruins
  that model's answers — a visitor to the room cannot know that, and should not
  be able to overrule it from another window.
"""

import pytest

from dpc_client_core.service import CoreService
from dpc_client_core.managers.group_manager import GroupManager


class _Api:
    """Stands in for the WebSocket surface; only records what was announced."""

    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _Node:
    """A CoreService reduced to the two collaborators this command touches.

    The GroupManager is the real one on a temp directory, so the value has to
    survive an actual write to be read back.
    """

    def __init__(self, tmp_path):
        self.group_manager = GroupManager(tmp_path, node_id="dpc-node-self-abc")
        self.local_api = _Api()


@pytest.mark.asyncio
async def test_off_is_accepted_and_stored(tmp_path):
    node = _Node(tmp_path)
    gid = node.group_manager.create_group("Room", "", []).group_id

    result = await CoreService.set_group_reasoning_effort(node, gid, "off")

    assert result["status"] == "success"
    assert result["reasoning_effort"] == "off"
    assert node.group_manager.get_group(gid).reasoning_effort == "off"


@pytest.mark.asyncio
async def test_off_is_announced_like_any_other_choice(tmp_path):
    """The header redraws from the event, not from the reply."""
    node = _Node(tmp_path)
    gid = node.group_manager.create_group("Room", "", []).group_id

    await CoreService.set_group_reasoning_effort(node, gid, "off")

    assert ("group_updated", {"group_id": gid, "reasoning_effort": "off"}) in node.local_api.events


@pytest.mark.asyncio
async def test_a_word_that_is_not_a_position_is_still_refused(tmp_path):
    """Widening the vocabulary by one is not opening it: `none` and `minimal`
    are real words in another provider's table and must not arrive here as if
    the operator had chosen something."""
    node = _Node(tmp_path)
    gid = node.group_manager.create_group("Room", "", []).group_id

    result = await CoreService.set_group_reasoning_effort(node, gid, "minimal")

    assert result["status"] == "error"
    assert node.group_manager.get_group(gid).reasoning_effort is None


@pytest.mark.asyncio
async def test_every_position_the_header_offers_is_accepted(tmp_path):
    """The list under test is the one the control draws — if a sixth position
    is ever added to the header, this is where the two go out of step."""
    node = _Node(tmp_path)
    gid = node.group_manager.create_group("Room", "", []).group_id

    for position in ("off", "low", "medium", "high", "max"):
        result = await CoreService.set_group_reasoning_effort(node, gid, position)
        assert result["status"] == "success", position
        assert node.group_manager.get_group(gid).reasoning_effort == position
