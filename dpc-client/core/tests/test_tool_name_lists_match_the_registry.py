"""Three places named tools, and only one of them knew which tools exist.

The validator carried its own hand-written set of "valid tool names". It fell behind the
registry and started calling live tools unknown: 24 registered names, 244 warnings in a
single startup, each advising the reader that a real permission was leftover config. The
tier lists drifted the same way in the other direction — `claude_code_edit`,
`request_restart`, `promote_to_stable`, `knowledge_read`, `knowledge_write` named tools
that no module registers.

These tests tie every list back to the registry, so the next tool that is added or
removed cannot leave a stale name behind without something going red.
"""
from __future__ import annotations

from dpc_client_core.firewall import LEGACY_TOOL_ALIASES, _known_tool_names
from dpc_client_core.dpc_agent.tools.registry import (
    CORE_TOOL_NAMES,
    RESTRICTED_TOOL_NAMES,
    ToolRegistry,
)


def _registered() -> set:
    return set(ToolRegistry()._entries)


def test_every_tiered_name_is_a_tool_that_exists():
    registered = _registered()
    assert not (CORE_TOOL_NAMES - registered), "CORE_TOOL_NAMES lists tools that are not registered"
    assert not (RESTRICTED_TOOL_NAMES - registered), "RESTRICTED_TOOL_NAMES lists tools that are not registered"


def test_the_two_tiers_do_not_overlap():
    assert not (CORE_TOOL_NAMES & RESTRICTED_TOOL_NAMES)


def test_the_validator_recognises_every_registered_tool():
    """What the validator accepts comes from the registry, not from a copy of it."""
    known = _known_tool_names()
    assert known is not None, "registry unreadable in this environment"
    missing = _registered() - known
    assert not missing, f"validator would call these registered tools unknown: {sorted(missing)}"
    # Older config keys still pass, which is why they are named explicitly.
    assert LEGACY_TOOL_ALIASES <= known


def test_a_name_that_is_not_a_tool_is_still_reported():
    known = _known_tool_names()
    assert known is not None
    assert "claude_code_edit" not in known
    assert "transcribe_audio" not in known
