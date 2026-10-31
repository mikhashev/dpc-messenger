"""Blocking a node removes every rule that names it and keeps it only in `blocked_nodes`.

Mike's call, 2026-10-31: Block writes the id into a top-level `blocked_nodes`
list in privacy_rules.json and deletes every other entry that is its id — a key,
a value or a list item, walked over the whole structure, not over a list of
sections, so a section added later is covered too. A string that only contains
the id (a sandbox path to that peer's files folder) is not a rule and stays. The deletion is not undone by Unblock. Another node's rules are
left exactly as they were.

Real `ContextFirewall` over a rules file in tmp_path, never ~/.dpc.
"""

import json

import pytest

from dpc_client_core.firewall import ContextFirewall

BLOCKED = "dpc-node-" + "b" * 32
OTHER = "dpc-node-" + "c" * 32
SANDBOX_PATH = f"C:\\Users\\someone\\.dpc\\conversations\\{BLOCKED}\\files\\"


def _rules():
    """A rules file that names BLOCKED in every place the walk has to reach."""
    return {
        "hub": {"personal.json:profile.name": "allow"},
        "node_groups": {"friends": [BLOCKED, OTHER], "family": [BLOCKED]},
        "nodes": {
            BLOCKED: {"personal.json:*": "allow"},
            OTHER: {"personal.json:profile.*": "allow"},
        },
        "compute": {
            "enabled": True,
            "allow_nodes": [BLOCKED, OTHER],
            "allow_groups": [],
            "allowed_models": [],
            "free_nodes": [BLOCKED],
        },
        "transcription": {
            "enabled": True,
            "allow_nodes": [BLOCKED],
            "send_to_nodes": [OTHER, BLOCKED],
        },
        "file_transfer": {
            "nodes": {
                BLOCKED: {"file_transfer.allow": "allow"},
                OTHER: {"file_transfer.allow": "allow"},
            },
        },
        "image_transfer": {"allowed_sources": ["clipboard"], "max_size_mb": 100},
        "agent_profiles": {
            "agent_001": {
                "owner_hint": BLOCKED,
                "keep": "yes",
                # A path that contains the id names a folder, not the node: Block
                # of the node whose files it holds must leave it whole.
                "sandbox_extensions": {
                    "read_only": [SANDBOX_PATH, "C:/work"],
                    "read_write": [SANDBOX_PATH],
                    "default_dir": SANDBOX_PATH,
                },
            },
        },
    }


@pytest.fixture
def firewall(tmp_path):
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps(_rules(), indent=2))
    return ContextFirewall(path)


def _names(value, node_id):
    """Whether any key, value or list item anywhere in `value` IS `node_id`."""
    if isinstance(value, dict):
        return any(k == node_id or _names(v, node_id) for k, v in value.items())
    if isinstance(value, list):
        return any(_names(v, node_id) for v in value)
    return value == node_id


def test_block_leaves_the_id_only_in_blocked_nodes(firewall):
    ok, _message, removed = firewall.block_node(BLOCKED)

    assert ok
    on_disk = firewall.get_rules_as_dict()
    assert on_disk["blocked_nodes"] == [BLOCKED]
    rest = {k: v for k, v in on_disk.items() if k != "blocked_nodes"}
    assert not _names(rest, BLOCKED), json.dumps(rest, indent=2)
    assert firewall.is_blocked(BLOCKED)


def test_a_sandbox_path_that_contains_the_id_is_left_whole(firewall):
    """Johnny's case, 2026-10-31: Ark's sandbox paths to the linux node's files
    folder carry its id; cutting the id out of them would point Ark at nothing."""
    before = firewall.get_rules_as_dict()["agent_profiles"]["agent_001"]["sandbox_extensions"]

    firewall.block_node(BLOCKED)

    after = firewall.get_rules_as_dict()["agent_profiles"]["agent_001"]["sandbox_extensions"]
    assert after == before == {"read_only": [SANDBOX_PATH, "C:/work"], "read_write": [SANDBOX_PATH],
                               "default_dir": SANDBOX_PATH}


def test_block_reports_the_path_of_each_removed_rule(firewall):
    _ok, _message, removed = firewall.block_node(BLOCKED)

    assert sorted(removed) == sorted([
        "node_groups.friends",
        "node_groups.family",
        f"nodes.{BLOCKED}",
        "compute.allow_nodes",
        "compute.free_nodes",
        "transcription.allow_nodes",
        "transcription.send_to_nodes",
        f"file_transfer.nodes.{BLOCKED}",
        "agent_profiles.agent_001.owner_hint",
    ])


def test_another_nodes_rules_stay_exactly_as_they_were(firewall):
    """The positive control: the walk takes the blocked id and nothing beside it."""
    firewall.block_node(BLOCKED)

    on_disk = firewall.get_rules_as_dict()
    assert on_disk["node_groups"] == {"friends": [OTHER], "family": []}
    assert on_disk["nodes"] == {OTHER: {"personal.json:profile.*": "allow"}}
    assert on_disk["compute"]["allow_nodes"] == [OTHER]
    assert on_disk["transcription"]["send_to_nodes"] == [OTHER]
    assert on_disk["file_transfer"]["nodes"] == {OTHER: {"file_transfer.allow": "allow"}}
    assert on_disk["agent_profiles"]["agent_001"]["keep"] == "yes"
    assert "owner_hint" not in on_disk["agent_profiles"]["agent_001"]
    assert on_disk["image_transfer"] == _rules()["image_transfer"]
    assert not firewall.is_blocked(OTHER)


def test_the_block_wins_over_the_allow_it_could_not_find(tmp_path):
    """An allow a later hand-edit writes back is still beaten by the block."""
    path = tmp_path / "privacy_rules.json"
    rules = _rules()
    rules["blocked_nodes"] = [BLOCKED]
    path.write_text(json.dumps(rules))

    firewall = ContextFirewall(path)

    assert firewall.is_blocked(BLOCKED)
    assert BLOCKED in firewall.compute_allowed_nodes, "the allow is there"


def test_the_save_leaves_no_half_file_behind(firewall):
    """Written beside the file and moved over it: no temp file stays, the file parses."""
    firewall.block_node(BLOCKED)

    leftovers = [p.name for p in firewall.access_file_path.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    json.loads(firewall.access_file_path.read_text())


def test_a_new_rules_file_starts_with_an_empty_block_list(tmp_path):
    firewall = ContextFirewall(tmp_path / "privacy_rules.json")

    assert firewall.get_rules_as_dict()["blocked_nodes"] == []
    assert firewall.blocked_nodes == set()


def test_the_example_rules_file_carries_the_key_and_validates():
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / "privacy_rules.example.json"
    rules = json.loads(example.read_text(encoding="utf-8"))

    assert rules["blocked_nodes"] == []
    assert ContextFirewall.validate_config({"blocked_nodes": rules["blocked_nodes"]})[0]
