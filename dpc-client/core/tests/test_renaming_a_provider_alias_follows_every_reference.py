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
        "enabled = true\n"
        "\n"
        "# what knowledge extraction falls back to when nothing is warm\n"
        "[knowledge]\n"
        "cold_fallback_provider = " + OLD + "\n",
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
        # `settings` is a spy rather than absent: the reload call sits inside a
        # try/except, so a double without it would let the test pass while the
        # reload silently did not happen — the vacuous shape this file exists to
        # avoid.
        reloads = []
        return SimpleNamespace(
            llm_manager=SimpleNamespace(config_path=home / "providers.json"),
            firewall=SimpleNamespace(reload=lambda: (True, "ok")),
            settings=SimpleNamespace(reload=lambda: reloads.append(True)),
            settings_reloads=reloads,
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


class TestTheFixtureWalksTheInventoryInsteadOfMirroringIt:
    """The property that stops this file certifying only what its author remembered.

    Both reviews of 2026-08-22 made the same point: a fixture written from the
    implementation lists exactly what the implementation lists, so a persisted key
    the implementation forgot is a key the fixture also forgets. That is how
    `[knowledge] cold_fallback_provider` stayed invisible while sixteen tests
    passed — the rename killed the knowledge cold fallback and every check agreed
    nothing was wrong.

    These two walk `PLACES` rather than a list typed here. A new `Place` whose
    shape this home does not carry fails the first; a `Place` the rename does not
    follow fails the second. Neither can be satisfied by remembering.
    """

    def _writable(self):
        return [p for p in provider_alias_refs.PLACES if p.writable]

    def test_this_home_names_the_old_alias_in_every_writable_place(self, home: Path):
        missing = [
            place.key for place in self._writable()
            if not any(value == OLD for _where, value in place.scan(home))
        ]
        assert not missing, (
            "the fixture does not exercise these places, so nothing here can notice "
            f"whether the rename reaches them: {missing}"
        )

    def test_after_the_rename_no_writable_place_still_names_the_old_alias(self, home: Path):
        provider_alias_refs.rename_references(OLD, NEW, home)

        left = [
            (place.key, value) for place in self._writable()
            for _where, value in place.scan(home) if value == OLD
        ]
        assert not left, f"the rename did not reach: {left}"

    def test_the_result_counts_every_writable_place_by_name(self, home: Path):
        counts = provider_alias_refs.rename_references(OLD, NEW, home)

        assert set(counts["by_place"]) == {p.key for p in self._writable()}, (
            "a place that renames without being counted is a place the caller's "
            "«did anything change» sum cannot see"
        )
        assert counts["by_place"]["config.ini:[knowledge]cold_fallback_provider"] == 1


class TestConfigIniIsRereadAfterTheRename:
    """The rename lands on disk and the readers keep the old value until restart.

    `Settings` is constructed once at startup, so rewriting `config.ini` moved the
    voice priority and the knowledge cold fallback on disk while every caller went
    on answering from the copy loaded at boot — for a setting whose commit message
    said no restart was needed.
    """

    def _service(self, home: Path):
        reloads = []
        return SimpleNamespace(
            llm_manager=SimpleNamespace(config_path=home / "providers.json"),
            firewall=SimpleNamespace(reload=lambda: (True, "ok")),
            settings=SimpleNamespace(reload=lambda: reloads.append(True)),
            settings_reloads=reloads,
            agent_service=None,
        )

    @pytest.mark.asyncio
    async def test_a_rename_that_moved_a_config_ini_key_rereads_the_file(self, home: Path):
        service = self._service(home)
        config = {"providers": [{"alias": a} for a in ["deepseek_flash", NEW]]}

        await CoreService._follow_alias_renames(service, config, {OLD: NEW})

        assert service.settings_reloads, (
            "config.ini was rewritten and nothing re-read it — every reader keeps "
            "the old alias until the next restart"
        )

    @pytest.mark.asyncio
    async def test_a_rename_that_touched_no_config_ini_key_does_not_reread(self, home: Path):
        """Not reloading is the ordinary case and must stay cheap: a rename that
        moved only JSON has no reason to re-read the ini."""
        (home / "config.ini").write_text("[api]\nport = 9999\n", encoding="utf-8")
        service = self._service(home)
        config = {"providers": [{"alias": a} for a in ["deepseek_flash", NEW]]}

        await CoreService._follow_alias_renames(service, config, {OLD: NEW})

        assert service.settings_reloads == []


class TestARenameThatMovedOnlyTheNewestKey:
    """The case the caller's old sum could not see.

    `touched` was the sum of four fields typed at the call site, so a rename that
    moved only a place added later counted as zero — the service took the `continue`
    branch, logged nothing and reported nothing, while the file on disk had changed.
    Summing the inventory makes a new place visible without anyone remembering to
    add a term.
    """

    @pytest.fixture
    def home_naming_it_only_in_knowledge(self, tmp_path: Path) -> Path:
        _write(tmp_path / "providers.json", {
            "default_provider": "deepseek_flash",
            "providers": [{"alias": "deepseek_flash"}, {"alias": NEW}],
        })
        (tmp_path / "config.ini").write_text(
            "[knowledge]\ncold_fallback_provider = " + OLD + "\n",
            encoding="utf-8",
        )
        return tmp_path

    @pytest.mark.asyncio
    async def test_it_is_followed_and_reported(self, home_naming_it_only_in_knowledge: Path):
        home = home_naming_it_only_in_knowledge
        reloads = []
        service = SimpleNamespace(
            llm_manager=SimpleNamespace(config_path=home / "providers.json"),
            firewall=SimpleNamespace(reload=lambda: (True, "ok")),
            settings=SimpleNamespace(reload=lambda: reloads.append(True)),
            agent_service=None,
        )
        config = {"providers": [{"alias": a} for a in ["deepseek_flash", NEW]]}

        summary = await CoreService._follow_alias_renames(service, config, {OLD: NEW})

        assert summary, "a rename nobody counted is a rename nobody can audit"
        assert NEW in (home / "config.ini").read_text(encoding="utf-8")
        assert reloads
