"""Follow-up 13 (Zcode #154 P3s on ADR-053): three holes in "who asked".

- A context with no asker, no source and no turn class passed the gate as the owner's. The
  absence of three fields is now no turn at all and fails closed; a builder that means an
  internal call says so with `ToolContext.internal_call`, and a real asker still wins over it.
- run_shell and comfyui_submit called directly (bypassing ToolRegistry.execute) skipped the
  card. Each handler now checks again: a foreign asker runs only with the gate's per-call mark.
- `resolve_asker` returned a stored record with `is_owner: true` as it was. The flag is now
  recomputed from the node, the verdict and the own node group, every time.
"""
import types

import pytest

from dpc_client_core.dpc_agent import owner_approval
from dpc_client_core.dpc_agent.agent import (
    ASKER_AGENT, ASKER_HUMAN, ASKER_UNKNOWN, OTHER_NODE_TURN, Asker, DpcAgent,
)
from dpc_client_core.dpc_agent.tools import comfyui, shell
from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolRegistry

FRIEND = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=False, verified=True)
MIKE = Asker(ASKER_HUMAN, "Mike", "dpc-node-this", is_owner=True, verified=True)


# -- a context no turn made ----------------------------------------------------------------

def test_a_context_with_no_asker_no_source_and_no_class_is_not_the_owner(tmp_path):
    assert owner_approval.asker_is_owner(ToolContext(agent_root=tmp_path)) is False
    assert owner_approval.asker_is_owner(None) is False


def test_an_internal_call_says_so_with_its_marker(tmp_path):
    ctx = ToolContext(agent_root=tmp_path, internal_call=True)
    assert owner_approval.asker_is_owner(ctx) is True


def test_the_marker_never_outranks_a_real_asker(tmp_path):
    ctx = ToolContext(agent_root=tmp_path, internal_call=True, asker=FRIEND)
    assert owner_approval.asker_is_owner(ctx) is False
    ctx = ToolContext(agent_root=tmp_path, internal_call=True, message_source="telegram")
    assert owner_approval.asker_is_owner(ctx) is False


