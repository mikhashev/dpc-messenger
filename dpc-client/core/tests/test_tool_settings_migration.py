"""A tools block should hold tool names, and it held three kinds of thing.

`run_shell_group_allowed` and `run_shell_tier1_whitelist` are settings *of* a tool, and
they sat in the same dict as the tools themselves under compound names. Every reader had
to know which names were not tools, and they disagreed: the validator skipped two of them
by name, the tools map coerced them to booleans, and the group-chat gate built the name
from a tool name at runtime — so a setting for any other tool was classified by nobody.

The migration moves them to `tool_settings.<tool>.<setting>`. What has to be proved is
not that values arrive there, but that the move changes no permission: the group gate and
the Tier 1 whitelist have always read the agent's own profile and never the global block,
and inheriting the global `run_shell_group_allowed: true` would hand run_shell to every
profile-less agent in group chats. That is the last test here.
"""
from __future__ import annotations

import json

import pytest

from dpc_client_core.firewall import TOOL_SETTINGS_KEY, ContextFirewall, _is_tool_key


@pytest.fixture
def firewall(tmp_path):
    path = tmp_path / "privacy_rules.json"
    path.write_text(json.dumps({"dpc_agent": {"tools": {}}}), encoding="utf-8")
    return ContextFirewall(path)


def test_compound_keys_move_and_the_tools_block_is_left_with_tools(firewall):
    firewall.rules = {
        "dpc_agent": {
            "tools": {
                "run_shell": False,
                "run_shell_group_allowed": True,
                "_comment": "kept",
            }
        },
        "agent_profiles": {
            "agent_x": {
                "tools": {
                    "run_shell": True,
                    "run_shell_tier1_whitelist": ["git status"],
                    "browse_page_group_allowed": False,
                }
            }
        },
    }

    assert firewall._migrate_tool_settings_out_of_tools() is True

    global_block = firewall.rules["dpc_agent"]
    assert global_block["tools"] == {"run_shell": False, "_comment": "kept"}
    assert global_block[TOOL_SETTINGS_KEY]["run_shell"]["group_allowed"] is True

    profile = firewall.rules["agent_profiles"]["agent_x"]
    assert profile["tools"] == {"run_shell": True}
    assert profile[TOOL_SETTINGS_KEY]["run_shell"]["tier1_whitelist"] == ["git status"]
    # The `_group_allowed` convention is open to every tool, not just run_shell.
    assert profile[TOOL_SETTINGS_KEY]["browse_page"]["group_allowed"] is False

    # Idempotent: a migrated file is not rewritten on every startup.
    assert firewall._migrate_tool_settings_out_of_tools() is False


def test_group_allowed_suffix_is_never_mistaken_for_a_tool():
    assert _is_tool_key("run_shell") is True
    assert _is_tool_key("browse_page_group_allowed") is False
    assert _is_tool_key("run_shell_tier1_whitelist") is False
    assert _is_tool_key("_comment") is False


def test_setting_is_read_from_the_new_place_and_from_the_old_one(firewall):
    firewall.rules = {
        "dpc_agent": {"tools": {}},
        "agent_profiles": {
            "migrated": {TOOL_SETTINGS_KEY: {"run_shell": {"group_allowed": True}}},
            "legacy": {"tools": {"run_shell_group_allowed": True}},
        },
    }

    assert firewall.get_tool_setting("run_shell", "group_allowed", "migrated") is True
    # A hand-edited or restored file that predates the migration still answers.
    assert firewall.get_tool_setting("run_shell", "group_allowed", "legacy") is True
    assert firewall.get_tool_setting("run_shell", "group_allowed", "absent", default="x") == "x"


def test_the_global_value_does_not_leak_into_agents_that_never_set_it(firewall):
    """The move must not turn a global default into an inherited permission."""
    firewall.rules = {
        "dpc_agent": {TOOL_SETTINGS_KEY: {"run_shell": {"group_allowed": True}}},
        "agent_profiles": {"agent_x": {"tools": {"run_shell": True}}},
    }

    # Both readers of this setting have always been profile-only. Inheriting here
    # would allow run_shell in group chats for every agent without its own value.
    assert firewall.get_tool_setting(
        "run_shell", "group_allowed", "agent_x", default=False) is False
    assert firewall.get_tool_setting(
        "run_shell", "group_allowed", None, default=False) is False
    # Inheritance is available, but only when a caller asks for it explicitly.
    assert firewall.get_tool_setting(
        "run_shell", "group_allowed", "agent_x", default=False, inherit_global=True) is True
