"""A tool marked `owner_only` is neither offered nor run outside an owner turn; ComfyUI is owner_only.

Card AGENT-TOOLS-ARE-OFFERED-IN-ANY-TURN-CLASS-SO-A-PEER-CAN-MAKE-THE-OWNERS-AGENT-RUN-COMFYUI-JOBS-
ON-THE-OWNERS-GPU; Zcode's review #51, section 3 [6]; Mike's decisions A and B/L, 2026-11-24. The rule
is declarative, in one place: `ToolEntry.owner_only`, read by `ToolRegistry.execute` (the call is
refused and the body never runs) and by the offered list (`DpcAgent._get_allowed_tools` and
`ToolRegistry.schemas`). The turn class is the one `agent.turn_class` gives; a turn whose class is
unknown is not the owner's.

Real `DpcAgent.process`, `run_llm_loop`, adapter, tool registry and the real `comfyui_submit`, on a
pool thread with its call scheduled back on the turn's loop. Only the provider is a double: it
records the tool names it is offered and forces one `comfyui_submit` call whatever it was offered.
The HTTP layer is stubbed at `httpx.AsyncClient.send`, so any request ComfyUI would get is counted.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent.agent import OWNER_TURN, AgentConfig, DpcAgent
from dpc_client_core.dpc_agent.retrieval import factory as retrieval_factory
from dpc_client_core.dpc_agent.tools import comfyui as cf
from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolEntry, ToolRegistry
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.llm_manager import LLMManager

ALIAS = "cloud"
PROFILE = "agent_x"
THIS_NODE = "dpc-node-this0000000000000000"
OWN_NODE = "dpc-node-own00000000000000000"
FOREIGN_NODE = "dpc-node-foreign0000000000000"
WORKFLOW = {"1": {"class_type": "KSampler", "inputs": {}}}
COMFYUI = sorted(e.name for e in cf.get_tools())


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("DPC_HOME", str(tmp_path / "home" / ".dpc"))
    monkeypatch.setattr(retrieval_factory, "_derive_embedding_metadata", lambda cfg: ("BAAI/bge-m3", 1024))
    monkeypatch.setattr(cf, "_client", None)


class _ComfyUI:
    """The HTTP layer: every request that would leave for ComfyUI, answered as ComfyUI would."""

    def __init__(self, monkeypatch):
        self.requests = []

        async def send(client, request, **_kw):
            self.requests.append(request)
            return httpx.Response(200, json={"prompt_id": "pid-7"}, request=request)

        monkeypatch.setattr(httpx.AsyncClient, "send", send)


def _names(tools) -> list:
    out = []
    for t in tools or []:
        name = t.get("name") or (t.get("function") or {}).get("name")
        if name:
            out.append(name)
    return out


class _Provider:
    """Round 1: records the offered tool names and calls comfyui_submit regardless. Round 2: answers,
    after recording what the tool result said."""

    model = "m"

    def __init__(self):
        self.offered = None
        self.after_call = ""
        self.round = 0

    def supports_vision(self):
        return False

    def supports_thinking(self):
        return False

    def get_last_usage(self):
        return {}

    async def generate_with_tools(self, messages, tools, system=None, on_chunk=None,
                                  conversation_id=None, **_):
        self.round += 1
        usage = {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}
        if self.round == 1:
            self.offered = _names(tools)
            call = SimpleNamespace(id="call-1", name="comfyui_submit", input={"workflow_json": WORKFLOW})
            return {"content": "", "tool_calls_raw": [call], "usage": usage}
        self.after_call = json.dumps(messages, default=str, ensure_ascii=False)
        return {"content": "the answer", "usage": usage}

    async def generate_response(self, prompt, **_):
        return "the answer"


class _Manager:
    token_count_manager = None
    agent_provider = None
    vision_provider = None
    _alias_for_query = LLMManager._alias_for_query

    def __init__(self, provider):
        self.providers = {ALIAS: provider}
        self.default_provider = ALIAS

    def alias_context_window(self, alias):
        return 128000

    async def query(self, prompt, provider_alias=None, images=None, return_metadata=False, **_):
        return {"response": "the answer", "provider": ALIAS} if return_metadata else "the answer"


class _Groups:
    node_id = THIS_NODE

    def __init__(self, *groups):
        self._groups = {g.group_id: g for g in groups}

    def get_group(self, gid):
        return self._groups.get(gid)

    def get_all_groups(self):
        return list(self._groups.values())


def _group(gid, members, discord=False):
    return SimpleNamespace(group_id=gid, name=gid, members=list(members), is_discord_bridge=discord)


GROUPS = _Groups(
    _group("group-mixed", [THIS_NODE, FOREIGN_NODE]),
    _group("group-own", [THIS_NODE]),
    _group("group-discord", [THIS_NODE], discord=True),
)


def _firewall(tmp_path) -> ContextFirewall:
    """A real firewall that enables comfyui_submit for the agent, in groups too."""
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({
        "dpc_agent": {"enabled": True, "tools": {name: True for name in COMFYUI}},
        "agent_profiles": {PROFILE: {"tools": {name: True for name in COMFYUI}}},
    }), encoding="utf-8")
    return ContextFirewall(rules)


def _agent(tmp_path, provider, firewall=None) -> DpcAgent:
    return DpcAgent(llm_manager=_Manager(provider), config=AgentConfig(max_rounds=5),
                    agent_root=tmp_path / "agents" / PROFILE, provider_alias=ALIAS,
                    firewall=firewall, firewall_profile=PROFILE if firewall else None,
                    service=SimpleNamespace(group_manager=GROUPS, p2p_manager=None))


async def _turn(tmp_path, monkeypatch, source, conversation_id, firewall=False):
    comfy = _ComfyUI(monkeypatch)
    provider = _Provider()
    agent = _agent(tmp_path, provider, _firewall(tmp_path) if firewall else None)
    answer = await asyncio.wait_for(
        agent.process("make me a picture", conversation_id, message_source=source), 30)
    assert answer == "the answer"
    return provider, comfy


def _refused(provider, comfy):
    assert "comfyui_submit" not in provider.offered, provider.offered
    assert not set(COMFYUI) & set(provider.offered), "no ComfyUI schema is offered"
    assert "pid-7" not in provider.after_call, "the tool body never ran"
    assert comfy.requests == [], [str(r.url) for r in comfy.requests]


NOT_THE_OWNER = [
    # (a) a group with a foreign node is other_node
    ("group", "group-mixed"),
    # (b) a stranger: Telegram that is not the owner's chat, no source, Discord
    ("telegram", "telegram-5"),
    (None, "conv-none"),
    ("discord", "discord-9"),
    # a Discord bridge group is other_node even with this node alone
    ("group", "group-discord"),
]


class TestNotTheOwnersTurn:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("firewall", [False, True], ids=["no-firewall", "firewall-allows-it"])
    @pytest.mark.parametrize("source,conversation_id", NOT_THE_OWNER)
    async def test_comfyui_is_not_offered_and_a_forced_call_is_refused_with_no_http_request(
        self, tmp_path, monkeypatch, source, conversation_id, firewall,
    ):
        provider, comfy = await _turn(tmp_path, monkeypatch, source, conversation_id, firewall)
        _refused(provider, comfy)
        assert "comfyui_submit" in provider.after_call and "⚠️" in provider.after_call


OWNER = [
    ("ui", "conv-ui"),
    ("external_agent", "conv-bridge"),
    ("telegram_owner", "telegram-1"),
    # a group of this node alone is the owner's
    ("group", "group-own"),
]


class TestTheOwnersTurn:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("firewall", [False, True], ids=["no-firewall", "firewall-allows-it"])
    @pytest.mark.parametrize("source,conversation_id", OWNER)
    async def test_comfyui_is_offered_and_runs(self, tmp_path, monkeypatch, source, conversation_id,
                                               firewall):
        provider, comfy = await _turn(tmp_path, monkeypatch, source, conversation_id, firewall)
        assert "comfyui_submit" in provider.offered
        assert set(COMFYUI) <= set(provider.offered)
        assert [str(r.url) for r in comfy.requests] == [f"{cf.DEFAULT_API_URL}/prompt"]
        assert "Queued. prompt_id=pid-7" in provider.after_call


class TestAnUnknownClass:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cls", [None, "", "owner ", "OWNER", "guest"])
    async def test_a_turn_of_a_class_nobody_named_is_refused(self, tmp_path, monkeypatch, cls):
        # The owner's door, but the class came back as something that is not OWNER_TURN.
        monkeypatch.setattr(DpcAgent, "turn_audience", lambda self, s, c: (cls, frozenset()))
        provider, comfy = await _turn(tmp_path, monkeypatch, "ui", "conv-ui")
        _refused(provider, comfy)

    @pytest.mark.asyncio
    async def test_a_classification_that_fails_is_not_the_owners(self, tmp_path, monkeypatch):
        agent = _agent(tmp_path, _Provider(), _firewall(tmp_path))

        def broken(self, source, conversation_id):
            raise RuntimeError("roster unreadable")

        monkeypatch.setattr(DpcAgent, "turn_audience", broken)
        allowed = agent._get_allowed_tools(message_source="ui", conversation_id="conv-ui")
        assert allowed is not None and not set(COMFYUI) & allowed

    def test_a_bare_context_runs_no_owner_only_tool(self, tmp_path, monkeypatch):
        comfy = _ComfyUI(monkeypatch)
        registry = ToolRegistry(agent_root=tmp_path / "agents" / PROFILE)
        ctx = ToolContext(agent_root=tmp_path / "agents" / PROFILE)  # no class: a probe, not a turn
        answer = registry.execute("comfyui_submit", {"workflow_json": WORKFLOW}, ctx=ctx)
        assert answer.startswith("⚠️") and "owner" in answer and "\n" not in answer, answer
        assert comfy.requests == []
        registry.set_context(ctx)
        assert not set(COMFYUI) & set(_names(registry.schemas(include_restricted=True)))


class TestTheFlag:
    def test_every_comfyui_tool_is_owner_only(self):
        assert COMFYUI and all(e.owner_only for e in cf.get_tools())

    def test_a_tool_is_not_owner_only_unless_it_says_so(self):
        entry = ToolEntry(name="t", schema={"name": "t"}, handler=lambda ctx: "ran")
        assert entry.owner_only is False

    def test_a_handler_override_keeps_the_flag(self, tmp_path):
        registry = ToolRegistry(agent_root=tmp_path / "agents" / PROFILE)
        registry.override_handler("comfyui_submit", lambda ctx, **kw: "the override ran")
        ctx = ToolContext(agent_root=tmp_path, turn_class="other_node", message_source="group")
        answer = registry.execute("comfyui_submit", {}, ctx=ctx)
        assert answer.startswith("⚠️") and "the override ran" not in answer

    def test_the_refusal_names_the_tool_and_the_turn_class(self, tmp_path):
        registry = ToolRegistry(agent_root=tmp_path / "agents" / PROFILE)
        ran = []
        registry.register(ToolEntry(name="t", schema={"name": "t", "parameters": {"type": "object"}},
                                    handler=lambda ctx: ran.append(1) or "ran", owner_only=True))
        answer = registry.execute("t", {}, ctx=ToolContext(agent_root=tmp_path, turn_class="stranger"))
        assert "'t'" in answer and "stranger" in answer and not ran
        owner = ToolContext(agent_root=tmp_path, turn_class=OWNER_TURN, message_source="ui")
        assert registry.execute("t", {}, ctx=owner) == "ran"
