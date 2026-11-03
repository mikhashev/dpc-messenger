"""An agent switched to a local alias runs on that engine's window.

On 2026-11-02 agent Forge was switched in the Agent Configuration to a Strata alias
Add Provider had made from the model files folder. Its first message answered
"Context window full (33,533 / 4,096 tokens, 819%)" and no model was called. The alias
had no `context_window`, its model name is in no table, and `get_context_window` answered
4096 for a name it did not know. The engine that alias starts holds 131072.

Every test here drives the real switch (`AgentService.save_agent_model_config`), a real
`LLMManager` loading a real `StrataProvider` from `providers.json`, the real manager and
the real guard in `DpcAgent.process`. What is faked is the engine: its process and its
`/props`, or nothing at all when none has started. `run_llm_loop` raises on arrival, so
"the model was called" is an event a test can see and needs no model.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.agent_service import AgentService
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent import utils as agent_utils
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.llm_manager import LLMManager, MODEL_CONTEXT_WINDOWS
from dpc_client_core.managers import agent_manager as am
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS, LlamaServerProvider

from tests.test_strata_supervisor import BASE_ARGS, home, _install  # noqa: F401

AGENT = "agent_forge_test"
ALIAS = "flash-next-iq3_s Strata"
MODEL = "flash-next-iq3_s"
# What Forge's first prompt measured.
PROMPT_TOKENS = 33533
SERVER_WINDOW = 131072


class _ModelCalled(Exception):
    """The turn got past every guard and reached the model."""


def _args(max_context=SERVER_WINDOW):
    args = list(BASE_ARGS)
    i = args.index("--max-context")
    del args[i:i + 2]
    return args if max_context is None else args + ["--max-context", str(max_context)]


def _alias(root, alias=ALIAS, model=MODEL, **fields):
    return {
        "alias": alias, "type": "strata", "model": model,
        "strata_install_dir": str(root), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 7168, **fields,
    }


def _fake_engine(provider, n_ctx):
    """The launcher as a provider sees it once it has answered /props."""
    provider.supervisor._proc = SimpleNamespace(returncode=None)
    provider.supervisor.props = {"default_generation_settings": {"n_ctx": n_ctx, "params": {}},
                                 "modalities": {"vision": False}}


class _Node:
    """One node: providers.json, its LLMManager, one agent config on disk and a live manager."""

    def __init__(self, tmp_path, monkeypatch, *aliases, agent_config=None):
        self.tmp_path = tmp_path
        self.providers_path = tmp_path / "providers.json"
        self.providers_path.write_text(json.dumps({
            "default_provider": "vendor", "agent_provider": "vendor",
            "providers": [{"alias": "vendor", "type": "deepseek", "model": "deepseek-v4-flash",
                           "api_key": "x", "context_window": 1000000}, *aliases],
        }), encoding="utf-8")
        self.llm = LLMManager(self.providers_path)

        self.config_path = tmp_path / AGENT / "config.json"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text(json.dumps(
            agent_config if agent_config is not None else {"provider_alias": "vendor", "compute_host": ""}
        ), encoding="utf-8")
        registry = SimpleNamespace(get_agent=lambda aid: {"agent_id": aid}, update_agent=lambda *a: None)
        monkeypatch.setattr(agent_utils, "get_agent_config_path", lambda aid: tmp_path / aid / "config.json")
        monkeypatch.setattr(agent_utils, "AgentRegistry", lambda *a, **k: registry)

        self.monitor = ConversationMonitor(
            conversation_id=AGENT,
            participants=[{"node_id": "dpc-node-me", "name": "User", "context": "local"},
                          {"node_id": AGENT, "name": "Forge", "context": "agent"}],
            llm_manager=None,
        )
        self.monitor._get_history_path = lambda: tmp_path / AGENT / "history.json"

        self.events = []

        async def _broadcast(event, payload):
            self.events.append((event, payload))

        self.agent = _agent(tmp_path, monkeypatch, self.llm)
        manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
        manager.agent_id = AGENT
        manager.firewall = MagicMock()
        manager.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        manager._agent = self.agent
        manager._agents = {}
        manager._chat_aliases = {}
        manager._memory_indexes_initialized = True
        manager._agent_display_name = "Forge"
        manager._interrupt_events = {}
        manager._kill_events = {}
        manager._daily_tokens_used = 0
        manager._daily_tokens_date = ""
        manager._agent_monitors = {AGENT: self.monitor}
        manager.ensure_started = AsyncMock(return_value=manager)
        manager._resolve_reasoning_effort = lambda conv, effort: (None, "none")
        manager.service = SimpleNamespace(
            llm_manager=self.llm, peer_metadata={},
            p2p_manager=SimpleNamespace(node_id="dpc-node-me"),
            local_api=SimpleNamespace(broadcast_event=_broadcast),
        )
        self.manager = manager
        self.llm.providers["dpc_agent"] = SimpleNamespace(
            model="dpc_agent", config={"type": "dpc_agent"}, _managers={AGENT: manager})

        self.service = AgentService.__new__(AgentService)
        self.service.llm_manager = self.llm
        self.service.peer_metadata = {}

    async def switch(self, alias):
        return await self.service.save_agent_model_config(AGENT, provider_alias=alias)

    async def send(self, **kwargs):
        return await self.manager._process_message_guarded("hello", AGENT, **kwargs)

    def stored(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))


def _agent(tmp_path, monkeypatch, llm):
    """The real guard over a prompt of the size Forge sent; the loop stands for the model."""
    monkeypatch.setattr(agent_mod, "build_llm_messages",
                        lambda **kw: ([], {"estimated_tokens_before": PROMPT_TOKENS}))

    async def _reached(**kw):
        raise _ModelCalled()

    monkeypatch.setattr(agent_mod, "run_llm_loop", _reached)
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = tmp_path
    agent.memory = agent.skill_store = agent._firewall = agent._embedding_provider = None
    agent._service = None
    agent.tools = MagicMock()
    agent.llm = DpcLlmAdapter(llm, provider_alias="vendor", compute_host="", caller=AGENT, caller_kind="agent")
    agent.config = SimpleNamespace(billing_model="pay_per_use", budget_usd=None, max_rounds=1)
    agent._get_allowed_tools = lambda **kw: set()
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    agent._last_usage = {}
    agent._last_trace = {}
    return agent


@pytest.fixture(autouse=True)
def _no_supervisors_left():
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


@pytest.fixture
def node(tmp_path, monkeypatch, home, signing_identity):  # noqa: F811
    def _make(*aliases, **kwargs):
        return _Node(tmp_path, monkeypatch, *aliases, **kwargs)
    return _make


# --- The defect ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_message_after_the_switch_reaches_the_model(node, tmp_path):
    root = _install(tmp_path, args=_args())
    n = node(_alias(root))
    await n.switch(ALIAS)

    try:
        text = await n.send()
    except _ModelCalled:
        return
    pytest.fail(f"refused with no model call: {text}")


@pytest.mark.asyncio
async def test_an_engine_not_started_answers_from_its_start_config(node, tmp_path):
    root = _install(tmp_path, args=_args())
    n = node(_alias(root))

    saved = await n.switch(ALIAS)

    assert saved["context_window"] == SERVER_WINDOW
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == SERVER_WINDOW
    assert n.monitor.token_limit == SERVER_WINDOW
    with pytest.raises(_ModelCalled):
        await n.send()


@pytest.mark.asyncio
async def test_an_agent_with_no_live_manager_gets_the_window_from_the_save(node, tmp_path):
    """The chat header takes the save's answer, whether or not the agent has run yet."""
    root = _install(tmp_path, args=_args())
    n = node(_alias(root))
    n.llm.providers["dpc_agent"]._managers.clear()

    assert (await n.switch(ALIAS))["context_window"] == SERVER_WINDOW


