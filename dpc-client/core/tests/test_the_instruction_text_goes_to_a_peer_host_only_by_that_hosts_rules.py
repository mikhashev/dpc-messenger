"""The instruction text goes to a peer host only by that host's rules.

`execute_ai_query` re-filters the personal and device context for a peer that
runs the inference, and the peer receives the whole assembled prompt. The
instructions.json primary text sat at its top unfiltered, also when the filter
had dropped both contexts. It is now a rule resource like the others:
`instructions.json:<set_key>.primary`, matched against the peer's node and
node-group entries; no rule, or a check that raises, means no instruction text.
Local inference is unchanged.

The query path is the real one, through the doubles of
test_a_peer_routed_query_carries_only_the_context_its_node_rules_allow.
"""

import json
import logging

import pytest
from dpc_protocol.pcm_core import InstructionBlock, InstructionSet

from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.managers.prompt_manager import PromptManager
from tests.test_a_peer_routed_query_carries_only_the_context_its_node_rules_allow import (
    ALLOWED_PEER,
    DENIED_PEER,
    _FakeService,
    _device_context,
    _personal_context,
    _run,
)

GENERAL = "GENERAL-INSTRUCTION-MARKER"
CODER = "CODER-INSTRUCTION-MARKER"
GROUP_PEER = "dpc-node-dave-in-friends"
NAMED_SET_PEER = "dpc-node-frank-general-only"


def _instructions():
    return InstructionSet(
        default="general",
        sets={
            "general": InstructionBlock(name="General", primary=GENERAL),
            "coder": InstructionBlock(name="Coder", primary=CODER),
        },
    )


@pytest.fixture
def firewall(tmp_path):
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({
        "node_groups": {"friends": [GROUP_PEER]},
        "groups": {"friends": {"instructions.json:*": "allow"}},
        "nodes": {
            ALLOWED_PEER: {
                "personal.json:profile": "allow",
                "personal.json:knowledge": "deny",
                "device_context.json:software.*": "allow",
            },
            NAMED_SET_PEER: {"instructions.json:general.primary": "allow"},
        },
    }))
    return ContextFirewall(rules)


def _service(firewall, monitor_set=None):
    service = _FakeService(firewall, _personal_context(), _device_context())
    service.prompt_manager = PromptManager(_instructions(), {})
    service.monitor.instruction_set_name = monitor_set
    return service


@pytest.mark.asyncio
async def test_a_peer_with_no_instruction_rule_gets_no_instruction_text(firewall, caplog):
    service = _service(firewall)

    with caplog.at_level(logging.INFO, logger="dpc_client_core.service"):
        where, peer_id, prompt = await _run(service, DENIED_PEER)

    assert (where, peer_id) == ("peer", DENIED_PEER)
    assert GENERAL not in prompt
    assert prompt.startswith("\n\n--- CONTEXTUAL DATA ---")  # an empty system instruction
    withheld = [r for r in caplog.records if "withheld" in r.getMessage()]
    assert len(withheld) == 1
    assert withheld[0].levelno == logging.INFO
    assert DENIED_PEER in withheld[0].getMessage()
    assert "'general'" in withheld[0].getMessage()
    assert GENERAL not in withheld[0].getMessage()


@pytest.mark.asyncio
async def test_a_peer_with_context_rules_alone_still_gets_its_context_and_no_instruction(firewall):
    service = _service(firewall)

    _, _, prompt = await _run(service, ALLOWED_PEER)

    assert GENERAL not in prompt
    assert "Alice Visible" in prompt
    assert "SHARED-OS-MARKER" in prompt
    assert "PRIVATE-KNOWLEDGE-MARKER" not in prompt
    assert "PRIVATE-GPU-MARKER" not in prompt


@pytest.mark.asyncio
async def test_a_node_group_allowing_every_instruction_set_gets_the_text(firewall):
    service = _service(firewall)

    _, peer_id, prompt = await _run(service, GROUP_PEER)

    assert peer_id == GROUP_PEER
    assert prompt.startswith(GENERAL)


@pytest.mark.asyncio
async def test_a_node_rule_on_one_set_allows_that_set_and_no_other(firewall):
    _, _, general_prompt = await _run(_service(firewall), NAMED_SET_PEER)
    _, _, coder_prompt = await _run(_service(firewall, monitor_set="coder"), NAMED_SET_PEER)

    assert general_prompt.startswith(GENERAL)
    assert CODER not in coder_prompt
    assert GENERAL not in coder_prompt


@pytest.mark.asyncio
async def test_a_rule_check_that_raises_sends_no_instruction_text(firewall, caplog):
    def _broken(*args, **kwargs):
        raise RuntimeError("rules unreadable")

    service = _service(firewall)
    firewall.can_access = _broken

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.service"):
        _, _, prompt = await _run(service, GROUP_PEER)

    assert GENERAL not in prompt
    assert any("no instruction text" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_local_inference_keeps_the_instruction_text(firewall):
    where, _, prompt = await _run(_service(firewall, monitor_set="coder"), None)

    assert where == "local"
    assert prompt.startswith(CODER)
