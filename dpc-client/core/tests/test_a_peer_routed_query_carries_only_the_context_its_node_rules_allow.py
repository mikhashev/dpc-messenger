"""A query routed to a peer for inference ships the assembled prompt off the machine.

`execute_ai_query` builds the prompt with the personal and device context blocks
and hands it to `send_ai_query`; when `compute_host` names a peer the finished
string travels as REMOTE_INFERENCE_REQUEST, and the peer may pass it to a vendor
under its own key. The only field-level filter for peers is the one the
REQUEST_CONTEXT path applies (`filter_context_for_peer`,
`filter_device_context_for_peer`), so a peer compute host must get exactly what
those filters would give it — and nothing when they cannot run.

The path is driven for real from `execute_ai_query` through `send_ai_query` and
the InferenceOrchestrator; only the wire call `_request_inference_from_peer` and
the local `llm_manager.query` are doubles, and each records the prompt it got.
"""

import json
import logging

import pytest
from dpc_protocol.pcm_core import PersonalContext, Profile, Topic

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.inference_orchestrator import InferenceOrchestrator
from dpc_client_core.managers.prompt_manager import PromptManager
from dpc_client_core.providers.remote_peer_provider import RemotePeerProvider
from dpc_client_core.service import CoreService

ALLOWED_PEER = "dpc-node-bob-allowed-profile"
DENIED_PEER = "dpc-node-eve-no-rules"


class _Stop(Exception):
    """Raised by the doubles once they have recorded the prompt."""


class _Instructions:
    def get_default(self):
        return type("I", (), {"primary": "SYSTEM INSTRUCTION"})()

    def get_set(self, name):
        return self.get_default()


class _Monitor:
    token_limit = 0
    current_token_count = 0
    instruction_set_name = None

    def __init__(self):
        self.history = []

    def add_message(self, role, content):
        self.history.append({"role": role, "content": content})

    def get_message_history(self):
        return list(self.history)


class _LocalApi:
    def __init__(self):
        self.responses = []

    async def send_response_to_all(self, **kwargs):
        self.responses.append(kwargs)


class _LLMManager:
    """Dispatches to a registered provider the way LLMManager.query does, and
    records a query that would have run on a local model."""

    def __init__(self, sent):
        self.sent = sent
        self.providers = {}
        self.default_provider = None

    async def query(self, prompt, provider_alias=None, **kwargs):
        chosen = self.providers.get(provider_alias or self.default_provider)
        if chosen is not None:
            return await chosen.generate_response(prompt)
        self.sent.append(("local", None, prompt))
        raise _Stop()


class _P2P:
    def __init__(self, context):
        self.local_context = context
        self.peers = {}


class _FakeService:
    """The attributes `execute_ai_query` reads before the prompt leaves."""

    _inference_peer_for = CoreService._inference_peer_for
    _context_for_compute_peer = CoreService._context_for_compute_peer
    execute_ai_query = CoreService.execute_ai_query
    send_ai_query = CoreService.send_ai_query

    def __init__(self, firewall, context, device_context):
        self.sent = []
        self.firewall = firewall
        self.device_context = device_context
        self.p2p_manager = _P2P(context)
        self.llm_manager = _LLMManager(self.sent)
        self.local_api = _LocalApi()
        self.prompt_manager = PromptManager(_Instructions(), {})
        self.monitor = _Monitor()
        self.inference_orchestrator = InferenceOrchestrator(self)

    def _get_or_create_conversation_monitor(self, conversation_id, instruction_set_name=None):
        return self.monitor

    async def _request_inference_from_peer(self, peer_id, prompt, **kwargs):
        self.sent.append(("peer", peer_id, prompt))
        raise _Stop()