@pytest.mark.asyncio
async def test_a_running_engine_answers_with_what_it_reports(node, tmp_path):
    """The start config said 131072; the engine says 99000, and it is the engine that refuses."""
    root = _install(tmp_path, args=_args())
    n = node(_alias(root))
    _fake_engine(n.llm.providers[ALIAS], 99000)

    saved = await n.switch(ALIAS)

    assert saved["context_window"] == 99000
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 99000
    with pytest.raises(_ModelCalled):
        await n.send()


@pytest.mark.asyncio
async def test_the_window_follows_the_engine_after_the_first_props(node, tmp_path):
    root = _install(tmp_path, args=_args())
    n = node(_alias(root))
    await n.switch(ALIAS)
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == SERVER_WINDOW

    provider = n.llm.providers[ALIAS]
    _fake_engine(provider, 99000)
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 99000

    _fake_engine(provider, 65536)
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 65536

    # Stopped: back to what the next start would make.
    provider.supervisor._proc = None
    provider.supervisor.props = None
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == SERVER_WINDOW


@pytest.mark.asyncio
async def test_the_aliass_own_context_window_stands_over_the_engine(node, tmp_path):
    """ADR-044 point 7: the owner may set the conversation size parking was measured at."""
    root = _install(tmp_path, args=_args())
    n = node(_alias(root, context_window=80000))
    _fake_engine(n.llm.providers[ALIAS], 99000)

    saved = await n.switch(ALIAS)

    assert saved["context_window"] == 80000
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 80000


@pytest.mark.asyncio
async def test_the_aliass_own_max_context_is_the_start_it_would_make(node, tmp_path):
    root = _install(tmp_path, args=_args())
    n = node(_alias(root, max_context=64000))

    assert (await n.switch(ALIAS))["context_window"] == 64000


