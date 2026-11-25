"""The room axis (ADR-048) gaps Zcode's review of ADR-053 found, closed as the room closes
them (no cards): transcripts/ and output/ are mixed stores; a task's result and error, a
process's command line and the device context are the owner's to see. Plus the picker's
backend half (acceptance check 15): a name not in node_groups is refused on save, and the
next turn reads the saved group.
"""
import types

from dpc_client_core.dpc_agent.agent import ASKER_HUMAN, OTHER_NODE_TURN, Asker, DpcAgent
from dpc_client_core.dpc_agent.tools import core as tools_core
from dpc_client_core.dpc_agent.tools import skills as skills_tools
from dpc_client_core.dpc_agent.tools.registry import ToolContext

FRIEND = Asker(ASKER_HUMAN, "Miron", "dpc-node-friend", False, True)


def _mixed(tmp_path):
    return ToolContext(agent_root=tmp_path, message_source="group", turn_class=OTHER_NODE_TURN,
                       open_layers=frozenset(), asker=FRIEND)


def _owner(tmp_path):
    return ToolContext(agent_root=tmp_path, message_source="ui", turn_class="owner",
                       open_layers=frozenset({"L5", "L6", "EXT"}))


def test_transcripts_and_output_are_closed_to_a_mixed_room(tmp_path):
    ctx = _mixed(tmp_path)
    for folder in ("transcripts", "output"):
        target = (tmp_path / folder / "x.txt").resolve()
        assert "mix private memory" in (tools_core.closed_to_this_turn(ctx, target) or ""), folder
        assert tools_core.closed_to_this_turn(_owner(tmp_path), target) is None


def test_a_tasks_result_and_error_are_withheld_from_a_mixed_room(tmp_path):
    task = types.SimpleNamespace(id="t1", task_type="chat", status="completed", created_at="a",
                                 scheduled_at=None, started_at=None, completed_at="b",
                                 result="the owner's private notes say X", error="trace with C:/secret",
                                 retry_count=0)
    queue = types.SimpleNamespace(get_task=lambda tid: task)
    ctx = _mixed(tmp_path)
    ctx._agent = types.SimpleNamespace(queue=queue)
    shown = tools_core.get_task_status(ctx, "t1")
    assert "private notes" not in shown and "secret" not in shown and "withheld" in shown
    owner = _owner(tmp_path)
    owner._agent = ctx._agent
    assert "private notes" in tools_core.get_task_status(owner, "t1")


def test_process_command_lines_are_withheld_from_a_mixed_room(tmp_path, monkeypatch):
    rows = [{"agent_id": tmp_path.name, "pid": 7, "command": "python secret_script.py --token abc",
             "ended_at": None, "started_at": "x", "conversation_id": "c", "tool": "run_shell", "round": 1}]
    from dpc_client_core import process_registry
    monkeypatch.setattr(process_registry, "get_registry",
                        lambda: types.SimpleNamespace(list_processes=lambda: rows))
    shown = skills_tools._list_my_processes(_mixed(tmp_path))
    assert "secret_script" not in shown and "withheld" in shown
    assert "secret_script" in skills_tools._list_my_processes(_owner(tmp_path))


def test_the_device_context_is_not_read_in_a_mixed_room(tmp_path):
    assert "Device context is not read on this turn" in tools_core.get_dpc_context(_mixed(tmp_path), "device")


def test_a_name_not_in_node_groups_is_refused_on_save():
    from dpc_client_core.firewall import ContextFirewall
    config = {"node_groups": {"my_machines": ["dpc-node-x"]},
              "agent_profiles": {"agent_ark": {"memory": {"own_node_group": "typo"}}}}
    ok, errors = ContextFirewall.validate_config(config)
    assert not ok
    assert any("own_node_group" in str(e) and "typo" in str(e) for e in errors)
    config["agent_profiles"]["agent_ark"]["memory"]["own_node_group"] = "my_machines"
    ok, errors = ContextFirewall.validate_config(config)
    assert not any("own_node_group" in str(e) for e in errors)


def test_the_next_turn_reads_the_saved_own_node_group(tmp_path):
    profile = {"memory": {}}
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = tmp_path
    agent._firewall_profile = "agent_ark"
    agent._firewall = types.SimpleNamespace(node_groups={"my_machines": ["dpc-node-laptop"]},
                                            get_agent_profile_settings=lambda name: profile)
    agent._service = types.SimpleNamespace(group_manager=types.SimpleNamespace(node_id="dpc-node-this"))
    laptop = Asker(ASKER_HUMAN, "Mike", "dpc-node-laptop", False, True)
    assert not agent.resolve_asker(laptop, "group").is_owner
    profile["memory"]["own_node_group"] = "my_machines"  # the picker's save
    assert agent.resolve_asker(laptop, "group").is_owner