def _personal_context():
    return PersonalContext(
        profile=Profile(name="Alice Visible", description="shown to Bob", core_values=[]),
        knowledge={"Health": Topic(summary="PRIVATE-KNOWLEDGE-MARKER")},
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


async def _run(service, compute_host, provider=None):
    await service.execute_ai_query(
        command_id="c1",
        prompt="what should I run?",
        compute_host=compute_host,
        provider=provider,
        include_context=True,
        conversation_id="local_ai",
    )
    assert len(service.sent) == 1
    return service.sent[0]


@pytest.mark.asyncio
async def test_a_peer_gets_the_fields_its_node_rules_allow_and_nothing_else(firewall):
    service = _FakeService(firewall, _personal_context(), _device_context())

    where, peer_id, prompt = await _run(service, ALLOWED_PEER)

    assert (where, peer_id) == ("peer", ALLOWED_PEER)
    assert "Alice Visible" in prompt
    assert "SHARED-OS-MARKER" in prompt
    assert "PRIVATE-KNOWLEDGE-MARKER" not in prompt
    assert "PRIVATE-GPU-MARKER" not in prompt


@pytest.mark.asyncio
async def test_a_peer_with_no_rules_gets_no_field_at_all(firewall):
    service = _FakeService(firewall, _personal_context(), _device_context())

    _, _, prompt = await _run(service, DENIED_PEER)

    for marker in ("Alice Visible", "PRIVATE-KNOWLEDGE-MARKER",
                   "PRIVATE-GPU-MARKER", "SHARED-OS-MARKER"):
        assert marker not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("as_default", [False, True])
async def test_a_remote_peer_alias_is_the_same_door_as_a_compute_host(firewall, as_default):
    """A `remote_peer` alias in providers.json reaches the peer through the local
    path, with no compute_host, whether it is chosen or is the default."""
    service = _FakeService(firewall, _personal_context(), _device_context())
    alias = RemotePeerProvider("eve_remote", {"peer_id": DENIED_PEER})
    alias.set_service(service)
    service.llm_manager.providers["eve_remote"] = alias
    if as_default:
        service.llm_manager.default_provider = "eve_remote"

    where, peer_id, prompt = await _run(
        service, None, provider=None if as_default else "eve_remote"
    )

    assert (where, peer_id) == ("peer", DENIED_PEER)
    for marker in ("Alice Visible", "PRIVATE-KNOWLEDGE-MARKER",
                   "PRIVATE-GPU-MARKER", "SHARED-OS-MARKER"):
        assert marker not in prompt


@pytest.mark.asyncio
async def test_a_filter_that_raises_sends_no_context_rather_than_all_of_it(firewall, caplog):
    def _broken(*args, **kwargs):
        raise RuntimeError("rules unreadable")

    firewall.filter_context_for_peer = _broken
    service = _FakeService(firewall, _personal_context(), _device_context())

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.service"):
        _, _, prompt = await _run(service, ALLOWED_PEER)

    assert '<CONTEXT source="local">' not in prompt
    assert "<DEVICE_CONTEXT" not in prompt
    assert "Alice Visible" not in prompt
    assert "SHARED-OS-MARKER" not in prompt
    assert "what should I run?" in prompt
    assert any("no personal or device context" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_local_inference_still_gets_the_whole_context_unchanged(firewall):
    context, device = _personal_context(), _device_context()
    service = _FakeService(firewall, context, device)

    where, _, prompt = await _run(service, None)

    expected = PromptManager(_Instructions(), {}).assemble_prompt(
        query="what should I run?",
        contexts={"local": context},
        device_context=device,
        peer_device_contexts={},
        message_history=[{"role": "user", "content": "what should I run?"}],
        include_context=True,
        instruction_set_name=None,
    )
    assert where == "local"
    assert prompt == expected


def test_the_query_command_still_answers_the_client_itself():
    """The helpers sit directly above `execute_ai_query`; the dispatch marker
    must stay on the command, not slide onto a helper."""
    assert getattr(CoreService.execute_ai_query, "dpc_sends_own_response", False)
    assert not getattr(CoreService._inference_peer_for, "dpc_sends_own_response", False)
