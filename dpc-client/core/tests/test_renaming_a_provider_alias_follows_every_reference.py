"""Renaming a provider alias must reach every persisted place that names it, not only providers.json."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dpc_client_core import provider_alias_refs
from dpc_client_core.service import CoreService


OLD = "llama.cpp-abl"
NEW = "qwen3.8 27b Mythos"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    _write(tmp_path / "providers.json", {
        "default_provider": "deepseek_flash",
        "vision_provider": NEW,
        "voice_provider": "whisper-large-v3-turbo",
        "agent_provider": "deepseek_flash",
        "providers": [
            {"alias": "deepseek_flash", "type": "deepseek", "model": "deepseek-v4-flash"},
            {"alias": "whisper-large-v3-turbo", "type": "local_whisper", "model": "whisper"},
            {"alias": NEW, "type": "llamacpp_server", "model": "qwen3.8 27b Mythos"},
        ],
    })

    _write(tmp_path / "agents" / "agent_johnny" / "config.json", {
        "agent_id": "agent_johnny",
        "name": "Johnny",
        "provider_alias": OLD,
        "sleep_provider_alias": OLD,
        "snapshot_summarize_provider": OLD,
        "compaction_provider": OLD,
        "budget_usd": 50,
    })
    _write(tmp_path / "agents" / "agent_ark" / "config.json", {
        "agent_id": "agent_ark",
        "name": "Ark",
        "provider_alias": "deepseek_flash",
        "sleep_provider_alias": "deepseek_flash",
    })
    _write(tmp_path / "agents" / "_registry.json", {
        "version": 1,
        "agents": {
            "agent_johnny": {"agent_id": "agent_johnny", "name": "Johnny", "provider_alias": OLD},
            "agent_ark": {"agent_id": "agent_ark", "name": "Ark", "provider_alias": "deepseek_flash"},
        },
    })
    _write(tmp_path / "privacy_rules.json", {
        "compute": {"enabled": True, "serving_alias": OLD, "allowed_models": []},
    })
    (tmp_path / "config.ini").write_text(
        "[api]\n"
        "port = 9999\n"
        "\n"
        "# the order transcription is attempted in\n"
        "[voice_transcription]\n"
        "provider_priority = whisper-large-v3-turbo," + OLD + ",openai\n"
        "enabled = true\n",
        encoding="utf-8",
    )
    return tmp_path


def _agent(home: Path, agent_id: str) -> dict:
    return json.loads((home / "agents" / agent_id / "config.json").read_text(encoding="utf-8"))


class TestTheRenameReachesEveryReference:
    def test_all_four_fields_of_the_agent_that_named_it_follow(self, home: Path):
        provider_alias_refs.rename_references(OLD, NEW, home)

        johnny = _agent(home, "agent_johnny")
        assert johnny["provider_alias"] == NEW
        assert johnny["sleep_provider_alias"] == NEW
        assert johnny["snapshot_summarize_provider"] == NEW
        assert johnny["compaction_provider"] == NEW

    def test_the_count_and_the_agent_it_touched_are_reported(self, home: Path):
        counts = provider_alias_refs.rename_references(OLD, NEW, home)

        assert counts["agent_configs"] == 4
        assert counts["registry"] == 1
        assert counts["firewall"] == 1
        assert counts["voice_priority"] == 1
        assert counts["agent_ids"] == ["agent_johnny"]

    def test_an_agent_on_another_provider_is_left_alone(self, home: Path):
        before = _agent(home, "agent_ark")

        provider_alias_refs.rename_references(OLD, NEW, home)

        assert _agent(home, "agent_ark") == before

    def test_the_registry_entry_follows_the_agent_config(self, home: Path):
        provider_alias_refs.rename_references(OLD, NEW, home)

        registry = json.loads((home / "agents" / "_registry.json").read_text(encoding="utf-8"))
        assert registry["agents"]["agent_johnny"]["provider_alias"] == NEW
        assert registry["agents"]["agent_ark"]["provider_alias"] == "deepseek_flash"

    def test_the_firewalls_serving_alias_follows(self, home: Path):
        provider_alias_refs.rename_references(OLD, NEW, home)

        rules = json.loads((home / "privacy_rules.json").read_text(encoding="utf-8"))
        assert rules["compute"]["serving_alias"] == NEW

    def test_the_voice_priority_follows_and_the_rest_of_the_file_survives(self, home: Path):
        provider_alias_refs.rename_references(OLD, NEW, home)

        text = (home / "config.ini").read_text(encoding="utf-8")
        assert f"whisper-large-v3-turbo,{NEW},openai" in text
        assert OLD not in text
        assert "# the order transcription is attempted in" in text
        assert "port = 9999" in text
        assert "enabled = true" in text

    def test_renaming_to_the_same_name_changes_nothing(self, home: Path):
        before = (home / "agents" / "agent_johnny" / "config.json").read_text(encoding="utf-8")

        counts = provider_alias_refs.rename_references(OLD, OLD, home)

        assert counts["agent_configs"] == 0
        assert (home / "agents" / "agent_johnny" / "config.json").read_text(encoding="utf-8") == before


class TestFindingWhoNamesAnAlias:
    def test_every_place_is_named_with_its_field(self, home: Path):
        found = provider_alias_refs.find_references(OLD, home)

        assert "agents/agent_johnny/config.json:provider_alias" in found
        assert "agents/agent_johnny/config.json:sleep_provider_alias" in found
        assert "agents/agent_johnny/config.json:snapshot_summarize_provider" in found
        assert "agents/agent_johnny/config.json:compaction_provider" in found
        assert "agents/_registry.json:agent_johnny.provider_alias" in found
        assert "privacy_rules.json:compute.serving_alias" in found
        assert "config.ini:[voice_transcription]provider_priority" in found

    def test_a_role_field_of_providers_json_is_named_too(self, home: Path):
        assert "providers.json:vision_provider" in provider_alias_refs.find_references(NEW, home)

    def test_an_alias_nobody_names_has_no_references(self, home: Path):
        assert provider_alias_refs.find_references("nobody-names-me", home) == []


class TestReportingWhatNoLongerResolves:
    def test_an_agent_cut_off_from_its_provider_is_reported_with_the_dead_name(self, home: Path):
        known = ["deepseek_flash", "whisper-large-v3-turbo", NEW]

        dangling = provider_alias_refs.unresolved_references(known, home)

        assert any("agent_johnny" in line and OLD in line for line in dangling)

    def test_nothing_is_reported_once_the_rename_has_been_followed(self, home: Path):
        provider_alias_refs.rename_references(OLD, NEW, home)

        known = ["deepseek_flash", "whisper-large-v3-turbo", NEW]
        assert provider_alias_refs.unresolved_references(known, home) == []


class TestTheServiceOnlyFollowsARealRename:
    def _service(self, home: Path):
        return SimpleNamespace(
            llm_manager=SimpleNamespace(config_path=home / "providers.json"),
            firewall=SimpleNamespace(reload=lambda: (True, "ok")),
            agent_service=None,
        )

    def _config(self, aliases):
        return {"providers": [{"alias": a} for a in aliases]}

    @pytest.mark.asyncio
    async def test_a_rename_named_by_the_form_is_followed(self, home: Path):
        service = self._service(home)
        config = self._config(["deepseek_flash", NEW])

        summary = await CoreService._follow_alias_renames(service, config, {OLD: NEW})

        assert summary and OLD in summary[0] and NEW in summary[0]
        assert _agent(home, "agent_johnny")["provider_alias"] == NEW

    @pytest.mark.asyncio
    async def test_an_alias_that_is_still_present_is_not_a_rename(self, home: Path):
        service = self._service(home)
        config = self._config(["deepseek_flash", OLD, NEW])

        summary = await CoreService._follow_alias_renames(service, config, {OLD: NEW})

        assert summary == []
        assert _agent(home, "agent_johnny")["provider_alias"] == OLD

    @pytest.mark.asyncio
    async def test_a_target_that_does_not_exist_is_refused(self, home: Path):
        service = self._service(home)
        config = self._config(["deepseek_flash", NEW])

        summary = await CoreService._follow_alias_renames(service, config, {OLD: "typo-never-created"})

        assert summary == []
        assert _agent(home, "agent_johnny")["provider_alias"] == OLD

    @pytest.mark.asyncio
    async def test_no_renames_at_all_is_a_no_op(self, home: Path):
        service = self._service(home)

        assert await CoreService._follow_alias_renames(service, self._config([NEW]), None) == []
        assert await CoreService._follow_alias_renames(service, self._config([NEW]), {}) == []