# --- A window nobody knows -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_window_is_said_and_invents_no_number(node, tmp_path):
    """No flag in the run config, nothing started: no table, no 4096, no 204800. The turn is
    refused by a sentence naming the alias and the way out, and no model call is made."""
    root = _install(tmp_path, args=_args(max_context=None))
    n = node(_alias(root))

    saved = await n.switch(ALIAS)
    assert saved["context_window"] is None
    assert n.monitor.token_limit == 0

    state = n.manager.get_session_state(AGENT)
    assert state["tokens_limit"] == 0
    assert ALIAS in state["window_unknown"]

    text = await n.send()

    assert ALIAS in text and "Context Window" in text
    assert "full" not in text and "4,096" not in text
    assert n.agent._last_trace == {"turn_end": "guard"}
    saved_records = json.loads(n.monitor._get_history_path().read_text(encoding="utf-8"))["messages"]
    assert saved_records[-1]["turn_end"] == "guard"


@pytest.mark.asyncio
async def test_an_unknown_window_ends_when_the_engine_has_said_it(node, tmp_path):
    root = _install(tmp_path, args=_args(max_context=None))
    n = node(_alias(root))
    await n.switch(ALIAS)
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 0

    _fake_engine(n.llm.providers[ALIAS], 99000)

    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 99000
    with pytest.raises(_ModelCalled):
        await n.send()


def test_a_local_alias_never_reads_the_model_name_table(tmp_path, monkeypatch, home):  # noqa: F811
    """`gpt-4` is in the table and the partial match would take this name for it."""
    root = _install(tmp_path, args=_args(max_context=None))
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"providers": [_alias(root, model="my-gpt-4-finetune")]}), encoding="utf-8")
    llm = LLMManager(path)

    assert llm.lookup_context_window("my-gpt-4-finetune") is None
    assert llm.alias_context_window(ALIAS) is None


def test_a_name_nobody_serves_still_gets_the_table_and_no_default(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"providers": []}), encoding="utf-8")
    llm = LLMManager(path)

    assert llm.lookup_context_window("gpt-4o") == MODEL_CONTEXT_WINDOWS["gpt-4o"]
    assert llm.lookup_context_window("no-such-model-here") is None


# --- Which alias is asked --------------------------------------------------------


@pytest.mark.asyncio
async def test_two_aliases_of_one_model_each_keep_their_own_window(node, tmp_path):
    root = _install(tmp_path, args=_args())
    n = node(_alias(root, alias="first", context_window=250000),
             _alias(root, alias="second", context_window=50000))

    assert (await n.switch("second"))["context_window"] == 50000
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 50000

    assert (await n.switch("first"))["context_window"] == 250000
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 250000


@pytest.mark.asyncio
async def test_two_aliases_of_one_model_one_with_no_window_reads_its_own_engine(node, tmp_path):
    """The sibling's number is not this alias's: the old lookup went by model name and took it."""
    root = _install(tmp_path, args=_args())
    n = node(_alias(root, alias="first", context_window=250000), _alias(root, alias="second"))

    assert (await n.switch("second"))["context_window"] == SERVER_WINDOW


@pytest.mark.asyncio
async def test_a_turn_on_the_chats_own_alias_is_measured_on_that_alias(node, tmp_path, monkeypatch):
    """The chat's Text selector: this message runs on `small`, and the guard must say so
    even though the agent's configuration still names `big`."""
    root = _install(tmp_path, args=_args())
    n = node(_alias(root, alias="big", context_window=250000),
             _alias(root, alias="small", context_window=8192))
    await n.switch("big")
    n.manager._agents["small"] = n.agent

    text = await n.send(agent_llm_provider="small")

    assert "Context window full" in text and "8,192" in text
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 8192

    # A switch in the Agent Configuration ends the chat's own choice.
    await n.switch("big")
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 250000


@pytest.mark.asyncio
async def test_a_chat_that_names_no_alias_runs_on_the_agents_configured_one(node, tmp_path):
    """`_execute_agent_query` passes the conversation id when the chat names no alias. That is
    no alias: the agent must run as configured, not on a second agent that falls through to
    the node's `agent_provider`."""
    root = _install(tmp_path, args=_args())
    n = node(_alias(root))
    await n.switch(ALIAS)

    assert n.manager._get_or_create_agent_for_provider(AGENT) is n.agent
    assert n.manager._agents == {}


@pytest.mark.asyncio
async def test_a_switch_reaches_the_cached_agent_of_each_alias(node, tmp_path):
    """Keyed by alias, created once: its peer pin used to stay what it was at creation."""
    root = _install(tmp_path, args=_args())
    n = node(_alias(root))
    pinned = []
    n.manager._agents[ALIAS] = SimpleNamespace(set_compute_host=pinned.append)
    n.agent.set_provider_alias = lambda alias: None
    n.agent.set_compute_host = lambda host: None

    await n.switch(ALIAS)

    assert pinned == [""]


