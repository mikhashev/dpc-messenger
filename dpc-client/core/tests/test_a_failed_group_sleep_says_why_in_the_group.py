"""A group sleep that fails says so in the group, from the agent, with the reason.

On 2026-11-06 Muse's sleep, pinned to a peer model whose peer was not connected,
and Ark's, on a synthesis answer that was not JSON, both failed with a log line
and nothing in the chat. Mike's call that day: the group gets the reason, posted
by the path a morning brief takes. A completed sleep posts its brief as before.
Fakes stop at the wire call and at `send_group_agent_message`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent import sleep_pipeline
from dpc_client_core.dpc_agent.llm_adapter import _llm_manager_for_agent
from dpc_client_core.service import CoreService

NODE = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
PEER_NAME = "Warren"
PEER_ALIAS = "llamacpp_qwen38_27b"
LOCAL = "local_alias"
AGENT = "agent_muse_test"
AGENT_NAME = "Muse"
GROUP = "group-studio"
REPLY = json.dumps({"summary": "s", "morning_brief": {"summary": "brief"}, "sleep_findings": {}})


class _Service:
    """What the peer route reads off CoreService."""

    def __init__(self, down=False):
        self.firewall = None
        self.device_context = None
        peer_cache = SimpleNamespace(get_peer=lambda pid: SimpleNamespace(display_name=PEER_NAME))
        self.p2p_manager = SimpleNamespace(local_context=None, peers={}, peer_cache=peer_cache)
        self.down = down

    async def _request_inference_from_peer(self, peer_id, prompt, **kwargs):
        if self.down:
            raise ConnectionError(f"Peer {peer_id} is not connected")
        return {"response": REPLY, "model": "Qwen3.8-27B"}


class _Manager:
    """This node's LLMManager; `synthesis` is what the second call answers."""

    def __init__(self, service, synthesis=REPLY):
        self.providers = {
            "dpc_agent": SimpleNamespace(peer_id=None, remote_model=None, remote_provider="x",
                                         timeout=5, _service=service),
            LOCAL: SimpleNamespace(model="local-model"),
        }
        self.default_provider = LOCAL
        self.token_count_manager = None
        self.synthesis = synthesis
        self.calls = 0

    async def query(self, prompt, provider_alias=None, **kwargs):
        self.calls += 1
        return REPLY if self.calls == 1 else self.synthesis

    def count_tokens(self, text, model):
        return len(text) // 4

    def alias_context_window(self, alias):
        return 32000


class _Api:
    async def broadcast_event(self, name, payload):
        pass


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    sleep_pipeline._SLEEP_PIPELINE_LOCK = asyncio.Lock()
    monkeypatch.setattr(sleep_pipeline, "_export_graph_snapshot", lambda *a, **k: None)
    from dpc_client_core.dpc_agent import knowledge_graph
    monkeypatch.setattr(knowledge_graph.KnowledgeGraph, "extract_entities_gliner",
                        lambda self, texts: [], raising=False)


def _setup(tmp_path, **config):
    """A group with one local agent, and that agent's 1:1 archive to sleep on."""
    conversations = tmp_path / ".dpc" / "conversations"
    group_dir = conversations / GROUP
    group_dir.mkdir(parents=True)
    (group_dir / "metadata.json").write_text(json.dumps(
        {"agents": {NODE: [AGENT]}, "agent_names": {NODE: {AGENT: AGENT_NAME}}}), encoding="utf-8")
    conv = conversations / AGENT
    (conv / "archive").mkdir(parents=True)
    (conv / "digest.jsonl").write_text(json.dumps(
        {"archive_file": "a1.json", "date": "2026-11-05T10:00:00", "message_count": 2}) + "\n",
        encoding="utf-8")
    (conv / "archive" / "a1.json").write_text(json.dumps({"messages": [
        {"role": "user", "content": "plan the trip"},
        {"role": "assistant", "content": "done"},
    ]}), encoding="utf-8")
    agent_dir = tmp_path / ".dpc" / "agents" / AGENT
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.json").write_text(json.dumps({"name": AGENT_NAME, **config}), encoding="utf-8")
    return conv


