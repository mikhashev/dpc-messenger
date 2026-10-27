"""GitHub Models was retired on 2026-07-30, inference API included.

The type stays registered for one release so an existing `providers.json`
alias does not take the backend down: it loads, the other aliases load, and
each call says what to do. The editor no longer offers the type to anyone
adding a provider, and the shipped example no longer shows it.
"""

import json
from pathlib import Path

import pytest

from dpc_client_core.llm_manager import PROVIDER_MAP, LLMManager
from dpc_client_core.providers.github_models_provider import GitHubModelsProvider

REPO = Path(__file__).resolve().parents[3]
EDITOR = REPO / "dpc-client" / "ui" / "src" / "lib" / "components" / "ProvidersEditor.svelte"
EXAMPLE = REPO / "dpc-client" / "providers.example.json"


def _manager(tmp_path: Path, providers) -> LLMManager:
    config = tmp_path / "providers.json"
    config.write_text(json.dumps({"default_provider": "", "providers": providers}), encoding="utf-8")
    return LLMManager(config_path=config)


def test_an_old_alias_loads_beside_the_others(tmp_path, monkeypatch):
    # The old class needed the azure SDK and a token at construction and raised
    # RuntimeError without them, which aborted the loading of every later alias.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    manager = _manager(tmp_path, [
        {"alias": "old_gh", "type": "github_models", "model": "gpt-4o", "api_key_env": "GITHUB_TOKEN"},
        {"alias": "after", "type": "dpc_agent"},
    ])
    assert isinstance(manager.providers["old_gh"], GitHubModelsProvider)
    assert "after" in manager.providers
    assert manager.providers["old_gh"].supports_vision() is False


@pytest.mark.asyncio
async def test_each_call_of_an_old_alias_names_the_retirement_and_the_remedy(tmp_path):
    provider = _manager(tmp_path, [{"alias": "old_gh", "type": "github_models", "model": "gpt-4o"}]).providers["old_gh"]

    async def on_chunk(text, conversation_id):  # pragma: no cover - must never run
        raise AssertionError("a retired provider streamed text")

    for call in (provider.generate_response("hi"), provider.generate_response_stream("hi", on_chunk)):
        with pytest.raises(RuntimeError) as err:
            await call
        message = str(err.value)
        assert "retired" in message and "2026-07-30" in message
        assert "old_gh" in message and "Remove this alias" in message
    await provider.close()


def test_the_type_is_still_registered_for_one_release():
    assert PROVIDER_MAP["github_models"] is GitHubModelsProvider


def test_the_editor_offers_the_type_to_nobody_adding_a_provider():
    source = EDITOR.read_text(encoding="utf-8")
    lines = source.splitlines()
    options = [i for i, line in enumerate(lines) if '<option value="github_models"' in line]
    # The one that remains belongs to an alias that already has the type, so the
    # select shows what it holds instead of a blank; it cannot be chosen.
    assert len(options) == 1
    i = options[0]
    assert "disabled" in lines[i]
    assert "(editedConfig.providers[i].type as string) === 'github_models'" in lines[i - 1]
    assert "newProvider.type === 'github_models'" not in source
    union = next(line for line in lines if line.lstrip().startswith("type ProviderType"))
    assert "github_models" not in union


def test_the_shipped_example_does_not_show_the_type():
    assert "github_models" not in EXAMPLE.read_text(encoding="utf-8")
    json.loads(EXAMPLE.read_text(encoding="utf-8"))
