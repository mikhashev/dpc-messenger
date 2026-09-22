"""Which agent tools a benchmark run may use, decided in one place.

An allow list, not a deny list: the firewall seeds a tool missing from the
rules with that tool's own default, which is True for anything registered as
safe — so a deny list lets a tool registered tomorrow arrive switched on in a
benchmark nobody re-read. Here every registered tool is written down and only
the listed ones say True.

What is on is what a public task needs: the web, files inside the task root, a
shell, documents, images and audio, and the agent's own task-root memory and
skills. What is off reads or reaches the operator rather than the task:
session archives, chat history and `get_dpc_context` (conversations and
personal files), messaging, scheduling and the task board, git, comfyui,
identity, other agents' skills, and `list_auth_domains` (the operator's vault).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

BENCHMARK_TOOLS = frozenset({
    "search_web", "browse_page", "fetch_json", "check_url",
    "browser_navigate", "browser_click", "browser_fill", "browser_select",
    "browser_scroll", "browser_wait_for", "browser_snapshot", "browser_extract",
    "browser_screenshot", "browser_collect", "browser_download",
    "browser_switch_tab", "browser_close",
    "read_file", "write_file", "list_dir", "search_files", "search_in_file",
    "read_document", "describe_image", "transcribe_audio_file", "run_shell",
    "update_scratchpad", "memory_search", "knowledge_list",
    "execute_skill", "list_my_skills", "list_my_tools",
})

# The shape of a rules file with no sandbox extensions: the agent's own root is
# the whole sandbox, so the repository, the hub cache and the results are out.
_NO_EXTENSIONS = {
    "read_only": [], "read_write": [],
    "extended_read_enabled": False, "extended_write_enabled": False,
}


def tool_map(allowed: Iterable[str] = BENCHMARK_TOOLS,
             registered: Optional[Iterable[str]] = None) -> Dict[str, bool]:
    """Every registered tool name -> whether the benchmark may use it."""
    if registered is None:
        from dpc_client_core.dpc_agent.tools.registry import ToolRegistry
        registered = ToolRegistry()._entries
    allowed = frozenset(allowed)
    return {name: name in allowed for name in registered}


def benchmark_firewall(workdir: Path, profile: str,
                       template: Optional[Dict[str, Any]] = None,
                       allowed: Iterable[str] = BENCHMARK_TOOLS):
    """A ContextFirewall owned by the run, never the operator's rules file.

    `template` is a privacy_rules dict (the GAIA run passes its own
    `benchmark_rules.json`); without one the profile gets no sandbox extensions.
    The copy lands in `workdir` because the firewall writes back what it
    reconciles, and that write must not reach `~/.dpc/privacy_rules.json`.
    """
    from dpc_client_core.firewall import ContextFirewall

    rules = json.loads(json.dumps(template)) if template else {
        "dpc_agent": {"tools": {}, "sandbox_extensions": dict(_NO_EXTENSIONS)},
        "agent_profiles": {},
    }
    rules.setdefault("agent_profiles", {}).setdefault(
        profile, {"tools": {}, "sandbox_extensions": dict(_NO_EXTENSIONS)})
    tools = tool_map(allowed)
    rules["dpc_agent"]["tools"] = dict(tools)
    rules["agent_profiles"][profile]["tools"] = dict(tools)

    local = Path(workdir) / "privacy_rules.json"
    local.write_text(json.dumps(rules, indent=2), encoding="utf-8")
    return ContextFirewall(local)