async def _group_sleep(monkeypatch, manager, run_sleep=None):
    """trigger_group_sleep with the real pipeline (group_id dropped, so it reads the
    agent's own archive), returning what was posted to the group."""
    from dpc_client_core import service as service_module

    real_run_sleep = sleep_pipeline.run_sleep

    async def _ungrouped(*args, group_id=None, **kwargs):
        return await real_run_sleep(*args, **kwargs)

    monkeypatch.setattr(sleep_pipeline, "run_sleep", run_sleep or _ungrouped)
    svc = CoreService.__new__(CoreService)
    svc.p2p_manager = SimpleNamespace(node_id=NODE)
    svc.local_api = _Api()
    svc.llm_manager = manager
    svc._delete_group_briefs = AsyncMock(return_value=0)
    svc.send_group_agent_message = AsyncMock(return_value="m" * 16)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(service_module.asyncio, "sleep", AsyncMock())

    await svc.trigger_group_sleep(GROUP)
    for _ in range(50):
        await real_sleep(0)
    return [c.args for c in svc.send_group_agent_message.call_args_list]


@pytest.mark.asyncio
async def test_a_peer_that_is_not_connected_is_named_in_the_group(tmp_path, monkeypatch):
    _setup(tmp_path, sleep_provider_alias=PEER_ALIAS, compute_host=PEER)

    posted = await _group_sleep(monkeypatch, _Manager(_Service(down=True)))

    assert posted == [(GROUP, AGENT_NAME,
                       f"**Sleep did not complete:** the peer model {PEER_ALIAS} "
                       f"on {PEER_NAME} is not connected.")]


@pytest.mark.asyncio
async def test_an_unparsable_synthesis_names_the_model_and_the_dump(tmp_path, monkeypatch):
    conv = _setup(tmp_path)

    posted = await _group_sleep(monkeypatch, _Manager(_Service(), synthesis="not json at all"))

    dumps = list((conv / "sleep_results").glob("failed_synthesis_*.txt"))
    assert len(dumps) == 1
    assert posted == [(GROUP, AGENT_NAME,
                       f"**Sleep did not complete:** the synthesis answer from {LOCAL} was not "
                       f"valid JSON; raw output saved to {dumps[0].name}.")]


@pytest.mark.asyncio
async def test_a_repairable_synthesis_posts_its_brief_and_keeps_the_raw_text(tmp_path, monkeypatch):
    conv = _setup(tmp_path)
    # Quotes inside a string and a cut at the end: mended, not failed (2026-11-06).
    broken = '{"summary": "s", "morning_brief": {"summary": "the "slow" brief"}, "sleep_findings": {"x": ["cu'

    posted = await _group_sleep(monkeypatch, _Manager(_Service(), synthesis=broken))

    assert len(posted) == 1
    assert posted[0][2].startswith("**Morning Brief (Sleep Consolidation)**")
    assert not list((conv / "sleep_results").glob("failed_*.txt"))
    assert len(list((conv / "sleep_results").glob("repaired_synthesis_*.txt"))) == 1


@pytest.mark.asyncio
async def test_a_completed_sleep_posts_its_brief_and_nothing_else(tmp_path, monkeypatch):
    conv = _setup(tmp_path)

    posted = await _group_sleep(monkeypatch, _Manager(_Service()))

    assert len(posted) == 1
    group, name, text = posted[0]
    assert (group, name) == (GROUP, AGENT_NAME)
    assert text.startswith("**Morning Brief (Sleep Consolidation)**")
    assert "Sleep did not complete" not in text
    brief = json.loads((conv / f"morning_brief_group_{GROUP}.json").read_text(encoding="utf-8"))
    assert brief["chat_message_id"] == "m" * 16 and brief["consumed"] is True


@pytest.mark.asyncio
async def test_a_skipped_sleep_posts_nothing(tmp_path, monkeypatch):
    _setup(tmp_path)

    posted = await _group_sleep(monkeypatch, _Manager(_Service()),
                                run_sleep=AsyncMock(return_value={"status": "already_sleeping"}))

    assert posted == []


@pytest.mark.asyncio
async def test_a_pipeline_that_raises_still_says_so(tmp_path, monkeypatch):
    _setup(tmp_path)

    posted = await _group_sleep(monkeypatch, _Manager(_Service()),
                                run_sleep=AsyncMock(side_effect=OSError("disk full")))

    assert posted == [(GROUP, AGENT_NAME, "**Sleep did not complete:** OSError: disk full.")]


def test_the_peer_reason_comes_from_the_exception_the_peer_route_raises():
    try:
        try:
            raise ConnectionError(f"Peer {PEER} is not connected")
        except ConnectionError as cause:
            err = RuntimeError("did not answer")
            err.host_name, err.provider_alias = PEER_NAME, PEER_ALIAS
            raise err from cause
    except RuntimeError as e:
        reason = sleep_pipeline._sleep_failure_reason(
            e, "RuntimeError: did not answer", alias=PEER_ALIAS,
            synthesis_dump=None, synthesis_unparsable=False)
    assert reason == f"the peer model {PEER_ALIAS} on {PEER_NAME} is not connected"
