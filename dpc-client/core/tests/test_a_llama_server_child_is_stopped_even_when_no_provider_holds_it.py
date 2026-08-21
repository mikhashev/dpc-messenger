"""A llama-server child outlives the provider that started it when an alias is renamed or dropped,
so the supervisor registry — not llm_manager.providers — has to be what shutdown and reload consult."""

import asyncio
import json
from pathlib import Path

import pytest

from dpc_client_core.providers import llamacpp_server_provider as lsp


class FakeSupervisor:
    def __init__(self, alias: str, started: bool = True, raises: bool = False):
        self.alias = alias
        self.props = {"vision": False} if started else None
        self.raises = raises
        self.stopped = False

    async def stop(self):
        if self.raises:
            raise OSError("the child is already gone")
        self.stopped = True


@pytest.fixture(autouse=True)
def clean_registry():
    lsp._ACTIVE_SUPERVISORS.clear()
    yield
    lsp._ACTIVE_SUPERVISORS.clear()


class TestAnAliasThatLeftTheConfiguration:
    @pytest.mark.asyncio
    async def test_its_child_is_stopped(self):
        gone = FakeSupervisor("llama.cpp-abl")
        lsp._ACTIVE_SUPERVISORS["llama.cpp-abl"] = gone

        retired = lsp.retire_absent(["qwen3.8 27b Mythos", "deepseek_flash"])
        await asyncio.sleep(0)

        assert retired == ["llama.cpp-abl"]
        assert gone.stopped is True
        assert "llama.cpp-abl" not in lsp._ACTIVE_SUPERVISORS

    @pytest.mark.asyncio
    async def test_an_alias_that_survived_the_reload_keeps_its_child(self):
        kept = FakeSupervisor("llama.cpp")
        lsp._ACTIVE_SUPERVISORS["llama.cpp"] = kept

        retired = lsp.retire_absent(["llama.cpp", "deepseek_flash"])
        await asyncio.sleep(0)

        assert retired == []
        assert kept.stopped is False
        assert lsp._ACTIVE_SUPERVISORS["llama.cpp"] is kept

    @pytest.mark.asyncio
    async def test_a_supervisor_that_never_started_is_not_reported_as_a_stopped_child(self):
        never_ran = FakeSupervisor("typo-alias", started=False)
        lsp._ACTIVE_SUPERVISORS["typo-alias"] = never_ran

        retired = lsp.retire_absent(["llama.cpp"])
        await asyncio.sleep(0)

        assert retired == []
        assert never_ran.stopped is False


class TestShutdownReachesAChildNoProviderHolds:
    @pytest.mark.asyncio
    async def test_every_registered_child_is_stopped_and_the_registry_is_emptied(self):
        one = FakeSupervisor("llama.cpp-abl")
        two = FakeSupervisor("llama.cpp")
        lsp._ACTIVE_SUPERVISORS.update({"llama.cpp-abl": one, "llama.cpp": two})

        stopped = await lsp.stop_all_supervisors()

        assert sorted(stopped) == ["llama.cpp", "llama.cpp-abl"]
        assert one.stopped and two.stopped
        assert lsp._ACTIVE_SUPERVISORS == {}

    @pytest.mark.asyncio
    async def test_one_child_that_cannot_be_stopped_does_not_strand_the_others(self):
        bad = FakeSupervisor("broken", raises=True)
        good = FakeSupervisor("llama.cpp")
        lsp._ACTIVE_SUPERVISORS.update({"broken": bad, "llama.cpp": good})

        stopped = await lsp.stop_all_supervisors()

        assert stopped == ["llama.cpp"]
        assert good.stopped is True
        assert lsp._ACTIVE_SUPERVISORS == {}

    @pytest.mark.asyncio
    async def test_the_managers_shutdown_reaches_an_orphan_the_providers_dict_cannot_see(
        self, tmp_path: Path
    ):
        from dpc_client_core.llm_manager import LLMManager

        config = tmp_path / "providers.json"
        config.write_text(json.dumps({"default_provider": "", "providers": []}), encoding="utf-8")
        manager = LLMManager(config_path=config)
        assert "llama.cpp-abl" not in manager.providers

        orphan = FakeSupervisor("llama.cpp-abl")
        lsp._ACTIVE_SUPERVISORS["llama.cpp-abl"] = orphan

        await manager.shutdown()

        assert orphan.stopped is True
        assert lsp._ACTIVE_SUPERVISORS == {}

    @pytest.mark.asyncio
    async def test_a_reload_that_drops_the_alias_stops_its_child_there_and_then(
        self, tmp_path: Path
    ):
        from dpc_client_core.llm_manager import LLMManager

        config = tmp_path / "providers.json"
        config.write_text(json.dumps({"default_provider": "", "providers": []}), encoding="utf-8")
        manager = LLMManager(config_path=config)

        orphan = FakeSupervisor("llama.cpp-abl")
        lsp._ACTIVE_SUPERVISORS["llama.cpp-abl"] = orphan

        manager.save_config({"default_provider": "", "providers": []})
        await asyncio.sleep(0)

        assert orphan.stopped is True
        assert "llama.cpp-abl" not in lsp._ACTIVE_SUPERVISORS
