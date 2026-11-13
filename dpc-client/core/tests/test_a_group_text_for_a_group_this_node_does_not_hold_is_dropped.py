"""GROUP_TEXT for a group this node does not hold is dropped before storage.

Team vote 2026-11-13 (A-GROUP-TEXT-IS-STORED-AND-SHOWN-UNDER-ANY-GROUP-ID-A-PEER-NAMES):
the one group handler that carries text used to go straight to dedup, storage
and mention routing for any `group_id` a connected peer named. Now an id with
no group behind it is dropped with one log line and a running count; nothing
is stored, no folder is made, no agent wakes. There is deliberately no member
check: a node that takes part through `agents` is not on `members`.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.group_handler import GroupTextHandler

ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
HANDLER_LOGGER = "dpc_client_core.message_handlers.GroupTextHandler"


class _Service:
    def __init__(self, dpc_home):
        self.group_manager = GroupManager(dpc_home, ME)
        self.p2p_manager = SimpleNamespace(node_id=ME, peers={}, send_message_to_peer=AsyncMock())
        self.local_api = SimpleNamespace(broadcast_event=AsyncMock())
        self.conversation_monitors = {}
        self._processed_message_ids = set()
        self._max_processed_ids = 1000
        self.conversations = dpc_home / "conversations"
        self.opened = []
        self.stored = []

    def _get_or_create_conversation_monitor(self, conversation_id):
        # What the real factory does first: the folder appears.
        self.opened.append(conversation_id)
        (self.conversations / conversation_id).mkdir(parents=True, exist_ok=True)
        service = self

        class _Monitor:
            message_ids = ()

            async def on_message(self, message):
                service.stored.append(message.message_id)

            def save_history(self):
                pass

            def get_message_history(self):
                return [{"id": m} for m in service.stored]

        return _Monitor()


@pytest.fixture
def node(tmp_path, monkeypatch):
    service = _Service(tmp_path)
    handler = GroupTextHandler(service)
    woken = []

    async def _spy(*args, **kwargs):
        woken.append(args)

    monkeypatch.setattr(handler, "_handle_agent_mentions", _spy)
    monkeypatch.setattr(GroupTextHandler, "dropped_not_held", 0, raising=False)
    monkeypatch.setattr(GroupTextHandler, "_dropped_by_pair", {})
    monkeypatch.setattr(GroupTextHandler, "_dropped_by_sender", {})
    return SimpleNamespace(service=service, handler=handler, woken=woken)


def _payload(group_id, text="hello", message_id="m-1"):
    return {"group_id": group_id, "text": text, "sender_name": "Peer",
            "message_id": message_id, "timestamp": "2026-11-13T10:00:00+00:00"}


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["group-0000deadbeef", PEER, "not-a-group"])
async def test_text_for_an_unheld_id_stores_nothing_makes_no_folder_and_wakes_nobody(node, group_id):
    await node.handler.handle(PEER, _payload(group_id, "@CC look at this"))

    assert node.service.opened == []
    assert node.service.stored == []
    assert not (node.service.conversations / group_id).exists()
    assert node.woken == []
    node.service.local_api.broadcast_event.assert_not_awaited()
    node.service.p2p_manager.send_message_to_peer.assert_not_awaited()
    assert f"{group_id}:m-1" not in node.service._processed_message_ids


def _drop_lines(caplog, level):
    return [r.getMessage() for r in caplog.records
            if "does not hold" in r.getMessage() and r.levelno == level]


@pytest.mark.asyncio
async def test_the_first_drop_for_a_sender_and_group_is_a_warning_the_next_are_debug(node, caplog):
    """Ark: the drop runs before any signature check, so a stranger must not be
    able to fill the log with WARNINGs. Every drop is still counted."""
    caplog.set_level(logging.DEBUG, logger=HANDLER_LOGGER)

    for i in range(3):
        await node.handler.handle(PEER, _payload("group-0000deadbeef", message_id=f"m-{i}"))

    (warning,) = _drop_lines(caplog, logging.WARNING)
    assert PEER[:20] in warning and "group-0000deadbeef" in warning
    assert "(1 for this group from this sender, 1 from this sender, 1 dropped so far)" in warning
    debug = _drop_lines(caplog, logging.DEBUG)
    assert len(debug) == 2 and "3 dropped so far" in debug[-1]
    assert GroupTextHandler.dropped_not_held == 3


@pytest.mark.asyncio
async def test_a_flood_for_one_group_warns_once_per_hundred_with_the_running_total(node, caplog):
    caplog.set_level(logging.WARNING, logger=HANDLER_LOGGER)
    every = GroupTextHandler.DROP_LOG_EVERY

    for i in range(every * 2 + 5):
        await node.handler.handle(PEER, _payload("group-0000deadbeef", message_id=f"m-{i}"))

    lines = _drop_lines(caplog, logging.WARNING)
    assert len(lines) == 3
    assert f"({every} for this group" in lines[1] and f"({every * 2} for this group" in lines[2]
    assert GroupTextHandler.dropped_not_held == every * 2 + 5


@pytest.mark.asyncio
async def test_another_sender_or_group_gets_its_own_first_warning(node, caplog):
    caplog.set_level(logging.WARNING, logger=HANDLER_LOGGER)
    other = "dpc-node-" + "c" * 32

    await node.handler.handle(PEER, _payload("group-0000deadbeef"))
    await node.handler.handle(PEER, _payload("group-1111deadbeef"))
    await node.handler.handle(other, _payload("group-0000deadbeef"))

    assert len(_drop_lines(caplog, logging.WARNING)) == 3


@pytest.mark.asyncio
async def test_a_sender_rotating_group_ids_is_held_by_its_own_total(node, caplog):
    """Per (sender, group) alone would let one sender warn once per invented id."""
    caplog.set_level(logging.WARNING, logger=HANDLER_LOGGER)
    every = GroupTextHandler.DROP_LOG_EVERY
    distinct_groups = GroupTextHandler.DROP_LOG_DISTINCT_GROUPS_PER_SENDER

    for i in range(every * 3):
        await node.handler.handle(PEER, _payload(f"group-{i:012x}", message_id=f"m-{i}"))

    lines = _drop_lines(caplog, logging.WARNING)
    assert len(lines) == distinct_groups + 3
    assert f"{every * 3} from this sender" in lines[-1]
    assert GroupTextHandler.dropped_not_held == every * 3


@pytest.mark.asyncio
async def test_the_group_id_is_cut_like_the_sender_in_the_line_and_the_count(node, caplog):
    """Johnny: the id is the stranger's choice, so it gets the sender's 20
    characters; ids that agree on those count as one group."""
    caplog.set_level(logging.DEBUG, logger=HANDLER_LOGGER)
    long_id = "group-" + "f" * 4000

    await node.handler.handle(PEER, _payload(long_id, message_id="m-1"))
    await node.handler.handle(PEER, _payload(long_id + "-other", message_id="m-2"))

    (warning,) = _drop_lines(caplog, logging.WARNING)
    assert long_id[:20] in warning and long_id[:21] not in warning
    assert len(warning) < 300
    (debug,) = _drop_lines(caplog, logging.DEBUG)
    assert "(2 for this group from this sender" in debug
    assert list(GroupTextHandler._dropped_by_pair) == [(PEER, long_id[:20])]


def test_the_drop_tables_stay_bounded(monkeypatch):
    monkeypatch.setattr(GroupTextHandler, "_DROP_TABLE_LIMIT", 8)
    monkeypatch.setattr(GroupTextHandler, "_dropped_by_pair", {})
    monkeypatch.setattr(GroupTextHandler, "_dropped_by_sender", {})
    monkeypatch.setattr(GroupTextHandler, "dropped_not_held", 0)

    for i in range(50):
        GroupTextHandler._note_drop_not_held(f"dpc-node-{i}", f"group-{i}")

    assert len(GroupTextHandler._dropped_by_pair) == 8
    assert len(GroupTextHandler._dropped_by_sender) == 8
    assert GroupTextHandler.dropped_not_held == 50


@pytest.mark.asyncio
async def test_text_for_a_held_group_is_still_stored_shown_and_routed(node):
    group = node.service.group_manager.create_group("Work", "", [PEER])

    await node.handler.handle(PEER, _payload(group.group_id, "@CC look at this"))

    assert node.service.stored == ["m-1"]
    shown = [c.args[0] for c in node.service.local_api.broadcast_event.await_args_list]
    assert shown == ["group_text_received"]
    assert len(node.woken) == 1
    assert GroupTextHandler.dropped_not_held == 0


@pytest.mark.asyncio
async def test_a_held_group_that_does_not_list_the_sender_still_stores(node):
    """The live constraint: DPC Project lists only this node, the Linux node
    takes part through `agents`. No member rule on GROUP_TEXT."""
    group = node.service.group_manager.create_group("DPC Project", "", [])
    assert group.members == [ME]

    await node.handler.handle(PEER, _payload(group.group_id))

    assert node.service.stored == ["m-1"]