def test_a_flagged_tool_on_a_bare_context_goes_to_the_owner_and_runs_nothing(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(shell, "_execute_shell_command", lambda *a, **k: ran.append(a) or "ran")
    registry = ToolRegistry(agent_root=tmp_path)
    owner_approval._reset_for_tests()
    result = registry.execute("run_shell", {"command": "echo hi"}, ctx=ToolContext(agent_root=tmp_path))
    assert result.startswith("⚠️") and not ran


def test_the_registrys_default_context_is_not_marked_internal(tmp_path):
    registry = ToolRegistry(agent_root=tmp_path)
    assert registry._ctx.internal_call is False


# -- the second check of run_shell and comfyui_submit -----------------------------------------

def _foreign_ctx(tmp_path, asker=FRIEND):
    return ToolContext(agent_root=tmp_path, message_source="group", turn_class=OTHER_NODE_TURN,
                       open_layers=frozenset(), asker=asker)


def _owner_ctx(tmp_path):
    return ToolContext(agent_root=tmp_path, message_source="ui", turn_class="owner",
                       open_layers=frozenset({"L5", "L6", "EXT"}), asker=MIKE)


@pytest.fixture
def shell_runs(monkeypatch):
    ran = []
    monkeypatch.setattr(shell, "_execute_shell_command",
                        lambda command, *a, **k: ran.append(command) or "ran")
    return ran


def test_run_shell_called_directly_for_a_foreign_asker_refuses(tmp_path, shell_runs):
    result = shell.run_shell(_foreign_ctx(tmp_path), "echo hi")
    assert result.startswith("⚠️") and "Miron" in result and "not the owner" in result
    assert shell_runs == []


def test_run_shell_runs_for_a_foreign_asker_with_the_gates_approval_of_this_call(tmp_path, shell_runs):
    token = owner_approval._CALL_MARK.set(owner_approval.MARK_APPROVED)
    try:
        assert shell.run_shell(_foreign_ctx(tmp_path), "echo hi") == "ran"
    finally:
        owner_approval._CALL_MARK.reset(token)
    assert shell.run_shell(_foreign_ctx(tmp_path), "echo hi").startswith("⚠️"), \
        "the mark is per call"


def test_run_shell_runs_for_the_owner(tmp_path, shell_runs):
    assert shell.run_shell(_owner_ctx(tmp_path), "echo hi") == "ran"


def test_a_tier_two_command_keeps_its_fixed_refusal_for_a_foreign_asker(tmp_path, shell_runs):
    result = shell.run_shell(_foreign_ctx(tmp_path), "shutdown /s /t 0")
    assert result.startswith("⛔") and shell_runs == []


def test_comfyui_submit_called_directly_for_a_foreign_asker_refuses(tmp_path):
    ctx = _foreign_ctx(tmp_path)
    ctx.agent_event_loop = object()
    result = comfyui.comfyui_submit(ctx, workflow_json={"1": {"class_type": "SaveImage"}})
    assert result.startswith("⚠️") and "Miron" in result and "not the owner" in result


def test_comfyui_submit_passes_its_second_check_with_the_gates_approval(tmp_path):
    ctx = _foreign_ctx(tmp_path)
    ctx.agent_event_loop = None  # stops right after the check, before any request
    token = owner_approval._CALL_MARK.set(owner_approval.MARK_APPROVED)
    try:
        result = comfyui.comfyui_submit(ctx, workflow_json={"1": {"class_type": "SaveImage"}})
    finally:
        owner_approval._CALL_MARK.reset(token)
    assert "no event loop" in result


# -- a stored asker's is_owner is recomputed --------------------------------------------------

def _agent(this_node="dpc-node-this", own_group=None):
    agent = DpcAgent.__new__(DpcAgent)
    gm = types.SimpleNamespace(node_id=this_node)
    agent._service = types.SimpleNamespace(group_manager=gm, p2p_manager=gm)
    groups = {"mine": list(own_group or [])}
    agent._firewall = types.SimpleNamespace(
        node_groups=groups,
        get_agent_profile_settings=lambda profile: {"memory": {"own_node_group": "mine"}},
    )
    agent._firewall_profile = "agent_ark"
    return agent


def test_a_stored_owner_flag_on_a_foreign_node_is_not_trusted():
    forged = Asker.from_record({"kind": "human", "name": "Miron", "node_id": "dpc-node-friend",
                                "is_owner": True, "verified": True})
    assert _agent().resolve_asker(forged, "group").is_owner is False


def test_a_node_taken_out_of_the_own_node_group_is_no_longer_the_owner():
    stored = Asker(ASKER_HUMAN, "Mike", "dpc-node-laptop", is_owner=True, verified=True)
    assert _agent(own_group=["dpc-node-laptop"]).resolve_asker(stored, "group").is_owner is True
    assert _agent(own_group=[]).resolve_asker(stored, "group").is_owner is False


def test_a_stored_owner_flag_that_is_not_verified_is_not_trusted():
    stored = Asker(ASKER_UNKNOWN, "Mike?", "dpc-node-this", is_owner=True, verified=False)
    assert _agent().resolve_asker(stored, "group").is_owner is False


def test_this_nodes_own_post_is_the_owner_and_keeps_its_relay_chain():
    stored = Asker(ASKER_AGENT, "Ark", "dpc-node-this", is_owner=False, verified=True, via=("Ark",))
    resolved = _agent().resolve_asker(stored, "group")
    assert resolved.is_owner is True and resolved.via == ("Ark",)


def test_the_owners_door_is_the_owner_and_a_foreign_node_at_it_is_not():
    agent = _agent()
    assert agent.resolve_asker(None, "ui").is_owner is True
    assert agent.resolve_asker(Asker(ASKER_HUMAN, "the owner", "", True, True), "ui").is_owner is True
    forged = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", is_owner=True, verified=True)
    assert agent.resolve_asker(forged, "ui").is_owner is False


def test_a_foreign_door_never_makes_an_owner_from_a_stored_flag():
    stored = Asker(ASKER_HUMAN, "stranger", "", is_owner=True, verified=True)
    assert _agent().resolve_asker(stored, "telegram").is_owner is False
