"""Which branch decided the effort, and why nothing else can answer that.

A room was billed at max for twelve minutes while its own metadata read high, with
no command and no restart at either edge. The transition log built for that window
could not have fired once: it watches the file, and the file never moved. The step
where one level becomes another is this resolution, and until it names its source
the four ways of arriving at an unresolved effort — and from there at the alias
ceiling — are indistinguishable in the log.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from dpc_client_core.managers.agent_manager import DpcAgentManager

CONFIG_READER = "dpc_client_core.dpc_agent.utils.load_agent_config"


def _manager(group=None, agent_id="agent_001") -> DpcAgentManager:
    service = SimpleNamespace(group_manager=SimpleNamespace(get_group=lambda _cid: group))
    return DpcAgentManager(service, {}, agent_id=agent_id)


def test_a_room_carrying_a_level_answers_and_is_named_as_the_source():
    manager = _manager(group=SimpleNamespace(reasoning_effort="high"))

    assert manager._resolve_reasoning_effort("group-b88b65076b85") == ("high", "group")


def test_a_conversation_that_is_not_a_room_never_consults_one(monkeypatch):
    """The fourth way to the ceiling: the room branch is gated on the id's shape,
    so a call named anything else skips it whatever the room holds."""
    consulted = []

    def _get_group(cid):
        consulted.append(cid)
        return SimpleNamespace(reasoning_effort="max")

    service = SimpleNamespace(group_manager=SimpleNamespace(get_group=_get_group))
    manager = DpcAgentManager(service, {}, agent_id="agent_001")
    monkeypatch.setattr(CONFIG_READER, lambda _id: {"reasoning_effort": "high"})

    assert manager._resolve_reasoning_effort("agent_001") == ("high", "agent-config")
    assert consulted == []


def test_a_room_without_a_level_falls_through_to_the_agents_own_config(monkeypatch):
    manager = _manager(group=SimpleNamespace(reasoning_effort=None))
    monkeypatch.setattr(CONFIG_READER, lambda _id: {"reasoning_effort": "high"})

    assert manager._resolve_reasoning_effort("group-b88b65076b85") == ("high", "agent-config")


def test_nobody_answering_is_a_source_of_its_own_and_not_a_level(monkeypatch):
    """The live shape of this: `~/.dpc/agents/default/` exists and has no config at
    all, so an agent resolving against that id ends the chain with nothing — which
    the provider then reads as "unspecified" and answers with the alias ceiling."""
    manager = _manager(group=None, agent_id="default")
    monkeypatch.setattr(CONFIG_READER, lambda _id: None)

    assert manager._resolve_reasoning_effort("group-b88b65076b85") == (None, "none")


def test_a_raise_is_named_in_the_log_rather_than_swallowed(monkeypatch, caplog):
    """This branch used to assign None and say nothing, so a failure to resolve and
    a deliberate absence produced the same silence and the same ceiling."""
    def _unreadable(_id):
        raise RuntimeError("config unreadable")

    manager = _manager(group=None)
    monkeypatch.setattr(CONFIG_READER, _unreadable)

    with caplog.at_level(logging.WARNING):
        assert manager._resolve_reasoning_effort("group-b88b65076b85") == (None, "exception")

    assert "config unreadable" in caplog.text
