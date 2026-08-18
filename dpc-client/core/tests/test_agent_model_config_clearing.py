"""Choosing «Default (global)» has to be a choice, not a no-op.

The Agent Models Configuration dialog offers `<option value="">Default
(global)</option>` for the sleep, snapshot-summarisation and compaction models.
Selecting it produced an empty string, which the save handler converted to
`null`, which this layer reads as «the caller did not mention this field» — so
the previous model stayed and the dialog reopened showing it. Measured on
2026-08-18 from the payload the UI actually sent:

    save_agent_model_config {'agent_id': 'agent_001',
        'provider_alias': 'deepseek_flash',
        'sleep_provider_alias': None, 'snapshot_summarize_provider': None,
        'compaction_provider': None, ...}

and `agent_001/config.json` kept `qwen3.8:latest` in exactly those three
fields while `provider_alias` changed — which is what «settings don't save»
looked like from the outside.

These pin the two meanings apart: **None keeps, empty string clears.** The UI
sends the empty string; anything that later folds the two together brings the
defect back.
"""

from __future__ import annotations

import json

import pytest

from dpc_client_core.agent_service import AgentService


class _Registry:
    """The real one reads ~/.dpc; this stands in for its two calls."""

    def __init__(self, agent_id):
        self._id = agent_id
        self.updates = []

    def get_agent(self, agent_id):
        return {"agent_id": agent_id} if agent_id == self._id else None

    def update_agent(self, agent_id, patch):
        self.updates.append((agent_id, patch))


@pytest.fixture
def saver(tmp_path, monkeypatch):
    """AgentService.save_agent_model_config against a config file on disk."""
    from dpc_client_core.dpc_agent import utils as agent_utils
    from dpc_client_core import agent_service as svc_mod

    agent_id = "agent_test"
    cfg_path = tmp_path / agent_id / "config.json"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(json.dumps({
        "provider_alias": "qwen3.8:latest",
        "sleep_provider_alias": "qwen3.8:latest",
        "snapshot_summarize_provider": "qwen3.8:latest",
        "compaction_provider": "qwen3.8:latest",
    }), encoding="utf-8")

    monkeypatch.setattr(agent_utils, "get_agent_config_path",
                        lambda aid: tmp_path / aid / "config.json")
    monkeypatch.setattr(agent_utils, "AgentRegistry", lambda *a, **k: _Registry(agent_id))

    service = AgentService.__new__(AgentService)
    service.peer_metadata = {}

    async def _no_refresh(_aid, _config):
        return None

    monkeypatch.setattr(service, "_refresh_live_agent_manager", _no_refresh, raising=False)

    async def _save(**kwargs):
        return await AgentService.save_agent_model_config(service, agent_id, **kwargs)

    def _read():
        return json.loads(cfg_path.read_text(encoding="utf-8"))

    return _save, _read


@pytest.mark.asyncio
async def test_an_empty_choice_clears_the_field(saver):
    """«Default (global)» is `value=""` in the dialog. It has to land."""
    save, read = saver

    result = await save(sleep_provider_alias="",
                        snapshot_summarize_provider="",
                        compaction_provider="")

    assert result["status"] == "ok"
    cfg = read()
    assert cfg["sleep_provider_alias"] == ""
    assert cfg["snapshot_summarize_provider"] == ""
    assert cfg["compaction_provider"] == ""


@pytest.mark.asyncio
async def test_an_omitted_field_is_left_alone(saver):
    """The other half of the contract: a caller that mentions only the main
    model must not wipe the rest."""
    save, read = saver

    await save(provider_alias="deepseek_flash")

    cfg = read()
    assert cfg["provider_alias"] == "deepseek_flash"
    assert cfg["sleep_provider_alias"] == "qwen3.8:latest"
    assert cfg["snapshot_summarize_provider"] == "qwen3.8:latest"
    assert cfg["compaction_provider"] == "qwen3.8:latest"


@pytest.mark.asyncio
async def test_a_cleared_field_reads_as_no_choice_downstream(saver):
    """Every consumer resolves this setting with `or None`, so the empty
    string means «use the default» rather than «use a provider named ''»."""
    save, read = saver

    await save(sleep_provider_alias="")

    cfg = read()
    assert (cfg.get("sleep_provider_alias") or None) is None
