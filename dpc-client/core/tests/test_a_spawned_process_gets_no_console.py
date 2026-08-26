"""A tool that spawns a process must not hand it the operator's console.

The defect was observed on 2026-08-14: an agent's shell command printed
PowerShell's "Do you want to continue? [Y] Yes [A] Yes to All [N] No" into the
terminal running the service and waited there for its whole timeout, while the
agent waited for output that would never come. `tools/shell.py` was closed the
same day (`c2c83f7c`); the class was not — `tools/git.py` spawned git twice and
`tools/comfyui.py` spawns ffmpeg once, all three with an inherited stdin.

git is the live half of that: `git_push` runs on a real repository, and an
https remote with no cached credentials asks for a username on the console.

Updated 2026-08-26: git no longer spawns anything itself — both its call sites
go through `run_supervised` in `tools/process.py`, which closes stdin
unconditionally. The two git tests below now assert that a closed stdin reaches
the actual `Popen` at the end of that chain, which is a longer claim than the
one they made before, not a shorter one.
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
        assert len(sites) >= 3, sites
        # git.py is deliberately absent: it spawns through process.py now. If it
        # ever reappears here, someone reintroduced a direct spawn.
        assert {name for name, _, _ in sites} >= {"shell.py", "comfyui.py", "process.py"}
        assert "git.py" not in {name for name, _, _ in sites}, (
            "git.py spawns directly again — it must go through run_supervised"
        )

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
        """Capture at the real Popen, on the far side of run_supervised."""
        from dpc_client_core.dpc_agent.tools import process as process_mod

        captured = {}

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                captured.update(kwargs)
                self.pid = -1
                self.returncode = 0

            def communicate(self, timeout=None):
                return ("ok", "")

            def poll(self):
                return 0

        monkeypatch.setattr(process_mod.subprocess, "Popen", _FakePopen)
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