def test_a_local_alias_a_chat_picks_does_not_dial_the_agents_peer(node, tmp_path):
    root = _install(tmp_path, args=_args())
    n = node(_alias(root), agent_config={"provider_alias": "peer-alias", "compute_host": "dpc-node-a"})

    assert n.manager._host_of_alias(ALIAS) == ""
    assert n.manager._host_of_alias("peer-alias") == "dpc-node-a"


@pytest.mark.asyncio
async def test_a_peer_row_that_states_no_window_leaves_no_old_number_behind(node, tmp_path):
    """Peer A stated 40000; the agent moved to peer B's alias, which states none."""
    root = _install(tmp_path, args=_args())
    n = node(_alias(root), agent_config={"provider_alias": "a-alias", "compute_host": "dpc-node-a",
                                         "context_window": 40000})
    n.service.peer_metadata = {"dpc-node-b": {"providers": [{"alias": "b-alias", "model": "x-unknown"}]}}
    n.manager.service.peer_metadata = n.service.peer_metadata

    saved = await n.switch("b-alias")

    assert "context_window" not in n.stored()
    assert saved["context_window"] is None
    text = await n.send()
    assert "b-alias" in text and "40,000" not in text


# --- The live chat ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_saved_provider_edit_reaches_the_live_monitors_and_the_header(node, tmp_path):
    root = _install(tmp_path, args=_args())
    n = node(_alias(root))
    await n.switch(ALIAS)
    assert n.monitor.token_limit == SERVER_WINDOW

    config = json.loads(n.providers_path.read_text(encoding="utf-8"))
    config["providers"][1]["context_window"] = 70000
    n.llm.providers.pop("dpc_agent")
    n.llm.providers["dpc_agent"] = SimpleNamespace(
        model="dpc_agent", config={"type": "dpc_agent"}, _managers={AGENT: n.manager})
    # The reload drops an alias the file does not name; this one is the test's own.
    config["providers"].append({"alias": "dpc_agent", "type": "dpc_agent"})
    n.llm.save_config(config)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert n.monitor.token_limit == 70000
    assert n.manager.get_session_state(AGENT)["tokens_limit"] == 70000
    pushed = [p for event, p in n.events if event == "token_usage_updated"]
    assert pushed and pushed[-1]["conversation_id"] == AGENT and pushed[-1]["token_limit"] == 70000


def test_a_history_file_that_remembers_4096_does_not_set_the_new_monitor(tmp_path, monkeypatch, home, signing_identity):  # noqa: F811
    """Forge's history.json holds `token_limit: 4096` from the refused turn."""
    root = _install(tmp_path, args=_args())
    n = _Node(tmp_path, monkeypatch, _alias(root), agent_config={"provider_alias": ALIAS, "compute_host": ""})
    n.manager._agent_monitors = {}
    history = tmp_path / "conversations" / AGENT / "history.json"
    history.parent.mkdir(parents=True)
    history.write_text(json.dumps({"conversation_id": AGENT, "messages": [],
                                   "token_stats": {"token_limit": 4096, "current_token_count": 0}}),
                       encoding="utf-8")
    monkeypatch.setattr(ConversationMonitor, "_get_history_path", lambda self: history)

    monitor = n.manager._get_or_create_agent_monitor(AGENT)

    assert monitor.token_limit == SERVER_WINDOW


# --- The other local type --------------------------------------------------------


def test_a_llama_server_alias_with_no_window_reads_the_pool_it_starts_or_the_child(tmp_path, monkeypatch, home):  # noqa: F811
    path = tmp_path / "providers.json"
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"")
    path.write_text(json.dumps({"providers": [
        {"alias": "ll", "type": "llamacpp_server", "model": "m", "gguf_path": str(gguf), "n_ctx": 100000},
    ]}), encoding="utf-8")
    llm = LLMManager(path)
    provider = llm.providers["ll"]
    assert isinstance(provider, LlamaServerProvider)

    assert llm.alias_context_window("ll") == 100000

    _fake_engine(provider, 90000)
    assert llm.alias_context_window("ll") == 90000


def test_a_llama_server_start_without_a_unified_pool_divides_it_by_its_slots(tmp_path, monkeypatch, home):  # noqa: F811
    path = tmp_path / "providers.json"
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"")
    path.write_text(json.dumps({"providers": [
        {"alias": "ll", "type": "llamacpp_server", "model": "m", "gguf_path": str(gguf),
         "n_ctx": 100000, "n_parallel": 2, "kv_unified": False},
    ]}), encoding="utf-8")

    assert LLMManager(path).alias_context_window("ll") == 50000
