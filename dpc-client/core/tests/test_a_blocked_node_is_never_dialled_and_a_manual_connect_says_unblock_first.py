"""A blocked node is never dialled, and a manual connect to it says to unblock first.

`_peers_to_auto_connect` takes `node_groups` and every chat group's roster, so
a node a third member put back in a group stayed in the dial set; and the
user's Disconnect mark (`_user_disconnected_peers`) lives in memory only and is
cleared on the next link. The dial set now drops a blocked node by the block
list, `_worth_dialling` says no to it, and so `_keeps_dialling` and the
reconnect after a loss never start. A manual `connect_to_peer`,
`connect_to_peer_by_id` or `connect_via_dht` answers with an error that names
the way out.

Real `CoreService` methods and a real `ContextFirewall` in tmp_path.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.service import CoreService

SELF = "dpc-node-" + "a" * 32
BLOCKED = "dpc-node-" + "b" * 32
FRIEND = "dpc-node-" + "c" * 32


@pytest.fixture
def service(tmp_path):
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({
        "node_groups": {"friends": [FRIEND]},
        "blocked_nodes": [BLOCKED],
    }))
    s = CoreService.__new__(CoreService)
    s.firewall = ContextFirewall(rules)
    group = SimpleNamespace(members=[SELF, BLOCKED, FRIEND])  # a third member put it back
    s.group_manager = SimpleNamespace(get_all_groups=lambda: [group])
    s.p2p_manager = SimpleNamespace(node_id=SELF, peers={}, peer_cache=None)
    s._is_running = True
    s._user_disconnected_peers = set()
    s._reconnect_tasks = {}
    s._reconnect_slow_phase = set()
    s._background_tasks = set()
    s._history_requested_peers = set()
    s.history_requests = SimpleNamespace(forget_peer=lambda peer: None)
    s.file_transfer_manager = SimpleNamespace(active_transfers={})
    s.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    s.connection_orchestrator = SimpleNamespace(connect=AsyncMock())
    s.p2p_coordinator = SimpleNamespace(connect_via_uri=AsyncMock(), connect_via_hub=AsyncMock())
    return s


def test_the_dial_set_leaves_out_a_blocked_member_and_keeps_the_rest(service):
    assert service._peers_to_auto_connect() == {FRIEND}


def test_no_redial_question_says_yes_to_a_blocked_node(service):
    assert service._worth_dialling(BLOCKED) is False
    assert service._keeps_dialling(BLOCKED) is False
    assert service._worth_dialling(FRIEND) is True
    assert service._keeps_dialling(FRIEND) is True


@pytest.mark.asyncio
async def test_a_lost_blocked_node_gets_no_reconnect_task(service):
    await service._handle_peer_disconnected(BLOCKED)
    assert BLOCKED not in service._reconnect_tasks

    await service._handle_peer_disconnected(FRIEND)
    assert FRIEND in service._reconnect_tasks
    service._reconnect_tasks[FRIEND].cancel()


@pytest.mark.asyncio
async def test_the_startup_auto_connect_dials_only_the_unblocked(service):
    service.settings = SimpleNamespace(get_p2p_auto_connect_delay=lambda: 0)

    await service._auto_connect_node_groups()

    dialled = [c.args[0] for c in service.connection_orchestrator.connect.await_args_list]
    assert dialled == [FRIEND]


@pytest.mark.asyncio
async def test_a_manual_connect_by_uri_to_a_blocked_node_says_unblock_first(service):
    result = await service.connect_to_peer(f"dpc://192.0.2.10:8888?node_id={BLOCKED}")

    assert result["status"] == "error" and "Unblock it first" in result["message"]
    service.p2p_coordinator.connect_via_uri.assert_not_awaited()

    await service.connect_to_peer(f"dpc://192.0.2.11:8888?node_id={FRIEND}")
    service.p2p_coordinator.connect_via_uri.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_manual_connect_by_id_to_a_blocked_node_says_unblock_first(service):
    by_hub = await service.connect_to_peer_by_id(BLOCKED)
    by_dht = await service.connect_via_dht(BLOCKED)

    assert by_hub["status"] == "error" and "Unblock it first" in by_hub["message"]
    assert by_dht["status"] == "error" and "Unblock it first" in by_dht["message"]
    service.p2p_coordinator.connect_via_hub.assert_not_awaited()
    service.connection_orchestrator.connect.assert_not_awaited()

    await service.connect_to_peer_by_id(FRIEND)
    assert (await service.connect_via_dht(FRIEND))["status"] == "success"
    service.p2p_coordinator.connect_via_hub.assert_awaited_once_with(FRIEND)
