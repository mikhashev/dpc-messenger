"""`list_agents` says, per agent, which model roles name an alias this node lacks.

Deleting a provider left every agent that named it without that model, and the
only sign was the per-agent configuration dialog. The providers save already
computed the dangling references (`provider_alias_refs.unresolved_references`) and
returned them as warnings; the agents list now carries the same check per agent as
`missing_aliases` (role names), and `alias_roles` ({role: alias}) for the screen
that asks "who uses this provider?" before a delete. An agent pinned to a
`compute_host` resolves its aliases on the peer and reports none. Built in tmp_path.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core.agent_service import AgentService

PEER = "dpc-node-0123456789abcdef0123456789abcdef"
HELD = "deepseek_flash"
GONE = "qwen-that-was-deleted"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _agent(home: Path, agent_id: str, name: str, **config) -> None:
    _write(home / "agents" / agent_id / "config.json",
           {"agent_id": agent_id, "name": name, **config})


@pytest.fixture
def service(tmp_path, monkeypatch):
    # The registry and agent configs live under Path.home()/.dpc.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    home = tmp_path / ".dpc"
    llm = SimpleNamespace(config_path=home / "providers.json",
                          providers={HELD: object(), "dpc_agent": object()})
    return AgentService(llm, None, None, {}), home


def _register(home: Path, **agents) -> None:
    _write(home / "agents" / "_registry.json", {"version": 1, "agents": {
        agent_id: {"agent_id": agent_id, "name": agent_id, "profile_name": "default", **entry}
        for agent_id, entry in agents.items()
    }})


def _rows(svc):
    result = asyncio.run(svc.list_agents())
    assert result["status"] == "success"
    return {row["agent_id"]: row for row in result["agents"]}


def test_a_missing_main_alias_is_named(service):
    svc, home = service
    _agent(home, "agent_a", "Kotler", provider_alias=GONE, sleep_provider_alias=GONE,
           snapshot_summarize_provider=GONE)
    _register(home, agent_a={"provider_alias": GONE})

    row = _rows(svc)["agent_a"]

    assert row["missing_aliases"] == ["main", "sleep", "snapshot"]


def test_a_missing_sleep_alias_alone_leaves_main_unmarked(service):
    svc, home = service
    _agent(home, "agent_a", "Kotler", provider_alias=HELD, sleep_provider_alias=GONE,
           compaction_provider=HELD)
    _register(home, agent_a={"provider_alias": HELD})

    row = _rows(svc)["agent_a"]

    assert row["missing_aliases"] == ["sleep"]


def test_an_agent_on_a_peer_is_skipped_even_when_its_alias_is_the_peers(service):
    svc, home = service
    _agent(home, "agent_a", "Muse", provider_alias=GONE, sleep_provider_alias=GONE,
           compute_host=PEER)
    _register(home, agent_a={"provider_alias": GONE})

    row = _rows(svc)["agent_a"]

    assert row["missing_aliases"] == []
    assert row["alias_roles"] == {}


def test_when_every_alias_is_held_the_list_is_empty(service):
    svc, home = service
    _agent(home, "agent_a", "Kotler", provider_alias=HELD, sleep_provider_alias=HELD,
           snapshot_summarize_provider=HELD, compaction_provider=HELD)
    _register(home, agent_a={"provider_alias": HELD})

    row = _rows(svc)["agent_a"]

    assert row["missing_aliases"] == []


def test_alias_roles_name_what_each_agent_uses_held_or_not(service):
    svc, home = service
    _agent(home, "agent_a", "Kotler", provider_alias=HELD, sleep_provider_alias=GONE)
    _agent(home, "agent_b", "Plain")
    _register(home, agent_a={"provider_alias": HELD}, agent_b={"provider_alias": HELD})

    rows = _rows(svc)

    assert rows["agent_a"]["alias_roles"] == {"main": HELD, "sleep": GONE}
    assert rows["agent_b"]["alias_roles"] == {}
    assert rows["agent_b"]["missing_aliases"] == []


def test_the_agents_list_and_the_providers_save_use_one_check(service):
    from dpc_client_core import provider_alias_refs
    svc, home = service
    _agent(home, "agent_a", "Kotler", provider_alias=GONE, sleep_provider_alias=HELD)
    _agent(home, "agent_b", "Muse", provider_alias=GONE, compute_host=PEER)
    _register(home, agent_a={"provider_alias": GONE}, agent_b={"provider_alias": GONE})

    warned = provider_alias_refs.unresolved_references(list(svc.llm_manager.providers), home)
    rows = _rows(svc)

    assert "agents/agent_a/config.json:provider_alias → '%s'" % GONE in warned
    assert not any("agent_b/config.json" in w for w in warned)
    assert rows["agent_a"]["missing_aliases"] == ["main"]
    assert rows["agent_b"]["missing_aliases"] == []


def test_every_alias_key_the_configure_save_writes_is_a_listed_role():
    # The save writes config["<key>"]; a key ending in _alias or _provider that is
    # not in the inventory would be a model role the list never reports.
    import re
    from dpc_client_core import provider_alias_refs as refs
    source = (Path(refs.__file__).parent / "agent_service.py").read_text(encoding="utf-8")
    written = set(re.findall(r'config\["(\w+(?:_alias|_provider))"\]\s*=', source))
    assert written, "the save no longer writes config keys this way; fix the pattern"
    assert written <= set(refs.AGENT_CONFIG_KEYS)
    assert set(refs.AGENT_CONFIG_KEYS) == set(refs.AGENT_ROLE_NAMES)
