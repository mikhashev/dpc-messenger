"""`blocked_nodes` holds whole node ids and nothing else.

A node id is `dpc-node-` and the first 32 lowercase hex of the SHA-256 of its
public key (dpc_protocol/crypto.py). The save path and the reload refuse
anything else in the list, so a typo cannot become a block that matches
nobody while the user believes the node is kept out.
"""

import json

import pytest

from dpc_client_core.firewall import ContextFirewall

GOOD = "dpc-node-" + "0123456789abcdef" * 2


@pytest.mark.parametrize("value", [
    "dpc-node-alice-123",
    "dpc-node-" + "A" * 32,
    "dpc-node-" + "a" * 31,
    "dpc-node-" + "a" * 33,
    " " + GOOD,
    42,
    None,
])
def test_a_list_entry_that_is_not_a_node_id_is_refused(value):
    ok, errors = ContextFirewall.validate_config({"blocked_nodes": [GOOD, value]})

    assert not ok
    assert any("blocked_nodes" in e for e in errors), errors


def test_a_value_that_is_not_a_list_is_refused():
    ok, errors = ContextFirewall.validate_config({"blocked_nodes": GOOD})

    assert not ok
    assert any("blocked_nodes" in e for e in errors), errors


def test_whole_node_ids_and_an_empty_list_pass():
    assert ContextFirewall.validate_config({"blocked_nodes": [GOOD]}) == (True, [])
    assert ContextFirewall.validate_config({"blocked_nodes": []}) == (True, [])


def test_the_save_refuses_a_bad_entry_and_leaves_the_file_as_it_was(tmp_path):
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps({"blocked_nodes": [GOOD]}))
    firewall = ContextFirewall(path)
    before = path.read_text()

    ok, _message, errors = firewall.save_rules_from_dict({"blocked_nodes": ["dpc-node-x"]})

    assert not ok and errors
    assert path.read_text() == before
    assert firewall.is_blocked(GOOD)


def test_a_reload_refuses_a_hand_edit_with_a_bad_entry(tmp_path):
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps({"blocked_nodes": [GOOD]}))
    firewall = ContextFirewall(path)
    path.write_text(json.dumps({"blocked_nodes": ["not-a-node"]}))

    ok, message = firewall.reload()

    assert not ok and "blocked_nodes" in message
    assert firewall.is_blocked(GOOD), "the rules in force stay in force"
