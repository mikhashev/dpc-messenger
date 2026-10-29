"""An agent pinned to a `compute_host` names its model by the PEER's alias.

`unresolved_references` compared every agent alias with this node's providers.json,
so an agent whose `provider_alias` lives on its compute host was reported as cut
off on every providers save. Only `provider_alias` is routed to the peer
(dpc_agent/llm_adapter.py); sleep, snapshot summarising and compaction aliases are
resolved by this node's own LLMManager, so they stay checked. Built in tmp_path.
"""

import json
from pathlib import Path

import pytest

from dpc_client_core import provider_alias_refs

PEER = "dpc-node-0123456789abcdef0123456789abcdef"
PEER_ONLY = "alias-that-only-the-peer-has"
KNOWN = ["deepseek_flash"]


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _agent(home: Path, agent_id: str, **config) -> None:
    _write(home / "agents" / agent_id / "config.json", {"agent_id": agent_id, **config})


def _registry(home: Path, **agents) -> None:
    _write(home / "agents" / "_registry.json", {"version": 1, "agents": {
        agent_id: {"agent_id": agent_id, **entry} for agent_id, entry in agents.items()
    }})


def _unresolved(home: Path):
    return provider_alias_refs.unresolved_references(KNOWN, home)


def test_a_peer_only_alias_of_an_agent_with_a_compute_host_is_not_reported(tmp_path):
    _agent(tmp_path, "agent_a", provider_alias=PEER_ONLY, compute_host=PEER)

    assert _unresolved(tmp_path) == []


def test_an_alias_the_node_resolves_itself_is_still_reported_for_that_agent(tmp_path):
    _agent(tmp_path, "agent_a", provider_alias=PEER_ONLY, compute_host=PEER,
           sleep_provider_alias="gone-1", snapshot_summarize_provider="gone-2",
           compaction_provider="gone-3")

    assert _unresolved(tmp_path) == [
        "agents/agent_a/config.json:sleep_provider_alias → 'gone-1'",
        "agents/agent_a/config.json:snapshot_summarize_provider → 'gone-2'",
        "agents/agent_a/config.json:compaction_provider → 'gone-3'",
    ]


def test_an_agent_without_a_compute_host_is_reported_as_before(tmp_path):
    _agent(tmp_path, "agent_a", provider_alias=PEER_ONLY)
    _agent(tmp_path, "agent_b", provider_alias=PEER_ONLY, compute_host="")
    _agent(tmp_path, "agent_c", provider_alias=PEER_ONLY, compute_host=None)

    assert _unresolved(tmp_path) == [
        f"agents/agent_{x}/config.json:provider_alias → '{PEER_ONLY}'" for x in "abc"
    ]


def test_the_registry_entry_of_an_agent_with_a_compute_host_is_not_reported(tmp_path):
    _agent(tmp_path, "agent_a", provider_alias=PEER_ONLY, compute_host=PEER)
    _agent(tmp_path, "agent_b", provider_alias=PEER_ONLY)
    _registry(tmp_path,
              agent_a={"provider_alias": PEER_ONLY},
              agent_b={"provider_alias": PEER_ONLY})

    assert _unresolved(tmp_path) == [
        f"agents/agent_b/config.json:provider_alias → '{PEER_ONLY}'",
        f"agents/_registry.json:agent_b.provider_alias → '{PEER_ONLY}'",
    ]


def test_a_registry_entry_with_no_config_file_speaks_with_its_own_compute_host(tmp_path):
    _registry(tmp_path,
              agent_a={"provider_alias": PEER_ONLY, "compute_host": PEER},
              agent_b={"provider_alias": PEER_ONLY, "compute_host": ""})

    assert _unresolved(tmp_path) == [
        f"agents/_registry.json:agent_b.provider_alias → '{PEER_ONLY}'",
    ]


def test_config_json_decides_when_it_disagrees_with_the_registry_copy(tmp_path):
    _agent(tmp_path, "agent_a", provider_alias=PEER_ONLY)  # unpinned now
    _registry(tmp_path, agent_a={"provider_alias": PEER_ONLY, "compute_host": PEER})

    assert len(_unresolved(tmp_path)) == 2


def test_find_references_and_rename_still_see_an_agent_that_runs_on_a_peer(tmp_path):
    _agent(tmp_path, "agent_a", provider_alias=PEER_ONLY, compute_host=PEER)
    _registry(tmp_path, agent_a={"provider_alias": PEER_ONLY, "compute_host": PEER})

    assert provider_alias_refs.find_references(PEER_ONLY, tmp_path) == [
        "agents/agent_a/config.json:provider_alias",
        "agents/_registry.json:agent_a.provider_alias",
    ]
    result = provider_alias_refs.rename_references(PEER_ONLY, "renamed", tmp_path)
    assert result["agent_configs"] == 1 and result["registry"] == 1
    config = json.loads((tmp_path / "agents" / "agent_a" / "config.json").read_text())
    assert config["provider_alias"] == "renamed" and config["compute_host"] == PEER
