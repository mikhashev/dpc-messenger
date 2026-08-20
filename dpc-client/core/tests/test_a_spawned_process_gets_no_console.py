"""A tool that spawns a process must not hand it the operator's console.

The defect was observed on 2026-08-14: an agent's shell command printed
PowerShell's "Do you want to continue? [Y] Yes [A] Yes to All [N] No" into the
terminal running the service and waited there for its whole timeout, while the
agent waited for output that would never come. `tools/shell.py` was closed the
same day (`c2c83f7c`); the class was not — `tools/git.py` spawns git twice and
`tools/comfyui.py` spawns ffmpeg once, all three with an inherited stdin.

git is the live half of that: `git_push` runs on a real repository, and an
https remote with no cached credentials asks for a username on the console.
"""

import ast
import pathlib
import subprocess

import pytest


TOOLS_DIR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "dpc_client_core"
    / "dpc_agent"
    / "tools"
)


def _spawn_sites():
    """Every subprocess.run / Popen call in the tools package, with its stdin."""
    sites = []
    for path in sorted(TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute) or fn.attr not in ("run", "Popen"):
                continue
            owner = fn.value
            if not (isinstance(owner, ast.Name) and owner.id == "subprocess"):
                continue
            names = {kw.arg for kw in node.keywords}
            sites.append((path.name, node.lineno, "stdin" in names))
    return sites


class TestEverySpawnSiteClosesStdin:
    def test_the_search_finds_the_sites_it_is_meant_to_guard(self):
        """A source check that finds nothing passes for every program.

        Three files spawn processes today; if a refactor moves them, this fails
        first and says so, instead of quietly guarding an empty set.
        """
        sites = _spawn_sites()
        assert len(sites) >= 4, sites
        assert {name for name, _, _ in sites} >= {"shell.py", "git.py", "comfyui.py"}

    def test_none_of_them_inherits_the_service_console(self):
        inherited = [(f, line) for f, line, has_stdin in _spawn_sites() if not has_stdin]
        assert inherited == [], (
            "these spawn a child on the operator's console; a prompt there "
            f"hangs the tool for its whole timeout: {inherited}"
        )


class TestGitAsksItsQuestionsOfNobody:
    """The behaviour, not the source: both git paths pass a closed stdin."""

    @pytest.fixture
    def seen(self, monkeypatch):
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        return captured

    def test_the_sandbox_path(self, seen, tmp_path):
        from dpc_client_core.dpc_agent.tools import git as git_tool
        from dpc_client_core.dpc_agent.tools.registry import ToolContext

        ctx = ToolContext(agent_root=tmp_path)
        result = git_tool._run_git(ctx, ["status", "--short"])

        assert result["success"] is True
        assert seen.get("stdin") is subprocess.DEVNULL

    def test_the_repository_path_that_git_push_uses(self, seen, tmp_path):
        from dpc_client_core.dpc_agent.tools import git as git_tool

        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        result = git_tool._run_git_external(str(repo), ["push", "origin", "main"])

        assert result["success"] is True
        assert seen.get("stdin") is subprocess.DEVNULL
