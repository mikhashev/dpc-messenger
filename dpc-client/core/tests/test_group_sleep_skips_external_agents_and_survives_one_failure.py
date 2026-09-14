"""Group Sleep must not run external participants, and one agent's failure must
not take the others down with it.

Mike's call, DPC Project group, 2026-09-14: external agents (`ext:` tags — a
Claude Code bridge or anything else answering over the local API, see
`EXTERNAL_AGENT_PREFIX` in `service.py`) do not use the Sleep mechanism and
must be excluded.

Live finding by CC_linux the same day: the Linux node's roster for group
`work` was `['agent_ubu_acbf15fb', 'ext:CC_linux', 'ext:Zcode']`. The old loop
in `trigger_group_sleep` called `load_agent_config(agent_id)` on
`ext:CC_linux` before checking anything, which reaches `get_agent_root` and
raises `ValueError("«ext:CC_linux» is not an agent id")` — uncaught, so the
whole command failed and the agent listed after the external one never ran.
Before the raise, `agent_dir.mkdir(parents=True, exist_ok=True)` had already
created a directory for the tag.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dpc_client_core import service as service_module
from dpc_client_core.dpc_agent import sleep_pipeline
from dpc_client_core.dpc_agent import utils as agent_utils
from dpc_client_core.service import EXTERNAL_AGENT_PREFIX, CoreService

NODE = "dpc-node-" + "a" * 32
GROUP = "group-work"


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _P2P:
    def __init__(self, node_id):
        self.node_id = node_id


def _make_service(tmp_path, monkeypatch, agents):
    """A CoreService stand-in wired only for trigger_group_sleep."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    group_dir = tmp_path / ".dpc" / "conversations" / GROUP
    group_dir.mkdir(parents=True)
    metadata = {"agents": {NODE: agents}, "agent_names": {}}
    (group_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    svc = CoreService.__new__(CoreService)
    svc.p2p_manager = _P2P(NODE)
    svc.local_api = _Api()
    svc.llm_manager = object()
    svc._delete_group_briefs = AsyncMock(return_value=0)

    # The sleep pipeline itself is not under test here — make it instant so the
    # loop's `await asyncio.sleep(2)` pacing doesn't slow the test, and patch
    # that pacing out too.
    monkeypatch.setattr(sleep_pipeline, "run_sleep",
                         AsyncMock(return_value={"status": "no_new_sessions"}))
    monkeypatch.setattr(service_module.asyncio, "sleep", AsyncMock())
    return svc, group_dir


@pytest.mark.asyncio
async def test_external_agents_are_skipped_before_any_load_or_mkdir(tmp_path, monkeypatch, caplog):
    svc, _ = _make_service(
        tmp_path, monkeypatch,
        agents=["agent_a", f"{EXTERNAL_AGENT_PREFIX}CC_linux", "agent_b"],
    )

    with caplog.at_level("INFO"):
        result = await svc.trigger_group_sleep(GROUP)

    assert result["status"] == "sleeping"
    assert result["agents"] == ["agent_a", "agent_b"]
    assert result["skipped_external"] == [f"{EXTERNAL_AGENT_PREFIX}CC_linux"]
    assert "failures" not in result

    ext_dir = tmp_path / ".dpc" / "conversations" / f"{EXTERNAL_AGENT_PREFIX}CC_linux"
    assert not ext_dir.exists(), "no directory may be created for a non-agent id"

    skip_lines = [r.message for r in caplog.records if "skipping external" in r.message]
    assert any("CC_linux" in line for line in skip_lines)
    found_lines = [r.message for r in caplog.records if r.message.startswith("Group sleep: found")]
    assert any("found 2 agents" in line for line in found_lines), found_lines


@pytest.mark.asyncio
async def test_a_failing_agent_does_not_abort_the_others_and_the_failure_is_reported(
    tmp_path, monkeypatch, caplog
):
    svc, _ = _make_service(tmp_path, monkeypatch, agents=["agent_a", "agent_b"])

    real_load = agent_utils.load_agent_config

    def _boom_for_agent_a(agent_id):
        if agent_id == "agent_a":
            raise ValueError("boom")
        return real_load(agent_id)

    monkeypatch.setattr(agent_utils, "load_agent_config", _boom_for_agent_a)

    with caplog.at_level("ERROR"):
        result = await svc.trigger_group_sleep(GROUP)

    assert result["status"] == "sleeping"
    assert result["agents"] == ["agent_b"], "agent_b must still run"
    assert result["failures"] == {"agent_a": "boom"}

    error_lines = [r.message for r in caplog.records if "failed to start agent_a" in r.message]
    assert error_lines, "the failing agent's id must be named in the log"


@pytest.mark.asyncio
async def test_all_external_group_is_an_error_not_a_crash(tmp_path, monkeypatch):
    svc, _ = _make_service(
        tmp_path, monkeypatch, agents=[f"{EXTERNAL_AGENT_PREFIX}CC_linux"]
    )

    result = await svc.trigger_group_sleep(GROUP)

    assert result["status"] == "error"
    assert result["skipped_external"] == [f"{EXTERNAL_AGENT_PREFIX}CC_linux"]
