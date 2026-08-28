"""Where the entity model runs was a choice nobody made.

`_get_gliner_model` took `"cuda" if torch.cuda.is_available() else "cpu"` and
loaded a process-wide singleton that has no unload path, so on any machine with
a card the backend held a second standing CUDA resident beside whisper — on a
box where the served model already takes 28 GiB of 32. The embedding model got
a setting for exactly this reason (ADR-040 0d); this is the same setting for
the other resident, resolved down the same three levels as the graph backend
next door.

These cover the resolution and the honesty of the singleton, not the load: a
test that actually loads GLiNER downloads 1.16 GB and decides nothing.
"""

import json
import logging

import pytest

from dpc_client_core.dpc_agent import knowledge_graph as kg
from dpc_client_core.settings import Settings


class TestTheAgentsOwnChoice:

    def _agent(self, tmp_path, value):
        (tmp_path / "config.json").write_text(
            json.dumps({"gliner_device": value}), encoding="utf-8",
        )
        return tmp_path

    def test_the_agent_config_decides_for_that_agent(self, tmp_path):
        assert kg._agent_gliner_device_override(self._agent(tmp_path, "cpu")) == "cpu"

    def test_case_and_spacing_do_not_change_the_answer(self, tmp_path):
        assert kg._agent_gliner_device_override(self._agent(tmp_path, "  CUDA ")) == "cuda"

    def test_a_typo_falls_through_instead_of_being_obeyed(self, tmp_path, caplog):
        """A misspelt device must not decide what holds VRAM."""
        with caplog.at_level(logging.WARNING):
            assert kg._agent_gliner_device_override(self._agent(tmp_path, "gpu")) is None
        assert "gliner_device" in caplog.text

    def test_no_config_and_no_key_both_fall_through(self, tmp_path):
        assert kg._agent_gliner_device_override(tmp_path) is None
        (tmp_path / "config.json").write_text("{}", encoding="utf-8")
        assert kg._agent_gliner_device_override(tmp_path) is None

    def test_unreadable_config_is_a_warning_not_a_crash(self, tmp_path, caplog):
        (tmp_path / "config.json").write_text("{ not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert kg._agent_gliner_device_override(tmp_path) is None


class TestTheFleetSetting:

    def _settings(self, tmp_path, ini: str) -> Settings:
        (tmp_path / "config.ini").write_text(ini, encoding="utf-8")
        return Settings(tmp_path)

    def test_the_default_is_what_every_install_had_before(self, tmp_path):
        assert self._settings(tmp_path, "[knowledge_graph]\nbackend = sqlite\n").get_gliner_device() == "auto"

    def test_a_named_device_travels(self, tmp_path):
        assert self._settings(tmp_path, "[knowledge_graph]\ngliner_device = cpu\n").get_gliner_device() == "cpu"

    def test_an_unknown_device_becomes_auto(self, tmp_path):
        assert self._settings(tmp_path, "[knowledge_graph]\ngliner_device = gpu0\n").get_gliner_device() == "auto"


class TestTheSingletonAnswersHonestly:
    """One model per process, the first caller decides — and a later caller who
    wanted something else is told, because silence reads as agreement."""

    @pytest.fixture(autouse=True)
    def _restore(self):
        model, device = kg._GLINER_MODEL, kg._GLINER_DEVICE
        yield
        kg._GLINER_MODEL, kg._GLINER_DEVICE = model, device

    def test_a_second_caller_wanting_another_device_is_told(self, caplog):
        kg._GLINER_MODEL, kg._GLINER_DEVICE = object(), "cuda"
        with caplog.at_level(logging.WARNING):
            returned = kg._get_gliner_model("cpu")
        assert returned is kg._GLINER_MODEL, "the loaded model is still the one returned"
        assert "already loaded on cuda" in caplog.text
        assert "request for cpu ignored" in caplog.text

    def test_agreement_is_silent(self, caplog):
        kg._GLINER_MODEL, kg._GLINER_DEVICE = object(), "cpu"
        with caplog.at_level(logging.WARNING):
            kg._get_gliner_model("cpu")
        assert caplog.text == ""

    def test_a_caller_with_no_opinion_is_silent(self, caplog):
        """`auto` and None are «whatever is loaded», not a disagreement."""
        kg._GLINER_MODEL, kg._GLINER_DEVICE = object(), "cuda"
        with caplog.at_level(logging.WARNING):
            kg._get_gliner_model(None)
            kg._get_gliner_model("auto")
        assert caplog.text == ""


class _FakeModel:
    def __init__(self):
        self.moved_to = None

    def to(self, device):
        self.moved_to = device
        return self


@pytest.fixture
def fake_gliner(monkeypatch):
    """A stand-in for the 1.16 GB download, so the load path itself is testable.

    `_get_gliner_model` imports gliner and torch inside the function, which is
    what makes this possible: the module lands in sys.modules before the call.
    `torch.cuda.is_available` is forced True so that "auto" and "cpu" have
    different answers — otherwise a CPU-only test machine would pass either way.
    """
    import sys
    import types

    made = {}

    class GLiNER:
        @staticmethod
        def from_pretrained(name):
            made["model"] = _FakeModel()
            return made["model"]

    module = types.ModuleType("gliner")
    module.GLiNER = GLiNER
    monkeypatch.setitem(sys.modules, "gliner", module)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    saved = kg._GLINER_MODEL, kg._GLINER_DEVICE
    kg._GLINER_MODEL, kg._GLINER_DEVICE = None, None
    yield made
    kg._GLINER_MODEL, kg._GLINER_DEVICE = saved


class TestTheConfiguredDeviceIsTheOneUsed:

    def test_cpu_is_honoured_on_a_machine_that_has_a_card(self, fake_gliner):
        """The whole point of the setting: a card being present is no longer
        the same statement as «put it on the card»."""
        kg._get_gliner_model("cpu")
        assert fake_gliner["model"].moved_to == "cpu"
        assert kg._GLINER_DEVICE == "cpu"

    def test_auto_still_takes_the_card_when_there_is_one(self, fake_gliner):
        kg._get_gliner_model("auto")
        assert fake_gliner["model"].moved_to == "cuda"

    def test_no_opinion_reads_as_auto(self, fake_gliner):
        kg._get_gliner_model(None)
        assert fake_gliner["model"].moved_to == "cuda"
