"""Music tools follow-up 12, item 4 (Zcode #68 P2, pre-existing in `dpc_agent/utils.py`).

`is_path_in_sandbox` compared strings: `<root>2` and `<root>-backup`, siblings of the agent's
folder, passed as inside it. It now compares path components (`relative_to`), so only the
root itself and what lies below it are inside, on Windows (case-insensitive) and POSIX alike.

Live path: `read_file` through the real `ToolRegistry`, whose absolute path goes through
`ToolContext.validate_extended_path`, the sandbox's first check; no firewall, so nothing else
admits the sibling.
"""

from __future__ import annotations

import sys

import pytest

from dpc_client_core.dpc_agent.tools.registry import ToolContext, ToolRegistry
from dpc_client_core.dpc_agent.utils import is_path_in_sandbox


def _read(root, path) -> str:
    ctx = ToolContext(agent_root=root, turn_class="owner", message_source="ui")
    reg = ToolRegistry(agent_root=root)
    reg.set_context(ctx)
    return reg.execute("read_file", {"path": str(path)})


@pytest.mark.parametrize("suffix", ["2", "-backup", ".old"])
def test_a_sibling_folder_sharing_the_roots_prefix_is_not_inside(tmp_path, suffix):
    root = tmp_path / "agents" / "agent_x"
    root.mkdir(parents=True)
    sibling = tmp_path / "agents" / f"agent_x{suffix}"
    sibling.mkdir()
    secret = sibling / "notes.txt"
    secret.write_text("another agent's notes", encoding="utf-8")

    assert is_path_in_sandbox(secret, root) is False
    out = _read(root, secret)
    assert "another agent's notes" not in out, out
    assert "Sandbox violation" in out or "SANDBOX_VIOLATION" in out, out


def test_the_root_and_what_lies_below_it_are_inside(tmp_path):
    root = tmp_path / "agents" / "agent_x"
    (root / "outputs").mkdir(parents=True)
    own = root / "outputs" / "mine.txt"
    own.write_text("my own notes", encoding="utf-8")
    assert is_path_in_sandbox(root, root) and is_path_in_sandbox(own, root)
    assert "my own notes" in _read(root, own)
    assert is_path_in_sandbox(root / ".." / "agent_x2", root) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths compare without case")
def test_on_windows_the_same_folder_in_another_case_is_inside(tmp_path):
    root = tmp_path / "agents" / "agent_x"
    root.mkdir(parents=True)
    assert is_path_in_sandbox(root / "File.txt", type(root)(str(root).upper())) is True
