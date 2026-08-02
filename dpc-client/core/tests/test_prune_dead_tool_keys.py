"""Seeding only ever added, so names that left the code never left the config.

`claude_code_edit`, `repo_commit_push`, `extract_links`, `transcribe_audio` sat in every
`tools` block long after the tools stopped existing (the last one was never an agent tool
at all — it is a WebSocket command). They were inert, but they made the file unreadable
as a statement of what an agent may do, and they were what made the profiles look out of
sync with each other.

Pruning them is only safe while the registry is a complete picture. It is not one when a
tool module fails to import: then "absent from the registry" means "we could not see it",
and the prune would delete real permissions. That refusal is the second test here — and
it is the one worth having, because the destructive branch is the one that has to prove
it stays shut.
"""
from __future__ import annotations

import json

import pytest

from dpc_client_core.firewall import ContextFirewall


def _rules(tools_global: dict, profiles: dict | None = None) -> dict:
    return {
        "dpc_agent": {"enabled": True, "tools": dict(tools_global)},
        "agent_profiles": profiles or {},
    }


def _write(tmp_path, rules) -> ContextFirewall:
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    return ContextFirewall(path)


@pytest.fixture
def firewall(tmp_path):
    """A firewall over a throwaway rules file, with no reconciliation applied yet."""
    return _write(tmp_path, _rules({"list_dir": True}))


def test_dead_keys_go_and_everything_else_stays(firewall):
    # State the precondition instead of inheriting it from the machine: the
    # fixture built a real registry, and on an install missing an optional
    # extra that registry carries load failures and the prune stands down —
    # correctly, but this test is about what the prune deletes when it runs.
    firewall._registry_load_failures = 0
    firewall.rules = _rules(
        {
            "_comment": "keep me",
            "list_dir": True,
            "claude_code_edit": False,
            "transcribe_audio": False,
            "run_shell_group_allowed": True,
        },
        {
            "agent_x": {
                "tools": {
                    "list_dir": False,
                    "repo_commit_push": True,
                    "run_shell_tier1_whitelist": ["git status"],
                }
            },
            "no_tools_block": {"enabled": True},
        },
    )

    assert firewall._prune_dead_tool_keys({"list_dir": True, "run_shell": False}) is True

    global_tools = firewall.rules["dpc_agent"]["tools"]
    assert "claude_code_edit" not in global_tools
    assert "transcribe_audio" not in global_tools
    # A comment and a run_shell setting are not tool names and are not the prune's business.
    assert global_tools["_comment"] == "keep me"
    assert global_tools["run_shell_group_allowed"] is True
    assert global_tools["list_dir"] is True

    profile_tools = firewall.rules["agent_profiles"]["agent_x"]["tools"]
    assert "repo_commit_push" not in profile_tools
    assert profile_tools["run_shell_tier1_whitelist"] == ["git status"]
    # The user's own value for a live tool survives — the prune is not a reset.
    assert profile_tools["list_dir"] is False


def test_nothing_is_pruned_when_a_tool_module_failed_to_load(firewall):
    firewall.rules = _rules({"list_dir": True, "browser_click": True})
    firewall._registry_load_failures = 1

    # `browser_click` is missing from the defaults only because its module did not
    # import. Deleting it here would silently revoke a permission the user granted.
    assert firewall._prune_dead_tool_keys({"list_dir": True}) is False
    assert firewall.rules["dpc_agent"]["tools"]["browser_click"] is True


def test_empty_registry_prunes_nothing(firewall):
    firewall.rules = _rules({"list_dir": True})
    assert firewall._prune_dead_tool_keys({}) is False
    assert firewall.rules["dpc_agent"]["tools"] == {"list_dir": True}


def test_reconciliation_seeds_and_prunes_in_one_pass_and_persists(tmp_path):
    """The startup path does both directions and writes the result once.

    This one builds a real registry, so it is the only test here whose
    expectation depends on the environment: where a tool module cannot be
    imported (an install without some optional extra, a broken dependency)
    the prune is *supposed* to stand down. Asserting "the dead key is gone"
    unconditionally would fail there — and fail for the one reason that
    means the code did the right thing. So assert the branch that applies.
    """
    from dpc_client_core.dpc_agent.tools.registry import ToolRegistry

    registry_is_complete = not ToolRegistry().load_failures

    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps(_rules({"claude_code_edit": False})), encoding="utf-8")
    firewall = ContextFirewall(path)

    on_disk = json.loads(path.read_text(encoding="utf-8"))["dpc_agent"]["tools"]
    # Seeding runs either way: adding a key is safe with an incomplete registry.
    assert "list_dir" in on_disk, "seeding did not run alongside the prune"
    assert firewall.dpc_agent_tools.get("list_dir") is not None

    if registry_is_complete:
        assert "claude_code_edit" not in on_disk, "dead key survived startup reconciliation"
    else:
        assert "claude_code_edit" in on_disk, (
            "prune ran while tool modules were missing — it must stand down instead"
        )
