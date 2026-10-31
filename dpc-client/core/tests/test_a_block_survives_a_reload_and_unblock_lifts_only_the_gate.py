"""A block survives a reload and a restart; Unblock lifts the gate and restores nothing.

`blocked_nodes` lives in privacy_rules.json, so it outlives the process and the
hot reload (`ContextFirewall.reload`, what `reload_firewall` and every save
run). Unblock removes the id from the list and nothing else: the groups and
rules Block deleted come back only by hand (Mike's call, 2026-10-31).

Real `ContextFirewall` over a rules file in tmp_path.
"""

import json

from dpc_client_core.firewall import ContextFirewall

BLOCKED = "dpc-node-" + "b" * 32


def _firewall(tmp_path):
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps({
        "nodes": {BLOCKED: {"personal.json:*": "allow"}},
        "node_groups": {"friends": [BLOCKED]},
    }))
    return ContextFirewall(path)


def test_the_block_is_read_back_by_a_reload_and_by_a_new_process(tmp_path):
    firewall = _firewall(tmp_path)
    firewall.block_node(BLOCKED)

    assert firewall.reload()[0]
    assert firewall.is_blocked(BLOCKED)
    assert ContextFirewall(firewall.access_file_path).is_blocked(BLOCKED), "a restart reads it"


def test_a_hand_edit_picked_up_by_reload_blocks_too(tmp_path):
    firewall = _firewall(tmp_path)
    assert not firewall.is_blocked(BLOCKED)

    rules = firewall.get_rules_as_dict()
    rules["blocked_nodes"] = [BLOCKED]
    firewall.access_file_path.write_text(json.dumps(rules))
    assert firewall.reload()[0]

    assert firewall.is_blocked(BLOCKED)


def test_unblock_removes_the_id_and_restores_no_rule(tmp_path):
    firewall = _firewall(tmp_path)
    firewall.block_node(BLOCKED)

    ok, _message = firewall.unblock_node(BLOCKED)

    assert ok
    assert not firewall.is_blocked(BLOCKED)
    on_disk = firewall.get_rules_as_dict()
    assert on_disk["blocked_nodes"] == []
    assert on_disk["nodes"] == {}
    assert on_disk["node_groups"] == {"friends": []}
    assert not ContextFirewall(firewall.access_file_path).is_blocked(BLOCKED)


def test_unblocking_a_node_that_is_not_blocked_is_refused(tmp_path):
    firewall = _firewall(tmp_path)

    ok, message = firewall.unblock_node(BLOCKED)

    assert not ok
    assert "not blocked" in message
