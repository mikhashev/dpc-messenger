"""Auto-connection must follow the people we talk to, not only the firewall list.

Both the startup connect and the reconnect-after-drop asked the same question —
"is this peer in a firewall node group?" — and a peer who shares a group chat
with us but was never typed into that list got neither. Measured 2026-08-06:
`f9e0ec2d` dropped four times while outside every node group and produced zero
reconnect series; added to `friends` at 15:22:29, the next drop produced the
full five. The chat membership was there the whole time and nothing read it.

The two sources are asked once, as one set, so the two call sites cannot drift
apart again.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.service import CoreService

US = "dpc-node-" + "a" * 32
IN_FIREWALL = "dpc-node-" + "b" * 32
IN_CHAT = "dpc-node-" + "c" * 32


def _firewall(tmp_path, node_groups):
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps({"node_groups": node_groups}), encoding="utf-8")
    return ContextFirewall(path)


def _groups(tmp_path, members):
    manager = GroupManager(tmp_path, US)
    if members is not None:
        manager.create_group("work", "", list(members))
    return manager


def _service(tmp_path, node_groups, chat_members):
    """A service with the real firewall and the real group manager.

    Both are the objects whose shape the fix depends on, so neither is faked:
    a double built to match the call would agree with the call and prove
    nothing.
    """
    service = object.__new__(CoreService)
    service.firewall = _firewall(tmp_path / "fw", node_groups)
    service.group_manager = _groups(tmp_path / "groups", chat_members)
    service.p2p_manager = SimpleNamespace(node_id=US, peers={})
    return service


@pytest.fixture
def tmp_home(tmp_path):
    (tmp_path / "fw").mkdir()
    (tmp_path / "groups").mkdir()
    return tmp_path


def test_a_chat_member_outside_every_firewall_group_is_kept(tmp_home):
    """The defect verbatim: shared a group chat, never typed into the firewall."""
    service = _service(tmp_home, node_groups={"friends": []}, chat_members=[IN_CHAT])
    assert IN_CHAT in service._peers_to_auto_connect()


def test_a_firewall_group_member_is_still_kept(tmp_home):
    """The source that already worked must keep working."""
    service = _service(tmp_home, node_groups={"friends": [IN_FIREWALL]}, chat_members=None)
    assert IN_FIREWALL in service._peers_to_auto_connect()


def test_both_sources_are_one_set(tmp_home):
    service = _service(
        tmp_home, node_groups={"friends": [IN_FIREWALL]}, chat_members=[IN_CHAT]
    )
    assert service._peers_to_auto_connect() == {IN_FIREWALL, IN_CHAT}


def test_a_peer_in_both_sources_is_counted_once(tmp_home):
    service = _service(
        tmp_home, node_groups={"friends": [IN_CHAT], "colleagues": [IN_CHAT]},
        chat_members=[IN_CHAT],
    )
    assert service._peers_to_auto_connect() == {IN_CHAT}


def test_we_never_try_to_connect_to_ourselves(tmp_home):
    """`create_group` puts us in the roster; the set must not."""
    service = _service(
        tmp_home, node_groups={"friends": [US]}, chat_members=[IN_CHAT]
    )
    assert US not in service._peers_to_auto_connect()


def test_a_missing_group_manager_leaves_the_firewall_source_alone(tmp_home):
    """Early startup and the smaller tests have no group manager."""
    service = _service(tmp_home, node_groups={"friends": [IN_FIREWALL]}, chat_members=None)
    service.group_manager = None
    assert service._peers_to_auto_connect() == {IN_FIREWALL}


# --- wiring: the set is worthless if neither call site asks for it ---


@pytest.mark.asyncio
async def test_startup_connects_to_a_chat_member(tmp_home, monkeypatch):
    service = _service(tmp_home, node_groups={"friends": []}, chat_members=[IN_CHAT])
    service.settings = SimpleNamespace(get_p2p_auto_connect_delay=lambda: 0)
    attempted = []
    service.connection_orchestrator = SimpleNamespace(
        connect=lambda node_id: attempted.append(node_id) or asyncio.sleep(0)
    )

    await service._auto_connect_node_groups()

    assert attempted == [IN_CHAT]


@pytest.mark.asyncio
async def test_a_dropped_chat_member_gets_a_reconnect(tmp_home):
    service = _service(tmp_home, node_groups={"friends": []}, chat_members=[IN_CHAT])
    service.connection_orchestrator = SimpleNamespace()
    service.file_transfer_manager = SimpleNamespace(active_transfers={})
    service.local_api = SimpleNamespace(broadcast_event=_noop)
    service._history_requested_peers = set()
    service.history_requests = SimpleNamespace(forget_peer=lambda peer_id: None)
    service._background_tasks = set()
    scheduled = []
    service._auto_reconnect_peer = lambda peer_id: _record(scheduled, peer_id)

    await service._handle_peer_disconnected(IN_CHAT)
    await asyncio.sleep(0)

    assert scheduled == [IN_CHAT]


async def _noop(*args, **kwargs):
    return None


async def _record(sink, peer_id):
    sink.append(peer_id)
