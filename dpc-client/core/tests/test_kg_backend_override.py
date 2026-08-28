"""One agent must be able to move before the other seven.

The `[knowledge_graph] backend` setting is fleet-global, so the only migration it can
express is all eight agents at once onto a store format nobody has moved before. Both
external reviews and both colleagues asked for the same shape instead — one agent, a
day of watching, then the rest — and until this override existed nothing in the code
could say it.
"""

from __future__ import annotations

import json

import pytest

from dpc_client_core.dpc_agent.knowledge_graph import KnowledgeGraph


def _agent(tmp_path, config: dict | None = None, name: str = "agent_pilot"):
    root = tmp_path / "agents" / name
    root.mkdir(parents=True)
    if config is not None:
        (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return root


def _store_name(kg: KnowledgeGraph) -> str:
    return kg.snapshot()["store_path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


def test_an_agent_can_choose_its_own_backend(tmp_path):
    kg = KnowledgeGraph(_agent(tmp_path, {"kg_backend": "sqlite"}))
    assert kg.snapshot()["backend"] == "sqlite"
    assert _store_name(kg) == "knowledge_graph.db"
    kg.backend.close()


def test_the_explicit_argument_still_wins(tmp_path):
    """Tests and migration scripts must be able to say exactly what they mean."""
    kg = KnowledgeGraph(_agent(tmp_path, {"kg_backend": "grafeo"}), backend="sqlite")
    assert kg.snapshot()["backend"] == "sqlite"
    kg.backend.close()


def test_an_agent_that_says_nothing_follows_the_fleet(tmp_path, monkeypatch):
    from dpc_client_core import settings as settings_module

    monkeypatch.setattr(settings_module.Settings, "get_kg_backend", lambda self: "sqlite")
    kg = KnowledgeGraph(_agent(tmp_path, {"sleep_provider_alias": "deepseek_flash"}))
    assert kg.snapshot()["backend"] == "sqlite"
    kg.backend.close()

    kg2 = KnowledgeGraph(_agent(tmp_path, None, name="agent_no_config"))
    assert kg2.snapshot()["backend"] == "sqlite"
    kg2.backend.close()


@pytest.mark.parametrize("bad", ["sqlite3", "", "Grafeo!", 7, None])
def test_a_value_this_build_does_not_know_is_ignored_not_obeyed(tmp_path, monkeypatch, bad):
    """A typo must not decide which file a graph lives in.

    Falling through to the fleet setting keeps the agent where it already was;
    guessing would open a different store and lose sight of the old one, which is the
    failure the whole migration is written around.
    """
    from dpc_client_core import settings as settings_module

    monkeypatch.setattr(settings_module.Settings, "get_kg_backend", lambda self: "sqlite")
    root = _agent(tmp_path, {"kg_backend": bad}, name=f"agent_bad_{abs(hash(str(bad)))}")
    kg = KnowledgeGraph(root)
    assert kg.snapshot()["backend"] == "sqlite"
    kg.backend.close()


def test_unreadable_config_does_not_stop_the_graph_opening(tmp_path, monkeypatch):
    from dpc_client_core import settings as settings_module

    monkeypatch.setattr(settings_module.Settings, "get_kg_backend", lambda self: "sqlite")
    root = _agent(tmp_path, name="agent_broken")
    (root / "config.json").write_text("{not json at all", encoding="utf-8")
    kg = KnowledgeGraph(root)
    assert kg.snapshot()["backend"] == "sqlite"
    kg.backend.close()
