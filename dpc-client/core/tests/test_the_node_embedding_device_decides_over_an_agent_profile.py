"""The embedding model's device belongs to the node, not to an agent.

The model is one per process, so "where it runs" has one answer for all agents.
`[memory] embedding_device` in config.ini gives it: any value but auto overrides
every profile's `memory.embedding_device`, and a profile that said otherwise is
warned once. Under auto nothing changes — the first profile to ask decides, as
before the key existed.

`torch.cuda.is_available` is forced True so that auto and cpu have different
answers on a CPU-only runner, and the loader is a stand-in: these test where the
weights would go, not a 2 GB download.
"""

import ast
import logging
import sys
import types
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent import memory
from dpc_client_core.settings import Settings


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    memory._singleton_providers.clear()
    memory._overridden_devices_reported.clear()
    saved = memory._node_embedding_device
    yield
    memory._singleton_providers.clear()
    memory._overridden_devices_reported.clear()
    memory._node_embedding_device = saved


@pytest.fixture
def fake_loader(monkeypatch):
    """SentenceTransformer replaced by a recorder of the device it was given."""
    made = {}

    class SentenceTransformer:
        def __init__(self, name, **kwargs):
            made["device"] = kwargs.get("device")
            self.max_seq_length = 8192

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = SentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return made


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


class TestANamedNodeDeviceWins:

    def test_cpu_on_the_node_puts_the_model_on_cpu_though_a_profile_says_cuda(self, fake_loader):
        memory.set_node_embedding_device("cpu")
        provider = memory.get_embedding_provider(model_name="test/node-a", device="cuda")
        assert provider.device == "cpu"
        provider._load_model()
        assert fake_loader["device"] == "cpu", "the weights go where config.ini says"

    def test_the_overruled_profile_is_warned_once(self, caplog):
        memory.set_node_embedding_device("cpu")
        with caplog.at_level(logging.WARNING, logger=memory.log.name):
            memory.get_embedding_provider(model_name="test/node-b", device="cuda")
            memory.get_embedding_provider(model_name="test/node-b", device="cuda")
            memory.get_embedding_provider(model_name="test/node-b", device="cuda")
        warned = _warnings(caplog)
        assert len(warned) == 1
        assert "the node setting in config.ini decides" in warned[0].getMessage()

    def test_a_profile_that_agrees_or_says_nothing_is_silent(self, caplog):
        memory.set_node_embedding_device("cpu")
        with caplog.at_level(logging.WARNING, logger=memory.log.name):
            memory.get_embedding_provider(model_name="test/node-c", device="cpu")
            memory.get_embedding_provider(model_name="test/node-c", device=None)
            memory.get_embedding_provider(model_name="test/node-c")
        assert _warnings(caplog) == []

    def test_a_caller_that_names_no_device_gets_the_node_one_too(self):
        """context.py and tools/core.py ask with no device at all."""
        memory.set_node_embedding_device("cpu")
        assert memory.get_embedding_provider(model_name="test/node-d").device == "cpu"

    def test_the_info_line_names_config_ini_as_the_source(self, caplog):
        memory.set_node_embedding_device("cpu")
        with caplog.at_level(logging.INFO, logger=memory.log.name):
            memory.get_embedding_provider(model_name="test/node-e", device="cuda")
        assert any("decided by [memory] embedding_device in config.ini" in r.getMessage()
                   for r in caplog.records)


class TestAutoKeepsTodaysBehaviour:

    def test_the_first_profile_to_ask_decides(self, fake_loader):
        memory.set_node_embedding_device("auto")
        provider = memory.get_embedding_provider(model_name="test/auto-a", device="cpu")
        assert provider.device == "cpu"
        provider._load_model()
        assert fake_loader["device"] == "cpu"

    def test_no_profile_value_is_the_automatic_choice(self, fake_loader):
        memory.set_node_embedding_device("auto")
        provider = memory.get_embedding_provider(model_name="test/auto-b", device=None)
        assert provider.device == "cuda"
        provider._load_model()
        assert fake_loader["device"] == "cuda"

    def test_a_later_profile_that_disagrees_gets_the_old_first_caller_warning(self, caplog):
        memory.set_node_embedding_device("auto")
        memory.get_embedding_provider(model_name="test/auto-c", device="cpu")
        with caplog.at_level(logging.WARNING, logger=memory.log.name):
            again = memory.get_embedding_provider(model_name="test/auto-c", device="cuda")
        assert again.device == "cpu"
        assert any("first caller decides" in r.getMessage() for r in caplog.records)
        assert not any("config.ini decides" in r.getMessage() for r in caplog.records)

    def test_the_info_line_names_the_profile_as_the_source(self, caplog):
        memory.set_node_embedding_device("auto")
        with caplog.at_level(logging.INFO, logger=memory.log.name):
            memory.get_embedding_provider(model_name="test/auto-d", device="cpu")
        assert any("decided by the first agent profile to ask" in r.getMessage()
                   for r in caplog.records)

    def test_a_process_that_never_set_it_behaves_as_auto(self):
        memory._node_embedding_device = None
        assert memory.get_embedding_provider(model_name="test/auto-e", device="cpu").device == "cpu"

    @pytest.mark.parametrize("value", [None, "", "AUTO", " auto ", "gpu0", 7])
    def test_anything_but_a_known_device_reads_as_auto(self, value):
        memory.set_node_embedding_device(value)
        assert memory._node_embedding_device is None

    def test_a_named_device_is_read_case_and_space_blind(self):
        memory.set_node_embedding_device("  CPU ")
        assert memory._node_embedding_device == "cpu"


class TestTheConfigIniKey:

    def _settings(self, tmp_path, ini):
        (tmp_path / "config.ini").write_text(ini, encoding="utf-8")
        return Settings(tmp_path)

    def test_a_missing_key_is_auto(self, tmp_path):
        assert self._settings(tmp_path, "[hub]\nurl = x\n").get_embedding_device() == "auto"

    @pytest.mark.parametrize("value", ["cpu", "cuda", "mps", "auto"])
    def test_a_named_device_travels(self, tmp_path, value):
        ini = f"[memory]\nembedding_device = {value}\n"
        assert self._settings(tmp_path, ini).get_embedding_device() == value

    def test_an_unknown_device_becomes_auto(self, tmp_path):
        ini = "[memory]\nembedding_device = gpu0\n"
        assert self._settings(tmp_path, ini).get_embedding_device() == "auto"


def test_the_service_hands_the_setting_over_right_after_reading_settings():
    """Structural: the push happens in CoreService.__init__, before any agent exists.

    Starting a CoreService here would bind ports; this reads the constructor
    instead and checks the call follows `self.settings = Settings(...)`.
    """
    src = (Path(memory.__file__).resolve().parents[1] / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    init = next(
        f for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.name == "CoreService"
        for f in c.body if isinstance(f, ast.FunctionDef) and f.name == "__init__"
    )
    lines = [ast.unparse(s) for s in init.body]
    settings_at = next(i for i, s in enumerate(lines) if s.startswith("self.settings = Settings("))
    push_at = next(i for i, s in enumerate(lines)
                   if s == "set_node_embedding_device(self.settings.get_embedding_device())")
    assert settings_at < push_at
    agents_at = [i for i, s in enumerate(lines) if "AgentManager(" in s or "agent_service" in s.lower()]
    assert agents_at, "the constructor no longer builds the agent service — re-anchor this test"
    assert all(push_at < i for i in agents_at)
