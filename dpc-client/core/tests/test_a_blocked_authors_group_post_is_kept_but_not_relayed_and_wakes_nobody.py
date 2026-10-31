"""A blocked author's group post is kept, but not relayed onward and it wakes nobody.

Mike's call, 2026-10-31: records authored by a blocked node are kept, never
dropped or edited — they are signed and chained, and they come back with
every history sync from third nodes anyway. On this node such a post is
stored and shown as today (the UI folds it), and then: no relay onward, no
mention routing, no `cc_group_mention`, no agent run. The author is the one
the signature names (`sender_node_id`), or the node owning the agent
(`agent_owner`), never the hop that handed it over — so a post relayed by a
third member is held the same way.

Real `GroupTextHandler` and `ConversationMonitor`; records signed with real
keys whose certificates are cached where verification looks.
"""

import datetime
import logging
import uuid

import pytest

from dpc_protocol.message_signing import PREIMAGE_VERSION, message_content_hash
from dpc_client_core.message_handlers.group_handler import GroupTextHandler
from tests.test_a_mention_that_arrives_by_history_sync_wakes_once import (
    GROUP,
    _Service,
    _key,
)

THIRD = "dpc-node-" + "d" * 32
HANDLER_LOGGER = "dpc_client_core.message_handlers.GroupTextHandler"


class _Firewall:
    def __init__(self, *blocked):
        self.blocked = set(blocked)

    def is_blocked(self, node_id):
        return node_id in self.blocked


@pytest.fixture
def keys(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from dpc_protocol import commit_integrity
    from dpc_client_core.conversation_monitor import ConversationMonitor

    peers = tmp_path / "peers"
    peers.mkdir()
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(ConversationMonitor, "save_history", lambda self: True)
    return SimpleNamespace(blocked=_key(peers), friend=_key(peers))


def _post(key, text, *, sender_type=None, agent_owner=None, name="Mallory"):
    """A GROUP_TEXT payload as its author signs and sends it."""
    node_id, signer = key
    payload = {
        "group_id": GROUP,
        "message_id": uuid.uuid4().hex[:16],
        "text": text,
        "sender_name": name,
        "sender_node_id": node_id,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sender_type": sender_type,
        "agent_owner": agent_owner,
    }
    payload["content_hash"] = message_content_hash(
        conversation_id=GROUP, message_id=payload["message_id"], sender_node_id=node_id,
        sender_name=name, sender_type=sender_type, agent_owner=agent_owner,
        timestamp=payload["timestamp"], content=text, tool_calls=None,
        tool_calls_digest=None, mention_depth=None, version=PREIMAGE_VERSION,
    )
    payload["signature"] = signer.sign_commit(payload["content_hash"])
    payload["signer_node_id"] = node_id
    payload["preimage_version"] = PREIMAGE_VERSION
    return payload


def _node(keys, *blocked):
    service = _Service([keys.blocked[0], keys.friend[0], THIRD])
    service.firewall = _Firewall(*blocked)
    service.p2p_manager.peers = {THIRD: object()}  # a member we can relay to
    return service


def _relayed_to_third(service):
    return [c for c in service.p2p_manager.send_message_to_peer.await_args_list
            if c.args[0] == THIRD and c.args[1]["command"] == "GROUP_TEXT"]


def _shown(service):
    return [c.args[1] for c in service.local_api.broadcast_event.await_args_list
            if c.args[0] == "group_text_received"]


@pytest.mark.asyncio
async def test_a_blocked_authors_post_is_stored_and_shown(keys):
    service = _node(keys, keys.blocked[0])
    post = _post(keys.blocked, "hello all")

    await GroupTextHandler(service).handle(keys.blocked[0], post)

    stored = service.monitor.message_history[-1]
    assert stored["id"] == post["message_id"]
    assert stored["content_hash"] == post["content_hash"], "the record and its hash untouched"
    assert stored["signature"] == post["signature"]
    assert [e["message_id"] for e in _shown(service)] == [post["message_id"]]


@pytest.mark.asyncio
async def test_a_blocked_authors_post_is_not_relayed_and_says_so(keys, caplog):
    caplog.set_level(logging.INFO, logger=HANDLER_LOGGER)
    service = _node(keys, keys.blocked[0])

    await GroupTextHandler(service).handle(keys.blocked[0], _post(keys.blocked, "hello all"))

    assert _relayed_to_third(service) == []
    assert any("blocked node" in r.getMessage() and keys.blocked[0] in r.getMessage()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_an_unblocked_authors_post_is_relayed(keys):
    service = _node(keys, keys.blocked[0])

    await GroupTextHandler(service).handle(keys.friend[0], _post(keys.friend, "hello all"))

    assert len(_relayed_to_third(service)) == 1


@pytest.mark.asyncio
async def test_a_blocked_authors_mention_wakes_no_agent_and_no_external_agent(keys):
    service = _node(keys, keys.blocked[0])

    await GroupTextHandler(service).handle(keys.blocked[0], _post(keys.blocked, "@Ark @CC look"))

    assert service.run.await_count == 0
    assert service.cc_wakes() == []


@pytest.mark.asyncio
async def test_an_unblocked_authors_mention_wakes_both(keys):
    service = _node(keys, keys.blocked[0])

    await GroupTextHandler(service).handle(keys.friend[0], _post(keys.friend, "@Ark @CC look"))

    assert service.run.await_count == 1
    assert service.cc_wakes() == ["cc"]


@pytest.mark.asyncio
async def test_a_post_relayed_by_a_third_member_is_held_by_its_author(keys):
    """The hop is THIRD, unblocked; the signature names the blocked author."""
    service = _node(keys, keys.blocked[0])
    service.p2p_manager.peers = {THIRD: object(), keys.friend[0]: object()}

    await GroupTextHandler(service).handle(THIRD, _post(keys.blocked, "@Ark relayed"))

    assert service.monitor.message_history[-1]["sender_node_id"] == keys.blocked[0]
    assert service.run.await_count == 0
    relayed = [c for c in service.p2p_manager.send_message_to_peer.await_args_list
               if c.args[1]["command"] == "GROUP_TEXT"]
    assert relayed == []


@pytest.mark.asyncio
async def test_an_agent_post_whose_owner_is_blocked_is_held_the_same_way(keys):
    service = _node(keys, keys.blocked[0])
    post = _post(keys.friend, "@CC from my agent", sender_type="agent",
                 agent_owner=keys.blocked[0], name="Ark_m")

    await GroupTextHandler(service).handle(keys.friend[0], post)

    assert service.monitor.message_history[-1]["id"] == post["message_id"]
    assert _relayed_to_third(service) == []
    assert service.cc_wakes() == []
