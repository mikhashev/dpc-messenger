"""GLiNER's device belongs to the node once config.ini names one.

The entity model is one per process. `[knowledge_graph] gliner_device` set to
cpu or cuda decides for every agent, and an agent whose own config.json asks for
something else is warned once. Under auto the agent's own value decides, as it
did before the node setting came first, and the log says so once.

Opens a real (sqlite) KnowledgeGraph in a temp home laid out like ~/.dpc, so
the Settings it builds reads this test's config.ini and nobody's real one.
"""

import json
import logging

import pytest

from dpc_client_core.dpc_agent import knowledge_graph as kg


@pytest.fixture(autouse=True)
def _clean_notices():
    kg._gliner_device_source_reported.clear()
    yield
    kg._gliner_device_source_reported.clear()


def _home(tmp_path, node_device=None, agent_device=None):
    ini = "[knowledge_graph]\nbackend = sqlite\n"
    if node_device is not None:
        ini += f"gliner_device = {node_device}\n"
    (tmp_path / "config.ini").write_text(ini, encoding="utf-8")
    agent_root = tmp_path / "agents" / "agent_x"
    agent_root.mkdir(parents=True)
    if agent_device is not None:
        (agent_root / "config.json").write_text(
            json.dumps({"gliner_device": agent_device}), encoding="utf-8")
    return agent_root


def _open(agent_root):
    return kg.KnowledgeGraph(agent_root, backend="sqlite")


def _from_kg(caplog, level):
    return [r for r in caplog.records if r.name == kg.log.name and r.levelno == level]


class TestANamedNodeDeviceWins:

    def test_cpu_on_the_node_wins_over_an_agent_that_says_cuda(self, tmp_path):
        assert _open(_home(tmp_path, "cpu", "cuda"))._gliner_device == "cpu"

    def test_the_overruled_agent_is_warned_once_across_opens(self, tmp_path, caplog):
        root = _home(tmp_path, "cpu", "cuda")
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                _open(root)
        warned = [r for r in _from_kg(caplog, logging.WARNING)
                  if "config.ini decides" in r.getMessage()]
        assert len(warned) == 1

    def test_an_agent_that_agrees_or_says_auto_is_silent(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            assert kg._resolve_gliner_device("cpu", "cpu", "a") == "cpu"
            assert kg._resolve_gliner_device("cpu", "auto", "a") == "cpu"
            assert kg._resolve_gliner_device("cpu", None, "a") == "cpu"
        assert _from_kg(caplog, logging.WARNING) == []


class TestAutoKeepsTodaysBehaviour:

    def test_the_agents_own_value_decides(self, tmp_path):
        assert _open(_home(tmp_path, "auto", "cpu"))._gliner_device == "cpu"

    def test_a_config_without_the_key_is_auto_too(self, tmp_path):
        assert _open(_home(tmp_path, None, "cpu"))._gliner_device == "cpu"

    def test_no_agent_value_is_auto(self, tmp_path):
        assert _open(_home(tmp_path, "auto", None))._gliner_device == "auto"

    def test_the_deciding_source_is_logged_once(self, tmp_path, caplog):
        root = _home(tmp_path, "auto", "cpu")
        with caplog.at_level(logging.INFO):
            _open(root)
            _open(root)
        said = [r for r in _from_kg(caplog, logging.INFO)
                if "config.json decides gliner_device=cpu" in r.getMessage()]
        assert len(said) == 1
        assert _from_kg(caplog, logging.WARNING) == []
