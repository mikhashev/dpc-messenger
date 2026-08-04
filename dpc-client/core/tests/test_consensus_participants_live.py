"""Consensus must count votes against the group as it is now.

`ConversationMonitor.participants` was assigned once in the constructor and
never again. A monitor born before the second node joined made every proposal
claim one participant, so the proposer's own vote was unanimity and the other
person's arrived after the session had already closed.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dpc_client_core.knowledge_service import KnowledgeService

LOCAL = "dpc-node-local"
REMOTE = "dpc-node-remote"
GROUP = "group-970e5c7006a0"


def _service(members):
    group = SimpleNamespace(name="work", members=list(members), agents={}, is_discord_bridge=False)
    svc = KnowledgeService.__new__(KnowledgeService)
    svc.p2p_manager = SimpleNamespace(node_id=LOCAL)
    svc.group_manager = SimpleNamespace(get_group=lambda cid: group if cid == GROUP else None)
    svc.peer_metadata = {}
    svc.conversation_monitors = {}
    return svc, group


def test_group_participants_follow_a_late_join():
    """The defect verbatim: a monitor made before the invite must not stay alone."""
    svc, group = _service([LOCAL])

    svc.conversation_monitors[GROUP] = MagicMock(participants=svc._build_participants(GROUP))
    assert len(svc.conversation_monitors[GROUP].participants) == 1

    group.members.append(REMOTE)  # the second node is invited later

    monitor = svc._get_or_create_conversation_monitor(GROUP)

    ids = {p["node_id"] for p in monitor.participants}
    assert ids == {LOCAL, REMOTE}, "a proposal built from this list would still be a solo vote"


def test_departure_is_reflected_too():
    svc, group = _service([LOCAL, REMOTE])
    svc.conversation_monitors[GROUP] = MagicMock(participants=svc._build_participants(GROUP))

    group.members.remove(REMOTE)

    monitor = svc._get_or_create_conversation_monitor(GROUP)
    assert {p["node_id"] for p in monitor.participants} == {LOCAL}


def test_local_node_is_marked_local_and_peers_are_not():
    svc, _ = _service([LOCAL, REMOTE])
    by_id = {p["node_id"]: p for p in svc._build_participants(GROUP)}
    assert by_id[LOCAL]["context"] == "local"
    assert by_id[REMOTE]["context"] == "peer"


def test_one_to_one_conversation_is_untouched():
    """Only groups have a roster that moves; peer chats must not start churning."""
    svc, _ = _service([LOCAL, REMOTE])
    ids = {p["node_id"] for p in svc._build_participants("dpc-node-peer")}
    assert ids == {LOCAL, "dpc-node-peer"}


def test_missing_group_falls_back_to_the_local_node_alone():
    svc, _ = _service([LOCAL])
    assert svc._build_participants("group-does-not-exist") == [
        {"node_id": LOCAL, "name": "User", "context": "local"}
    ]
