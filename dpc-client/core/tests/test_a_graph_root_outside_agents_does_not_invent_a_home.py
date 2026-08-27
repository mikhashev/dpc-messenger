"""A KnowledgeGraph built anywhere else must not adopt its grandparent as home.

`agent_root.parent.parent` is the DPC home only when the root really is
`~/.dpc/agents/<id>`. For a temp directory it is `/`, and Settings creates a
default config wherever it is pointed — so a test that opened a graph in a
temp dir wrote to `/config.ini` on the runner (PermissionError) and to a
stray config.ini inside the repo on Windows, which is the observed defect in
SETTINGS-CAN-WRITE-ITS-DEFAULT-CONFIG-INTO-THE-WORKING-DIRECTORY.
"""

from pathlib import Path

import pytest

from dpc_client_core.dpc_agent import knowledge_graph as kg


@pytest.fixture
def watched_settings(monkeypatch):
    """Records every DPC home a Settings is constructed on."""
    seen = []

    class _Settings:
        def __init__(self, dpc_home_dir):
            seen.append(Path(dpc_home_dir))

        def get_kg_backend(self):
            return "sqlite"

        def get_gliner_device(self):
            return "cpu"

    monkeypatch.setattr("dpc_client_core.settings.Settings", _Settings)
    return seen


def test_a_root_outside_agents_falls_back_to_the_real_home(tmp_path, monkeypatch, watched_settings):
    home = tmp_path / "home"
    monkeypatch.setenv("DPC_HOME", str(home))

    kg.KnowledgeGraph(tmp_path / "store")

    assert watched_settings, "the constructor must have asked Settings something"
    assert set(watched_settings) == {home}


def test_a_real_agent_root_still_resolves_to_its_own_home(tmp_path, monkeypatch, watched_settings):
    monkeypatch.setenv("DPC_HOME", str(tmp_path / "somewhere-else"))
    agent_root = tmp_path / ".dpc" / "agents" / "agent_001"
    agent_root.mkdir(parents=True)

    kg.KnowledgeGraph(agent_root)

    assert set(watched_settings) == {tmp_path / ".dpc"}
