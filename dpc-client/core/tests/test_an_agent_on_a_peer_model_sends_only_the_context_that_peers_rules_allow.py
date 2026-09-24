"""An agent whose model runs on a peer ships its whole message list to that peer.

`DpcLlmAdapter` flattens the turns with `messages_to_prompt`, which writes every
tool result verbatim, and hands the string to `_request_inference_from_peer`. The
agent reads the user's context through `get_dpc_context`, so without a filter the
tool's JSON — personal.json or device_context.json — reached the peer untouched,
past the gate `execute_ai_query` applies to a plain chat. Two routes lead there:
a per-agent `compute_host` (or the dpc_agent `peer_id`), and an agent whose alias
is a `remote_peer` provider. Both must carry what REQUEST_CONTEXT would give the
peer and nothing else.

The adapter runs for real down to the wire call; the tool result in the history
is produced by the real `get_dpc_context` over a personal.json on disk.
"""

import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from dpc_protocol.pcm_core import PersonalContext, Profile, Topic

from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.tools.core import PEER_CONTEXT_WITHHELD, get_dpc_context
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.providers.remote_peer_provider import RemotePeerProvider
from dpc_client_core.service import CoreService

ALLOWED_PEER = "dpc-node-bob-allowed-profile"
DENIED_PEER = "dpc-node-eve-no-rules"
MARKERS = ("Alice Visible", "PRIVATE-KNOWLEDGE-MARKER", "PRIVATE-METADATA-MARKER",
           "PRIVATE-GPU-MARKER", "SHARED-OS-MARKER")


class _Service:
    """What the adapter and the peer filter read off CoreService."""

    _context_for_compute_peer = CoreService._context_for_compute_peer

    def __init__(self, firewall, context, device_context):
        self.firewall = firewall
        self.device_context = device_context
        self.p2p_manager = SimpleNamespace(local_context=context, peers={})
        self.sent = []

    async def _request_inference_from_peer(self, peer_id, prompt, **kwargs):
        self.sent.append((peer_id, prompt))
        return {"response": "done"}


def _personal_context():
    return PersonalContext(
        profile=Profile(name="Alice Visible", description="shown to Bob", core_values=[]),
        knowledge={"Health": Topic(summary="PRIVATE-KNOWLEDGE-MARKER")},
        metadata={"note": "PRIVATE-METADATA-MARKER"},
    )


def _device_context():
    return {
        "hardware": {"gpu": {"model": "PRIVATE-GPU-MARKER"}},
        "software": {"os": {"family": "SHARED-OS-MARKER"}},
    }


@pytest.fixture
def firewall(tmp_path):
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({
        "nodes": {
            ALLOWED_PEER: {
                "personal.json:profile": "allow",
                "personal.json:knowledge": "deny",
                "device_context.json:software.*": "allow",
            }
        }
    }))
    return ContextFirewall(rules)


