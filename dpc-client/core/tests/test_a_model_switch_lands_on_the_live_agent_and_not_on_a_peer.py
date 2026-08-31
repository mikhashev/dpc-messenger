"""Switching an agent's model has to reach the agent that is running.

Two defects, one symptom, found on 2026-08-31 from the Ubuntu node's own log
(Mike's report, plus Ubu's timeline out of `dpc-client.log`):

    12:57:34  save_agent_model_config: provider_alias='qwen3.8 27b Mythos'
    14:11:00  Routing to per-agent remote peer: dpc-node-86cdcd… (provider=qwen3.8 27b Mythos)
    14:11:59  save_agent_model_config: provider_alias='ds_flash'      <- the switch
    14:12:18  Routing to per-agent remote peer: dpc-node-86cdcd… (provider=ds_flash)
    14:12:19  Access denied: not authorized to request inference via provider ds_flash
    14:47:38  disconnect_from_peer                                    <- what fixed it

**A — how the pin appears.** `save_agent_model_config` resolves the alias
against `self.peer_metadata` only; `llm_manager.providers` is not read in that
function at all. The alias is compared as a bare string, so a name that exists
both locally and on a peer goes to the peer by construction, and the local
provider is never considered again: `llm_adapter.chat` puts
`effective_peer_id = self._compute_host or …` ahead of every local branch.

**B — why the switch could not remove it.** `apply_model_config` pushed the new
alias into the live agent (`set_provider_alias`) and nothing else.
`DpcLlmAdapter._compute_host` was assigned once, in the constructor, and
`set_compute_host` did not exist anywhere in the tree. So the config on disk was
already correct at 14:11:59 while the running adapter still held the peer — and
kept sending the peer an alias the peer does not serve.

The two do not substitute for each other: without B a correct config never
reaches the live agent, without A the pin returns on the next save.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dpc_client_core.agent_service import AgentService
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter


PEER = "dpc-node-86cdcdaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _Registry:
    def __init__(self, agent_id):
        self._id = agent_id
        self.updates = []

    def get_agent(self, agent_id):
        return {"agent_id": agent_id} if agent_id == self._id else None

    def update_agent(self, agent_id, patch):
        self.updates.append((agent_id, patch))


@pytest.fixture
def saver(tmp_path, monkeypatch):
    """`save_agent_model_config` over a config file, with both a local provider
    registry and peer metadata in play — which is the case the defect needs."""
    from dpc_client_core.dpc_agent import utils as agent_utils

    agent_id = "agent_test"
    cfg_path = tmp_path / agent_id / "config.json"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(json.dumps({"provider_alias": "qwen3.8 27b Mythos"}), encoding="utf-8")

    registry = _Registry(agent_id)
    monkeypatch.setattr(agent_utils, "get_agent_config_path",
                        lambda aid: tmp_path / aid / "config.json")
    monkeypatch.setattr(agent_utils, "AgentRegistry", lambda *a, **k: registry)

    service = AgentService.__new__(AgentService)
    service.peer_metadata = {}
    service.llm_manager = SimpleNamespace(providers={})

    async def _no_refresh(_aid, _config):
        return None

    monkeypatch.setattr(service, "_refresh_live_agent_manager", _no_refresh, raising=False)

    def _configure(local_aliases=(), peer_aliases=()):
        service.llm_manager.providers = {a: object() for a in local_aliases}
        service.peer_metadata = (
            {PEER: {"providers": [{"alias": a, "context_window": 40000} for a in peer_aliases]}}
            if peer_aliases else {}
        )

    async def _save(**kwargs):
        return await AgentService.save_agent_model_config(service, agent_id, **kwargs)

    def _read():
        return json.loads(cfg_path.read_text(encoding="utf-8"))

    return SimpleNamespace(configure=_configure, save=_save, read=_read, registry=registry)


# --- A: how the pin appears ------------------------------------------------

@pytest.mark.asyncio
async def test_an_alias_we_serve_ourselves_is_not_resolved_to_a_peer(saver):
    """The defect itself. `ds_flash` is ours; a peer advertising the same name
    must not capture it."""
    saver.configure(local_aliases=["ds_flash"], peer_aliases=["ds_flash"])

    await saver.save(provider_alias="ds_flash")

    assert saver.read()["compute_host"] == ""


@pytest.mark.asyncio
async def test_a_local_alias_does_not_borrow_the_peers_context_window(saver):
    """The window travels with the pin. If we run the model, the peer's number
    is not ours to keep."""
    saver.configure(local_aliases=["ds_flash"], peer_aliases=["ds_flash"])

    await saver.save(provider_alias="ds_flash")

    assert "context_window" not in saver.read()


@pytest.mark.asyncio
async def test_the_registry_is_told_the_same_thing_as_the_config(saver):
    """Two stores, one answer — a disagreement here is the pin coming back at
    the next read."""
    saver.configure(local_aliases=["ds_flash"], peer_aliases=["ds_flash"])

    await saver.save(provider_alias="ds_flash")

    patches = [p for _aid, p in saver.registry.updates if "compute_host" in p]
    assert patches and all(p["compute_host"] == "" for p in patches)


@pytest.mark.asyncio
async def test_an_alias_only_a_peer_has_still_resolves_to_that_peer(saver):
    """The other half of the contract. Compute sharing is the feature; this
    guards the fix against being an amputation."""
    saver.configure(local_aliases=["ds_flash"], peer_aliases=["qwen3.8 27b Mythos"])

    await saver.save(provider_alias="qwen3.8 27b Mythos")

    cfg = saver.read()
    assert cfg["compute_host"] == PEER
    assert cfg["context_window"] == 40000


@pytest.mark.asyncio
async def test_an_alias_nobody_has_pins_nothing(saver):
    saver.configure(local_aliases=["ds_flash"], peer_aliases=["qwen3.8 27b Mythos"])

    await saver.save(provider_alias="a name neither side knows")

    assert saver.read()["compute_host"] == ""


# --- B: why the switch could not remove it ---------------------------------

def _adapter(compute_host, providers=None):
    manager = SimpleNamespace(providers=providers if providers is not None else {},
                              token_count_manager=None)
    return DpcLlmAdapter(manager, provider_alias="ds_flash", compute_host=compute_host)


def test_the_adapter_can_be_told_to_drop_its_peer():
    """`set_provider_alias` existed; its counterpart did not."""
    adapter = _adapter(PEER)
    assert adapter._compute_host == PEER

    adapter.set_compute_host("")

    assert adapter._compute_host == ""


def test_the_adapter_can_be_told_to_take_a_peer():
    adapter = _adapter("")

    adapter.set_compute_host(PEER)

    assert adapter._compute_host == PEER


def test_none_is_read_as_no_peer_and_not_as_a_peer_named_none():
    """The config carries `""`, the registry may carry `None`; both mean the
    same thing and neither may reach the routing test as truthy."""
    adapter = _adapter(PEER)

    adapter.set_compute_host(None)

    assert adapter._compute_host == ""


def test_the_agent_passes_the_change_down_to_its_adapter():
    """The manager talks to the agent, not to the adapter."""
    from dpc_client_core.dpc_agent.agent import DpcAgent

    agent = DpcAgent.__new__(DpcAgent)
    agent.llm = _adapter(PEER)

    DpcAgent.set_compute_host(agent, "")

    assert agent.llm._compute_host == ""


def test_applying_a_config_clears_the_pin_on_the_running_agent():
    """The measured defect: at 14:11:59 the config said «no peer» and the live
    adapter went on routing to one."""
    from dpc_client_core.managers.agent_manager import DpcAgentManager

    manager = DpcAgentManager.__new__(DpcAgentManager)
    manager.config = {"provider_alias": "qwen3.8 27b Mythos", "compute_host": PEER}
    manager.service = None
    manager.agent_id = "agent_test"
    manager._agent_monitors = {}

    class _Agent:
        def __init__(self):
            self.llm = _adapter(PEER)
            self.alias = None

        def set_provider_alias(self, alias):
            self.alias = alias
            self.llm.set_provider_alias(alias)

        def set_compute_host(self, host):
            self.llm.set_compute_host(host)

    manager._agent = _Agent()

    DpcAgentManager.apply_model_config(manager, {"provider_alias": "ds_flash", "compute_host": ""})

    assert manager._agent.alias == "ds_flash"
    assert manager._agent.llm._compute_host == ""


def test_applying_a_config_that_names_a_peer_pins_the_running_agent():
    """Symmetry: the switch has to work in the direction that adds a peer too,
    otherwise choosing a peer model needs a restart."""
    from dpc_client_core.managers.agent_manager import DpcAgentManager

    manager = DpcAgentManager.__new__(DpcAgentManager)
    manager.config = {}
    manager.service = None
    manager.agent_id = "agent_test"
    manager._agent_monitors = {}

    class _Agent:
        def __init__(self):
            self.llm = _adapter("")

        def set_provider_alias(self, alias):
            self.llm.set_provider_alias(alias)

        def set_compute_host(self, host):
            self.llm.set_compute_host(host)

    manager._agent = _Agent()

    DpcAgentManager.apply_model_config(
        manager, {"provider_alias": "qwen3.8 27b Mythos", "compute_host": PEER})

    assert manager._agent.llm._compute_host == PEER


# --- B, behaviourally: what the adapter then does with a request -----------

@pytest.mark.asyncio
async def test_a_cleared_pin_stops_the_request_going_to_the_peer():
    """The pin is only interesting because of where it sends the call. With it
    cleared the adapter must fall through to local resolution — here there is
    no local provider either, so it must say so rather than dial the peer."""
    adapter = _adapter(PEER, providers={"dpc_agent": SimpleNamespace(peer_id=None)})

    async def _must_not_be_called(*a, **k):
        raise AssertionError("routed to the peer after the pin was cleared")

    adapter._chat_via_remote_peer = _must_not_be_called
    adapter._get_agent_provider_alias = lambda: None
    adapter.set_compute_host("")

    with pytest.raises(RuntimeError, match="No AI provider configured"):
        await adapter.chat([{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_a_standing_pin_still_routes_to_the_peer():
    """Red-before-green needs both directions: this one must keep passing."""
    adapter = _adapter(PEER, providers={"dpc_agent": SimpleNamespace(peer_id=None)})
    seen = {}

    async def _remote(ctx, messages, tools, on_stream_chunk, conversation_id):
        seen["peer"] = ctx.peer_id
        return ({"role": "assistant", "content": "ok"}, {})

    adapter._chat_via_remote_peer = _remote

    await adapter.chat([{"role": "user", "content": "hello"}])

    assert seen["peer"] == PEER
