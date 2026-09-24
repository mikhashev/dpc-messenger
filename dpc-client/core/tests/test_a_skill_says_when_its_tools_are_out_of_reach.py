"""A skill is not offered as usable to an agent that cannot call its tools.

The skills section listed every skill in the agent's home and never read
required_tools, so the former p2p-research starter (needs send_user_message,
now removed — it called request_inference, a tool that was never built) was
offered to eight agents whose firewall refuses that tool, and starter skills
copied before the tool merge kept telling agents to call repo_read and
drive_write. Each skill now carries the reason on its own line; the section
reads the same allowed/all tool sets as the capabilities section. The
sample-skill fixture below stands in for any skill with required_tools —
it is not a starter skill.
"""
from __future__ import annotations

import pathlib

from dpc_client_core.dpc_agent.context import _build_skills_section
from dpc_client_core.dpc_agent.skill_store import SkillStore
from dpc_client_core.dpc_agent.tools.registry import ToolRegistry
from dpc_client_core.firewall import LEGACY_TOOL_ALIASES


def _skill(root: pathlib.Path, name: str, required=None, body: str = "Do the thing.") -> None:
    req = ""
    if required is not None:
        req = "  required_tools:\n" + "".join(f"    - {t}\n" for t in required)
    text = (
        f"---\nname: {name}\nversion: 1\ndescription: >\n  Skill {name}.\n"
        f"metadata:\n  execution_mode: knowledge\n{req}---\n\n{body}\n"
    )
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _line(section: str, name: str) -> str:
    [line] = [l for l in section.splitlines() if l.startswith(f"- **{name}**")]
    return line


ALL = {"send_user_message": False, "read_file": True, "write_file": True}


def test_a_skill_whose_tool_is_disabled_names_it(tmp_path):
    _skill(tmp_path, "sample-skill", ["send_user_message", "read_file"])
    section = _build_skills_section(SkillStore(tmp_path), {"read_file", "write_file"}, ALL)
    assert _line(section, "sample-skill").startswith(
        "- **sample-skill** (needs send_user_message — disabled by firewall): Skill")


def test_the_same_skill_is_plain_where_the_tool_is_enabled(tmp_path):
    _skill(tmp_path, "sample-skill", ["send_user_message"])
    allowed = {"send_user_message", "read_file", "write_file"}
    section = _build_skills_section(SkillStore(tmp_path), allowed, ALL)
    assert _line(section, "sample-skill") == "- **sample-skill**: Skill sample-skill."


def test_no_required_tools_is_no_constraint(tmp_path):
    _skill(tmp_path, "free", required=None)
    _skill(tmp_path, "empty", required=[])
    section = _build_skills_section(SkillStore(tmp_path), set(), ALL)
    assert _line(section, "free") == "- **free**: Skill free."
    assert _line(section, "empty") == "- **empty**: Skill empty."


def test_a_tool_the_firewall_does_not_know_is_called_missing_not_disabled(tmp_path):
    _skill(tmp_path, "sample-skill", ["request_inference"])
    section = _build_skills_section(SkillStore(tmp_path), {"read_file"}, ALL)
    assert "(needs request_inference — no such tool)" in _line(section, "sample-skill")


def test_without_a_firewall_only_retired_names_are_marked(tmp_path):
    _skill(tmp_path, "sample-skill", ["send_user_message"])
    _skill(tmp_path, "old", ["read_file"], body="Call repo_read(path) first.")
    section = _build_skills_section(SkillStore(tmp_path), None, None)
    assert _line(section, "sample-skill") == "- **sample-skill**: Skill sample-skill."
    assert "(references retired tool repo_read)" in _line(section, "old")


def test_a_retired_name_in_the_body_or_the_frontmatter_is_marked_once(tmp_path):
    _skill(tmp_path, "code-analysis", ["repo_read", "search_files"],
           body="Start with repo_list, then repo_read; drive_*, repo_reader and x_repo_read are not tools.")
    allowed = {"search_files", "read_file"}
    line = _line(_build_skills_section(SkillStore(tmp_path), allowed, {**ALL, "search_files": True}),
                 "code-analysis")
    # Retired, not "disabled": turning it on in the firewall would not help.
    assert "(references retired tools repo_list, repo_read)" in line
    assert "disabled by firewall" not in line
    assert "no such tool" not in line


def test_a_retired_name_is_never_a_registered_tool():
    """What lets the marker trust LEGACY_TOOL_ALIASES without asking the registry each turn."""
    assert not (LEGACY_TOOL_ALIASES & set(ToolRegistry()._entries))


def test_the_starter_skills_need_no_retired_tool(tmp_path):
    store = SkillStore(tmp_path)
    store.ensure_starter_skills()
    for s in store.list_skills():
        assert s["retired_tools"] == [], s["name"]


def test_every_starter_skills_required_tool_is_registered(tmp_path):
    """Not-retired is weaker than registered: a name absent from both the
    retired-alias map and the live registry (a typo, a tool that was never
    built) would pass the check above and still fail at call time. Every
    required_tools entry of every starter skill must be a name the live
    ToolRegistry actually holds.
    """
    store = SkillStore(tmp_path)
    store.ensure_starter_skills()
    registered = set(ToolRegistry()._entries)
    for s in store.list_skills():
        for tool_name in s.get("required_tools") or []:
            assert tool_name in registered, f"{s['name']} requires unregistered tool {tool_name!r}"


def test_the_marker_reaches_the_cached_system_block(tmp_path):
    """build_llm_messages hands the firewall's sets to the skills section, not only to capabilities."""
    from dpc_client_core.dpc_agent.context import build_llm_messages
    from dpc_client_core.dpc_agent.memory import Memory

    root = tmp_path / "agent_x"
    Memory(root).ensure_files()
    _skill(root, "sample-skill", ["send_user_message"])
    messages, _ = build_llm_messages(
        agent_root=root, memory=Memory(root), task={"id": "t1", "text": "hi"},
        skill_store=SkillStore(root), allowed_tools={"read_file"}, all_tools=ALL,
    )
    semi_stable = messages[0]["content"][1]["text"]
    assert "- **sample-skill** (needs send_user_message — disabled by firewall):" in semi_stable