@pytest.fixture
def history(tmp_path, monkeypatch):
    """A turn in which the agent called get_dpc_context for both contexts; the
    results are the real tool's output over files on disk."""
    home = tmp_path / "dpc"
    home.mkdir()
    (home / "personal.json").write_text(json.dumps(asdict(_personal_context())), encoding="utf-8")
    (home / "device_context.json").write_text(json.dumps(_device_context()), encoding="utf-8")
    monkeypatch.setenv("DPC_HOME", str(home))
    ctx = ToolContext(agent_root=tmp_path / "agent")
    personal = get_dpc_context(ctx, "personal")
    device = get_dpc_context(ctx, "device")
    for marker in ("Alice Visible", "PRIVATE-METADATA-MARKER", "PRIVATE-GPU-MARKER"):
        assert marker in personal + device, "the history must hold the context to begin with"
    return [
        {"role": "system", "content": "You are an agent."},
        {"role": "user", "content": "what do you know about me?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_p", "type": "function",
             "function": {"name": "get_dpc_context", "arguments": '{"context_type": "personal"}'}},
            {"id": "call_d", "type": "function",
             "function": {"name": "get_dpc_context", "arguments": '{"context_type": "device"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_p", "content": personal},
        {"role": "tool", "tool_call_id": "call_d", "content": device},
    ]


def _adapter(route, service, peer):
    """An adapter that sends to `peer` by one of the two routes."""
    if route == "compute_host":
        dpc_agent = SimpleNamespace(peer_id=None, remote_model=None, remote_provider="m",
                                    timeout=5, _service=service)
        manager = SimpleNamespace(providers={"dpc_agent": dpc_agent}, token_count_manager=None,
                                  default_provider=None)
        return DpcLlmAdapter(manager, provider_alias="m", compute_host=peer)
    alias = RemotePeerProvider("peer_alias", {"peer_id": peer})
    alias.set_service(service)
    manager = SimpleNamespace(providers={"peer_alias": alias}, token_count_manager=None,
                              default_provider=None)
    return DpcLlmAdapter(manager, provider_alias="peer_alias")


ROUTES = ["compute_host", "remote_peer_alias"]


async def _sent_prompt(route, service, peer, messages):
    await _adapter(route, service, peer)._chat(list(messages))
    assert len(service.sent) == 1
    sent_peer, prompt = service.sent[0]
    assert sent_peer == peer
    return prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_the_peer_gets_the_fields_its_node_rules_allow_and_nothing_else(firewall, history, route):
    service = _Service(firewall, _personal_context(), _device_context())

    prompt = await _sent_prompt(route, service, ALLOWED_PEER, history)

    assert "Alice Visible" in prompt
    assert "SHARED-OS-MARKER" in prompt
    for marker in ("PRIVATE-KNOWLEDGE-MARKER", "PRIVATE-METADATA-MARKER", "PRIVATE-GPU-MARKER"):
        assert marker not in prompt
    assert "what do you know about me?" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_a_peer_with_no_rules_gets_no_field_at_all(firewall, history, route):
    service = _Service(firewall, _personal_context(), _device_context())

    prompt = await _sent_prompt(route, service, DENIED_PEER, history)

    for marker in MARKERS:
        assert marker not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_a_filter_that_raises_sends_the_placeholder(firewall, history, route):
    def _broken(*args, **kwargs):
        raise RuntimeError("rules unreadable")

    firewall.filter_context_for_peer = _broken
    firewall.filter_device_context_for_peer = _broken
    service = _Service(firewall, _personal_context(), _device_context())

    prompt = await _sent_prompt(route, service, ALLOWED_PEER, history)

    for marker in MARKERS:
        assert marker not in prompt
    assert prompt.count(PEER_CONTEXT_WITHHELD) == 2


@pytest.mark.asyncio
async def test_the_agents_own_history_is_left_as_it_was(firewall, history):
    """The loop keeps its message list for the next round; re-cutting it for the
    wire must not rewrite what the agent itself holds."""
    service = _Service(firewall, _personal_context(), _device_context())
    before = json.dumps(history)

    await _adapter("compute_host", service, DENIED_PEER)._chat(history)

    assert json.dumps(history) == before


# --- One wire, every caller known -------------------------------------------

# Each call site that hands a prompt to a peer's model, and the gate it passes.
# A new one fails this test until someone decides which gate covers it.
REVIEWED_PEER_INFERENCE_CALLERS = {
    # execute_ai_query → send_ai_query → here: the gate in execute_ai_query.
    # Also the instruction wizard (no context) and knowledge extraction
    # (conversation text only).
    ("inference_orchestrator.py", "_request_inference_from_peer"),
    # A remote_peer alias under any caller: execute_ai_query's gate for a chat,
    # DpcLlmAdapter._messages_for_peer for an agent.
    ("remote_peer_provider.py", "_request_inference_from_peer"),
    # Agent on a compute_host / dpc_agent peer_id: DpcLlmAdapter._messages_for_peer.
    ("llm_adapter.py", "_request_inference_from_peer"),
    # The thin delegate to the wire.
    ("service.py", "request_inference_from_peer"),
    # The gateway forwards an external client's own turns; no node context is added.
    ("gateway.py", "request_inference_from_peer"),
}


def test_every_path_to_a_peers_model_is_one_somebody_reviewed():
    package = Path(__file__).resolve().parents[1] / "dpc_client_core"
    call = re.compile(r"\b(_?request_inference_from_peer)\(")
    found = set()
    for path in package.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith(("def ", "async def ")):
                continue
            found.update((path.name, name) for name in call.findall(line))

    assert found == REVIEWED_PEER_INFERENCE_CALLERS
