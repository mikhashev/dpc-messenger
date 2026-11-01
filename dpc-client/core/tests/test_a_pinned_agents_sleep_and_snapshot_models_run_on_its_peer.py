"""A pinned agent's sleep, snapshot and compaction models run where its dialog put them.

The agent dialog offers the pinned peer's models for the sleep and snapshot
summarising keys (94d6d227) and for compaction, and Mike's call of 2026-11-01 is
that those values are meant to be there. Sleep and the snapshot summariser asked
this node's LLMManager for the alias anyway, where it does not exist: sleep raised
"Provider '<alias>' is not configured", the summariser fell back to truncation.

Now an alias this node does not have, on an agent with a `compute_host`, goes to
that peer by the route the agent's main calls take (`DpcLlmAdapter._chat`): the
user's context in the prompt re-cut to the peer's node rules, one usage row with
route peer. A local alias stays local, an unpinned agent is as before, and a peer
that does not answer fails sleep with the peer and the alias named, never by
asking this node's default model instead. Fakes stop at the wire call.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dpc_protocol.pcm_core import PersonalContext, Profile

from dpc_client_core.dpc_agent import sleep_pipeline
from dpc_client_core.dpc_agent.context import compact_tool_history_llm
from dpc_client_core.dpc_agent.llm_adapter import _llm_manager_for_agent
from dpc_client_core.dpc_agent.sleep_pipeline import run_sleep
from dpc_client_core.dpc_agent.tools import browser
from dpc_client_core.dpc_agent.tools.core import get_dpc_context
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.service import CoreService

PEER = "dpc-node-bob-allowed-profile"
PEER_ALIAS = "llamacpp_qwen38_27b"
LOCAL = "local_alias"
AGENT = "agent_muse_test"
REPLY = json.dumps({"summary": "s", "morning_brief": {"summary": "brief"}, "sleep_findings": {}})


class _Service:
    """What the peer route and the context re-cut read off CoreService."""

    _context_for_compute_peer = CoreService._context_for_compute_peer

    def __init__(self, firewall=None, context=None, down=False):
        self.firewall = firewall
        self.device_context = None
        self.p2p_manager = SimpleNamespace(local_context=context, peers={})
        self.down = down
        self.sent = []

    async def _request_inference_from_peer(self, peer_id, prompt, **kwargs):
        if self.down:
            raise ConnectionError(f"Peer {peer_id} is not connected")
        self.sent.append({"peer_id": peer_id, "prompt": prompt, **kwargs})
        return {"response": REPLY, "model": "Qwen3.8-27B"}


class _Manager:
    """This node's LLMManager: one local alias, and the dpc_agent row the peer
    route reads its service and timeout from."""

    def __init__(self, service):
        self.providers = {
            "dpc_agent": SimpleNamespace(peer_id=None, remote_model=None, remote_provider="x",
                                         timeout=5, _service=service),
            LOCAL: SimpleNamespace(model="local-model"),
        }
        self.default_provider = LOCAL
        self.token_count_manager = None
        self.local_calls = []

    async def query(self, prompt, provider_alias=None, **kwargs):
        self.local_calls.append(provider_alias)
        return REPLY

    def count_tokens(self, text, model):
        return len(text) // 4

    def get_context_window(self, model):
        return 32000


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    sleep_pipeline._SLEEP_PIPELINE_LOCK = asyncio.Lock()
    monkeypatch.setattr(sleep_pipeline, "_export_graph_snapshot", lambda *a, **k: None)
    from dpc_client_core.dpc_agent import knowledge_graph
    monkeypatch.setattr(knowledge_graph.KnowledgeGraph, "extract_entities_gliner",
                        lambda self, texts: [], raising=False)


def _conversation(tmp_path: Path) -> Path:
    conv = tmp_path / "conversations" / AGENT
    (conv / "archive").mkdir(parents=True)
    (conv / "digest.jsonl").write_text(json.dumps(
        {"archive_file": "a1.json", "date": "2026-10-30T10:00:00", "message_count": 2}) + "\n",
        encoding="utf-8")
    (conv / "archive" / "a1.json").write_text(json.dumps({"messages": [
        {"role": "user", "content": "plan the trip"},
        {"role": "assistant", "content": "done"},
    ]}), encoding="utf-8")
    return conv


def _rows(tmp_path):
    return list(NodeLedger(tmp_path / "ledger").rows())


# --- sleep ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_pinned_agents_peer_sleep_model_is_asked_of_its_peer(tmp_path):
    service = _Service()
    manager = _Manager(service)

    result = await run_sleep(_conversation(tmp_path), _llm_manager_for_agent(manager, PEER, AGENT),
                             provider_alias=PEER_ALIAS)

    assert result["status"] == "completed", result
    assert manager.local_calls == []
    assert [(s["peer_id"], s["provider"]) for s in service.sent] == [(PEER, PEER_ALIAS)] * 2
    rows = _rows(tmp_path)
    assert [(r["route"], r["alias"], r["served_by"], r["caller"]) for r in rows] == \
        [("peer", PEER_ALIAS, PEER, AGENT)] * 2


@pytest.mark.asyncio
async def test_a_pinned_agents_local_sleep_model_stays_local(tmp_path):
    service = _Service()
    manager = _Manager(service)

    result = await run_sleep(_conversation(tmp_path), _llm_manager_for_agent(manager, PEER, AGENT),
                             provider_alias=LOCAL)

    assert result["status"] == "completed", result
    assert manager.local_calls == [LOCAL, LOCAL]
    assert service.sent == []


@pytest.mark.asyncio
async def test_an_unpinned_agents_sleep_asks_this_node_as_before(tmp_path):
    manager = _Manager(_Service())
    assert _llm_manager_for_agent(manager, None, AGENT) is manager
    assert _llm_manager_for_agent(manager, "", AGENT) is manager

    await run_sleep(_conversation(tmp_path), _llm_manager_for_agent(manager, None, AGENT),
                    provider_alias=PEER_ALIAS)

    assert manager.local_calls == [PEER_ALIAS, PEER_ALIAS]


@pytest.mark.asyncio
async def test_an_unreachable_peer_fails_sleep_naming_the_peer_and_the_alias(tmp_path):
    service = _Service(down=True)
    manager = _Manager(service)
    conv = _conversation(tmp_path)

    result = await run_sleep(conv, _llm_manager_for_agent(manager, PEER, AGENT),
                             provider_alias=PEER_ALIAS)

    assert result["status"] == "error"
    assert PEER in result["error"] and PEER_ALIAS in result["error"]
    assert manager.local_calls == [], "no model of this node may stand in for the peer's"
    assert not (conv / sleep_pipeline.MORNING_BRIEF_FILE).exists()
    assert _rows(tmp_path) == []


# --- the two sleep entry points hand run_sleep the agent's route --------------

class _Api:
    async def broadcast_event(self, name, payload):
        pass


def _agent_config(tmp_path, **config):
    agent_dir = tmp_path / ".dpc" / "agents" / AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "config.json").write_text(json.dumps({"name": "Muse", **config}), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [True, False])
async def test_toggle_sleep_hands_run_sleep_the_agents_route(tmp_path, monkeypatch, pinned):
    service = _Service()
    manager = _Manager(service)
    (tmp_path / ".dpc" / "conversations" / AGENT).mkdir(parents=True)
    _agent_config(tmp_path, sleep_provider_alias=PEER_ALIAS,
                  **({"compute_host": PEER} if pinned else {}))
    svc = CoreService.__new__(CoreService)
    svc.local_api = _Api()
    svc.llm_manager = manager
    fake = AsyncMock(return_value={"status": "no_new_sessions"})
    monkeypatch.setattr(sleep_pipeline, "run_sleep", fake)

    await svc.toggle_sleep(AGENT)
    for _ in range(5):
        await asyncio.sleep(0)

    handed = fake.call_args.args[1]
    assert fake.call_args.kwargs["provider_alias"] == PEER_ALIAS
    if not pinned:
        assert handed is manager
        return
    await handed.query("digest", provider_alias=PEER_ALIAS)
    assert manager.local_calls == []
    assert [s["peer_id"] for s in service.sent] == [PEER]


@pytest.mark.asyncio
async def test_group_sleep_hands_run_sleep_the_agents_route(tmp_path, monkeypatch):
    from dpc_client_core import service as service_module

    node = "dpc-node-" + "a" * 32
    group = "group-work"
    group_dir = tmp_path / ".dpc" / "conversations" / group
    group_dir.mkdir(parents=True)
    (group_dir / "metadata.json").write_text(json.dumps(
        {"agents": {node: [AGENT]}, "agent_names": {}}), encoding="utf-8")
    _agent_config(tmp_path, sleep_provider_alias=PEER_ALIAS, compute_host=PEER)
    service = _Service()
    manager = _Manager(service)
    svc = CoreService.__new__(CoreService)
    svc.p2p_manager = SimpleNamespace(node_id=node)
    svc.local_api = _Api()
    svc.llm_manager = manager
    svc._delete_group_briefs = AsyncMock(return_value=0)
    fake = AsyncMock(return_value={"status": "no_new_sessions"})
    monkeypatch.setattr(sleep_pipeline, "run_sleep", fake)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(service_module.asyncio, "sleep", AsyncMock())

    await svc.trigger_group_sleep(group)
    for _ in range(5):
        await real_sleep(0)

    await fake.call_args.args[1].query("digest", provider_alias=PEER_ALIAS)
    assert manager.local_calls == []
    assert [s["peer_id"] for s in service.sent] == [PEER]


# --- snapshot summarising -------------------------------------------------------

SNAPSHOT = "heading: Flights to Lisbon\n" * 40


async def _summarise(monkeypatch, manager, alias, host):
    monkeypatch.setattr(browser, "_load_agent_summarize_config", lambda aid: (alias, 100, host))
    ctx = SimpleNamespace(dpc_service=SimpleNamespace(llm_manager=manager))
    return await browser._maybe_summarize_snapshot(SNAPSHOT, ctx, AGENT)


@pytest.mark.asyncio
async def test_a_pinned_agents_peer_snapshot_model_is_asked_of_its_peer(monkeypatch):
    service = _Service()
    manager = _Manager(service)

    out = await _summarise(monkeypatch, manager, PEER_ALIAS, PEER)

    assert out.startswith(f"[snapshot summarised by {PEER_ALIAS}")
    assert manager.local_calls == []
    assert [(s["peer_id"], s["provider"]) for s in service.sent] == [(PEER, PEER_ALIAS)]


@pytest.mark.asyncio
async def test_a_pinned_agents_local_snapshot_model_stays_local(monkeypatch):
    service = _Service()
    manager = _Manager(service)

    await _summarise(monkeypatch, manager, LOCAL, PEER)

    assert manager.local_calls == [LOCAL]
    assert service.sent == []


@pytest.mark.asyncio
async def test_an_unpinned_agents_snapshot_model_is_asked_of_this_node(monkeypatch):
    service = _Service()
    manager = _Manager(service)

    await _summarise(monkeypatch, manager, PEER_ALIAS, None)

    assert manager.local_calls == [PEER_ALIAS]
    assert service.sent == []


@pytest.mark.asyncio
async def test_an_unreachable_peer_leaves_the_snapshot_truncated(monkeypatch):
    manager = _Manager(_Service(down=True))

    out = await _summarise(monkeypatch, manager, PEER_ALIAS, PEER)

    assert not out.startswith("[snapshot summarised")
    assert len(out) < len(SNAPSHOT)
    assert manager.local_calls == []


# --- the user's context in a one-off prompt is re-cut for the peer -------------

@pytest.fixture
def personal_result(tmp_path, monkeypatch):
    context = PersonalContext(
        profile=Profile(name="Alice Visible", description="shown to Bob " * 200, core_values=[]),
        metadata={"note": "PRIVATE-METADATA-MARKER"},
    )
    home = tmp_path / "dpc"
    home.mkdir()
    (home / "personal.json").write_text(json.dumps(asdict(context)), encoding="utf-8")
    monkeypatch.setenv("DPC_HOME", str(home))
    result = get_dpc_context(ToolContext(agent_root=tmp_path / "agent"), "personal")
    assert "PRIVATE-METADATA-MARKER" in result and len(result) > 2000
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"nodes": {PEER: {"personal.json:profile": "allow"}}}))
    return result, _Service(ContextFirewall(rules), context)


@pytest.mark.asyncio
async def test_a_one_off_prompt_reaches_the_peer_with_only_what_its_rules_allow(personal_result):
    result, service = personal_result
    manager = _Manager(service)

    await _llm_manager_for_agent(manager, PEER, AGENT).query(
        "Summarise this:\n" + result, provider_alias=PEER_ALIAS)

    sent = service.sent[0]["prompt"]
    assert "Summarise this:" in sent and "Alice Visible" in sent
    assert "PRIVATE-METADATA-MARKER" not in sent


@pytest.mark.asyncio
async def test_compaction_of_a_context_result_reaches_the_peer_re_cut(personal_result):
    result, service = personal_result
    manager = _Manager(service)
    messages = [{"role": "user", "content": "who am I?"}]
    for n in range(2):
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{n}", "type": "function",
                 "function": {"name": "get_dpc_context", "arguments": '{"context_type": "personal"}'}}]},
            {"role": "tool", "tool_call_id": f"c{n}", "content": result},
        ]

    await compact_tool_history_llm(messages, _llm_manager_for_agent(manager, PEER, AGENT),
                                   PEER_ALIAS, keep_recent=1)

    assert len(service.sent) == 1 and manager.local_calls == []
    assert "Alice Visible" in service.sent[0]["prompt"]
    assert "PRIVATE-METADATA-MARKER" not in service.sent[0]["prompt"]


# --- compaction: the loop hands it the agent's route ---------------------------

class _Tools:
    _ctx = None

    def schemas(self, core_only=False, include_restricted=False):
        return []

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        return "ok"


class _AgentLlm:
    """The agent's adapter as the loop reads it: its manager, its pin, one answer."""

    def __init__(self, manager, compute_host):
        self._llm_manager = manager
        self._compute_host = compute_host
        self._caller = AGENT

    async def chat(self, messages, **kwargs):
        return {"role": "assistant", "content": "answer", "tool_calls": []}, {
            "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [True, False])
async def test_the_loop_hands_compaction_the_agents_route(tmp_path, monkeypatch, pinned):
    from dpc_client_core.dpc_agent import loop

    service = _Service()
    manager = _Manager(service)
    handed = []

    async def _capture(messages, **kwargs):
        handed.append(kwargs["llm_manager"])
        return messages

    monkeypatch.setattr(loop, "load_agent_config", lambda _n: {})
    monkeypatch.setattr(loop, "apply_compaction", _capture)

    await loop.run_llm_loop(
        messages=[{"role": "user", "content": "hi"}], tools=_Tools(),
        llm=_AgentLlm(manager, PEER if pinned else ""), agent_root=tmp_path / "agent",
        emit_progress=lambda *a, **k: None, task_id="t1", kill_event=asyncio.Event(),
    )

    if not pinned:
        assert handed[0] is manager
        return
    await handed[0].query("tool result", provider_alias=PEER_ALIAS)
    assert manager.local_calls == []
    assert [s["peer_id"] for s in service.sent] == [PEER]
