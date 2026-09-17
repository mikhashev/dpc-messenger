"""Three more sites that reshape ConversationMonitor.message_history in place.

Commit 1a18b8a2 added a `rebuild_message_ids()` call after the reshape in
`import_history` and `clear_before` (covered in test_history_sync_chain.py),
and at two call sites in `coordinators/discord_coordinator.py`
(`_check_conversation_ttl`, `_trim_conversation`) — covered here. A review
found a fifth site the commit missed: `CoreService._delete_group_briefs`
mutates `message_history` in place and never touches `message_ids` at all;
the fix for that lives in `service.py` and is exercised here too.

`message_ids` is a derivative of `message_history`, not a parallel store —
see `rebuild_message_ids` in conversation_monitor.py. Every test below checks
that one invariant after the reshape it targets.
"""

import time

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.coordinators.discord_coordinator import DiscordCoordinator
from dpc_client_core.service import CoreService

PARTICIPANTS = [{"node_id": "n1", "name": "User", "context": "local"}]


@pytest.fixture(autouse=True)
def _sign_with_a_test_key(signing_identity):
    """add_message signs every record; give it a key that isn't the machine's."""


@pytest.fixture(autouse=True)
def _always_persist(monkeypatch):
    """persist_history reads a settings file under Path.home() by default;
    pointing it straight at True keeps these tests off the real ~/.dpc.
    """
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))


def _monitor(tmp_path, cid):
    m = ConversationMonitor(conversation_id=cid, participants=PARTICIPANTS, llm_manager=None)
    m._get_history_path = lambda: tmp_path / cid / "history.json"
    return m


def _invariant_holds(monitor):
    return monitor.message_ids == {m.get("id") for m in monitor.message_history if m.get("id")}


class _StubApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _StubService:
    """The one thing both DiscordCoordinator and _delete_group_briefs need
    from a service: a monitor by conversation id. Returns the same instance
    every call, like the real KnowledgeService's cache — the methods under
    test mutate the monitor they're handed, so a fake that built a new one
    each time would verify nothing.
    """

    def __init__(self, monitor):
        self._monitors = {monitor.conversation_id: monitor}
        self.settings = None
        self.local_api = _StubApi()

    def _get_or_create_conversation_monitor(self, conversation_id, instruction_set_name=None):
        return self._monitors[conversation_id]


def test_discord_ttl_reset_rebuilds_the_dedup_set(tmp_path, monkeypatch):
    """_check_conversation_ttl clears message_history wholesale on an idle
    conversation. Without the rebuild, message_ids keeps every id the wipe
    just discarded.
    """
    monkeypatch.setattr(
        "dpc_client_core.coordinators.discord_coordinator.DPC_HOME", tmp_path / ".dpc"
    )

    conv_id = "discord-user-42"
    monitor = _monitor(tmp_path, conv_id)
    for text in ("one", "two", "three"):
        monitor.add_message(role="user", content=text, sender_node_id="n1", sender_name="Mike")
    assert _invariant_holds(monitor)

    coordinator = DiscordCoordinator(_StubService(monitor), discord_manager=None)
    coordinator._user_conversations["42"] = conv_id
    coordinator._user_last_message["42"] = time.time() - 3600  # older than the 30-min default TTL

    coordinator._check_conversation_ttl("42")

    assert monitor.message_history == []
    assert _invariant_holds(monitor)


def test_discord_trim_rebuilds_the_dedup_set(tmp_path, monkeypatch):
    """_trim_conversation drops the oldest messages once the conversation
    exceeds max_messages. Without the rebuild, message_ids keeps the ids of
    the messages just trimmed off the front.
    """
    monkeypatch.setattr(
        "dpc_client_core.coordinators.discord_coordinator.DPC_HOME", tmp_path / ".dpc"
    )

    conv_id = "discord-user-42"
    monitor = _monitor(tmp_path, conv_id)
    for text in ("one", "two", "three", "four", "five"):
        monitor.add_message(role="user", content=text, sender_node_id="n1", sender_name="Mike")
    assert _invariant_holds(monitor)

    coordinator = DiscordCoordinator(_StubService(monitor), discord_manager=None)
    coordinator._user_conversations["42"] = conv_id
    # Override so the trim fires without needing 50+ messages.
    coordinator._get_conversation_config = lambda: {"ttl_minutes": 30, "max_messages": 3}

    coordinator._trim_conversation("42")

    assert len(monitor.message_history) == 3
    assert _invariant_holds(monitor)


@pytest.mark.asyncio
async def test_delete_group_briefs_rebuilds_the_dedup_set(tmp_path):
    """The slice `_delete_group_briefs` does on message_history had no
    matching update to message_ids: a deleted brief's id stayed in the set,
    blocking a re-merge of the same brief in-process and letting it come back
    after a restart.
    """
    conv_id = "group-brief-cleanup"
    monitor = _monitor(tmp_path, conv_id)
    monitor.add_message(
        role="assistant",
        content="**Morning Brief (Sleep Consolidation)**\nStuff happened.",
        sender_node_id="agent-1",
        sender_name="Nightwatch",
    )
    monitor.add_message(role="user", content="unrelated", sender_node_id="n1", sender_name="Mike")
    assert _invariant_holds(monitor)

    deleted = await CoreService._delete_group_briefs(_StubService(monitor), conv_id, "Nightwatch")

    assert deleted == 1
    assert len(monitor.message_history) == 1
    assert _invariant_holds(monitor)
