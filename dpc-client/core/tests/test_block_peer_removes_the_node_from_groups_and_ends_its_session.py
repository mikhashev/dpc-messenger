"""`block_peer` writes the block, removes the node from every group and ends its session.

Mike's call, 2026-10-31. Block, in one command: the id goes into
`blocked_nodes` and out of every other rule; every chat group whose roster
holds it drops it through the existing `remove_group_member` (which tells the
removed node and the rest by GROUP_SYNC); file transfers with it are cancelled
and its session ends through `disconnect_from_peer`. The UI reads the answer
(`removed_from_groups`, `removed_rules`, `was_connected`) and the
`firewall_rules_updated` broadcast. Unblock lifts the gate and nothing else.

Real `CoreService` methods, real `ContextFirewall` and `GroupManager`, both in
tmp_path; the network and the UI are recorders.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.service import CoreService

SELF = "dpc-node-" + "a" * 32
BLOCKED = "dpc-node-" + "b" * 32
OTHER = "dpc-node-" + "c" * 32


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))

    def named(self, name):
        return [p for n, p in self.events if n == name]


class _P2P:
    def __init__(self, connected):
        self.node_id = SELF
        self.peers = {n: object() for n in connected}
        self.sent = []

    async def send_message_to_peer(self, node_id, message):
        self.sent.append((node_id, message))


class _Transfers:
    def __init__(self, transfers):
        self.active_transfers = dict(transfers)
        self.cancelled = []

    async def cancel_transfer(self, transfer_id, reason="user_cancelled"):
        self.cancelled.append((transfer_id, reason))
        self.active_transfers.pop(transfer_id, None)


@pytest.fixture
def node(tmp_path):
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({
        "nodes": {BLOCKED: {"personal.json:*": "allow"}},
        "node_groups": {"friends": [BLOCKED, OTHER]},
    }))
    service = CoreService.__new__(CoreService)
    service.firewall = ContextFirewall(rules)
    service.group_manager = GroupManager(tmp_path / "home", SELF)
    service.p2p_manager = _P2P(connected=[BLOCKED, OTHER])
    service.local_api = _Api()
    service.file_transfer_manager = _Transfers({
        "t-blocked": SimpleNamespace(node_id=BLOCKED, filename="a.txt", direction="download"),
        "t-other": SimpleNamespace(node_id=OTHER, filename="b.txt", direction="upload"),
    })
    service.llm_manager = SimpleNamespace(providers={})
    service._notify_peers_of_provider_changes = AsyncMock()
    service._user_disconnected_peers = set()
    service._reconnect_tasks = {}
    service._reconnect_slow_phase = set()
    service.p2p_coordinator = SimpleNamespace(disconnect=AsyncMock())
    with_blocked = service.group_manager.create_group("with", "", [BLOCKED, OTHER])
    without = service.group_manager.create_group("without", "", [OTHER])
    return SimpleNamespace(service=service, with_blocked=with_blocked, without=without)


@pytest.mark.asyncio
async def test_block_answers_with_what_it_removed(node):
    result = await node.service.block_peer(BLOCKED)

    assert result["status"] == "success"
    assert result["node_id"] == BLOCKED
    assert result["removed_from_groups"] == [node.with_blocked.group_id]
    assert sorted(result["removed_rules"]) == sorted([f"nodes.{BLOCKED}", "node_groups.friends"])
    assert result["was_connected"] is True


@pytest.mark.asyncio
async def test_block_removes_the_node_from_its_groups_and_tells_it(node):
    service = node.service

    await service.block_peer(BLOCKED)

    assert BLOCKED not in service.group_manager.get_group(node.with_blocked.group_id).members
    assert OTHER in service.group_manager.get_group(node.with_blocked.group_id).members
    told = [m for to, m in service.p2p_manager.sent if to == BLOCKED]
    assert [m["command"] for m in told] == ["GROUP_SYNC"]
    assert BLOCKED not in told[0]["payload"]["members"]
    # The positive control: a group it was never in is not touched.
    assert service.group_manager.get_group(node.without.group_id).version == node.without.version


@pytest.mark.asyncio
async def test_block_ends_the_session_and_cancels_only_its_transfers(node):
    service = node.service

    await service.block_peer(BLOCKED)

    service.p2p_coordinator.disconnect.assert_awaited_once_with(BLOCKED)
    assert BLOCKED in service._user_disconnected_peers
    assert service.file_transfer_manager.cancelled == [("t-blocked", "user_cancelled")]
    assert "t-other" in service.file_transfer_manager.active_transfers
    cancelled = service.local_api.named("file_transfer_cancelled")
    assert [e["transfer_id"] for e in cancelled] == ["t-blocked"]


@pytest.mark.asyncio
async def test_block_writes_the_list_and_broadcasts_the_rules_update(node):
    service = node.service

    await service.block_peer(BLOCKED)

    assert service.firewall.is_blocked(BLOCKED)
    assert service.firewall.get_rules_as_dict()["blocked_nodes"] == [BLOCKED]
    assert len(service.local_api.named("firewall_rules_updated")) == 1
    answer = await service.get_firewall_rules()
    assert answer["rules"]["blocked_nodes"] == [BLOCKED]


@pytest.mark.asyncio
async def test_an_offline_node_is_blocked_too_and_owed_its_removal(node):
    service = node.service
    service.p2p_manager.peers.pop(BLOCKED)

    result = await service.block_peer(BLOCKED)

    assert result["status"] == "success" and result["was_connected"] is False
    assert node.with_blocked.group_id in service.group_manager.removals_owed_to(BLOCKED)


@pytest.mark.asyncio
@pytest.mark.parametrize("node_id, words", [
    (SELF, "itself"),
    ("dpc-node-alice-123", "Not a node id"),
    ("", "Not a node id"),
])
async def test_block_refuses_its_own_id_and_a_malformed_one(node, node_id, words):
    result = await node.service.block_peer(node_id)

    assert result["status"] == "error" and words in result["message"]
    assert node.service.firewall.get_rules_as_dict().get("blocked_nodes", []) == []
    node.service.p2p_coordinator.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_block_refuses_a_node_already_blocked(node):
    await node.service.block_peer(BLOCKED)

    again = await node.service.block_peer(BLOCKED)

    assert again["status"] == "error" and "already blocked" in again["message"]


@pytest.mark.asyncio
async def test_unblock_lifts_the_gate_and_puts_back_no_group_and_no_rule(node):
    service = node.service
    await service.block_peer(BLOCKED)
    service.p2p_coordinator.disconnect.reset_mock()
    sent_before = len(service.p2p_manager.sent)

    result = await service.unblock_peer(BLOCKED)

    assert result == {"status": "success", "node_id": BLOCKED}
    assert not service.firewall.is_blocked(BLOCKED)
    assert BLOCKED not in service.group_manager.get_group(node.with_blocked.group_id).members
    rules = service.firewall.get_rules_as_dict()
    assert BLOCKED not in json.dumps(rules)
    assert len(service.p2p_manager.sent) == sent_before, "no dial, no sync"
    assert len(service.local_api.named("firewall_rules_updated")) == 2


@pytest.mark.asyncio
async def test_unblock_of_a_node_that_is_not_blocked_is_an_error(node):
    result = await node.service.unblock_peer(OTHER)

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_an_editor_save_from_before_the_block_does_not_lift_it(node):
    service = node.service
    stale = service.firewall.get_rules_as_dict()
    await service.block_peer(BLOCKED)

    result = await service.save_firewall_rules(stale)

    assert result["status"] == "success"
    assert service.firewall.is_blocked(BLOCKED)


def test_both_commands_are_reachable_from_the_ui_like_disconnect():
    from dpc_client_core.local_api import ALLOWED_COMMANDS

    assert {"block_peer", "unblock_peer", "disconnect_from_peer"} <= ALLOWED_COMMANDS
