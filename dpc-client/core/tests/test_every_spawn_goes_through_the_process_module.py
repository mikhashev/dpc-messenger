"""Every process the backend starts is started by `dpc_agent/tools/process.py`.

ADR-046 point 5. `run_supervised` and `run_probe` put a child in the process
registry, give it a console and a session of its own, a closed stdin, the
memory ceiling and bounded waits. A spawn written anywhere else gets none of
that and is in no list, which is how the incidents the ADR names happened.

This walks every `.py` under `dpc_client_core` and `run_service.py` with `ast`
and fails on a spawn call outside `process.py` that the allow-list below does
not name. Import aliases are resolved (`import subprocess as sp`,
`from subprocess import run`, `from asyncio import create_subprocess_exec`).
A spawn function stored in a variable and called through it is not seen.

An AST search that finds nothing passes for every program, so the tests first
prove the walk sees calls at all: it finds every allow-listed site and the
module's own spawns, and it flags planted calls in source strings. An
allow-list entry that matches no call fails too, so the list cannot rot.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import pytest

CORE = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = CORE / "dpc_client_core"
THE_MODULE = "dpc_client_core/dpc_agent/tools/process.py"

_SPAWN_CALLS = {
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    "asyncio.subprocess.create_subprocess_exec", "asyncio.subprocess.create_subprocess_shell",
    "os.system", "os.popen", "os.startfile",
}
_SPAWN_PREFIXES = ("os.spawn", "os.exec", "os.posix_spawn")
# `loop.subprocess_exec(...)`: the receiver is any event loop, so any receiver.
_LOOP_METHODS = {"subprocess_exec", "subprocess_shell"}

# (file, enclosing function) -> why it may spawn by itself, or who owns the change.
ALLOWED: Dict[Tuple[str, str], Dict[str, str]] = {
    ("dpc_client_core/managers/llama_server_supervisor.py", "LlamaServerSupervisor._spawn"): {
        "reason": "the engine streams to a log file and is stopped with CTRL_BREAK, which "
                  "run_supervised does not do; it registers its child at _spawn and reports "
                  "its end (ADR-046 point 5)",
    },
    ("dpc_client_core/managers/strata_supervisor.py", "StrataSupervisor._spawn"): {
        "reason": "as the llama-server supervisor: a log file, CTRL_BREAK, and the child "
                  "registered at _spawn (ADR-046 point 5)",
    },
    ("dpc_client_core/job_object.py", "_launch_explorer"): {
        "reason": "the one spawn whose purpose is to hand a URL to the shell outside the "
                  "backend's job; the registry has no launcher word for it",
    },
    ("dpc_client_core/dpc_agent/tools/comfyui.py", "comfyui_convert"): {
        "owner": "the developer of the ComfyUI tools",
        "due": "to be agreed with them (ADR-046 point 5)",
    },
}


@dataclass(frozen=True)
class Site:
    path: str
    function: str
    line: int
    call: str


def _dotted(node: ast.AST) -> Optional[List[str]]:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def _aliases(tree: ast.AST) -> Dict[str, str]:
    """Local name -> the dotted name it stands for, from every import in the file
    (function-level ones too: an over-approximation, never a blind spot)."""
    names: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    names[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _is_spawn(resolved: str, attr: Optional[str]) -> bool:
    return (resolved in _SPAWN_CALLS or resolved.startswith(_SPAWN_PREFIXES)
            or attr in _LOOP_METHODS)


class _Walker(ast.NodeVisitor):
    def __init__(self, path: str, aliases: Dict[str, str]):
        self.path, self.aliases = path, aliases
        self.scope: List[str] = []
        self.sites: List[Site] = []

    def _scoped(self, node: ast.AST) -> None:
        self.scope.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        parts = _dotted(node.func)
        if parts is not None:
            head = self.aliases.get(parts[0], parts[0])
            resolved = ".".join([head] + parts[1:])
            attr = parts[-1] if len(parts) > 1 else None
            if _is_spawn(resolved, attr):
                self.sites.append(Site(self.path, ".".join(self.scope) or "<module>", node.lineno, resolved))
        self.generic_visit(node)


def spawn_sites(sources: Dict[str, str]) -> List[Site]:
    """Every spawn call in `sources` ({path relative to dpc-client/core: text})."""
    found: List[Site] = []
    for path, text in sorted(sources.items()):
        tree = ast.parse(text, filename=path)
        walker = _Walker(path, _aliases(tree))
        walker.visit(tree)
        found.extend(walker.sites)
    return found


def violations(sites: Iterable[Site], allowed=ALLOWED) -> List[Site]:
    return [s for s in sites if s.path != THE_MODULE and (s.path, s.function) not in allowed]


def stale_entries(sites: Iterable[Site], allowed=ALLOWED) -> List[Tuple[str, str]]:
    seen = {(s.path, s.function) for s in sites}
    return [key for key in allowed if key not in seen]


def _the_tree() -> Dict[str, str]:
    files = sorted(PACKAGE.rglob("*.py")) + [CORE / "run_service.py"]
    return {p.relative_to(CORE).as_posix(): p.read_text(encoding="utf-8") for p in files}


@pytest.fixture(scope="module")
def tree_sites() -> List[Site]:
    return spawn_sites(_the_tree())


class TestTheWalkSeesCalls:
    def test_it_finds_every_allow_listed_site(self, tree_sites):
        """The positive control on the real tree: four places spawn by themselves
        today, and the walk names each of them."""
        found = {(s.path, s.function) for s in tree_sites}
        assert set(ALLOWED) <= found, sorted(set(ALLOWED) - found)

    def test_it_finds_the_modules_own_spawns(self, tree_sites):
        """`run_supervised`'s Popen, the `taskkill` and the `ps` of the tree kill."""
        own = {(s.function, s.call) for s in tree_sites if s.path == THE_MODULE}
        assert {("run_supervised", "subprocess.Popen"), ("_kill_process_tree", "subprocess.run"),
                ("_parent_map_from_ps", "subprocess.run")} <= own, own

    def test_it_reads_run_service_beside_the_package(self):
        assert "run_service.py" in _the_tree()

    @pytest.mark.parametrize("source, call", [
        ("import subprocess\nsubprocess.run(['x'])\n", "subprocess.run"),
        ("import subprocess as sp\nsp.Popen(['x'])\n", "subprocess.Popen"),
        ("from subprocess import run\nrun(['x'])\n", "subprocess.run"),
        ("from subprocess import check_output as co\nco(['x'])\n", "subprocess.check_output"),
        ("def f():\n    import subprocess\n    return subprocess.getoutput('x')\n", "subprocess.getoutput"),
        ("import asyncio\nasync def f():\n    await asyncio.create_subprocess_exec('x')\n",
         "asyncio.create_subprocess_exec"),
        ("from asyncio import create_subprocess_shell\nasync def f():\n    await create_subprocess_shell('x')\n",
         "asyncio.create_subprocess_shell"),
        ("import asyncio.subprocess\nasync def f():\n    await asyncio.subprocess.create_subprocess_exec('x')\n",
         "asyncio.subprocess.create_subprocess_exec"),
        ("async def f(loop):\n    await loop.subprocess_exec(object, 'x')\n", "loop.subprocess_exec"),
        ("import os\nos.system('x')\n", "os.system"),
        ("import os as _os\n_os.spawnl(0, 'x', 'x')\n", "os.spawnl"),
        ("from os import execv\nexecv('x', ['x'])\n", "os.execv"),
        ("import os\nos.startfile('x')\n", "os.startfile"),
        ("import os\nos.popen('x')\n", "os.popen"),
    ])
    def test_a_planted_spawn_outside_the_module_is_flagged(self, source, call):
        sites = spawn_sites({"dpc_client_core/planted.py": source})
        assert [s.call for s in violations(sites)] == [call]

    def test_the_same_spawn_inside_the_module_is_not(self):
        sites = spawn_sites({THE_MODULE: "import subprocess\nsubprocess.Popen(['x'])\n"})
        assert len(sites) == 1 and violations(sites) == []

    def test_what_is_not_a_spawn_is_not_flagged(self):
        source = (
            "import subprocess, os\n"
            "from dpc_client_core.dpc_agent.tools.process import run_probe, run_supervised\n"
            "run_probe(['git'], launcher='git_helper', timeout=1)\n"
            "run_supervised(['x'], launcher='run_shell', timeout=1)\n"
            "subprocess.CompletedProcess(['x'], 0)\n"
            "raise subprocess.TimeoutExpired('x', 1)\n"
            "os.path.join('a', 'b')\n"
            "class C:\n    def run(self):\n        return self.run()\n"
        )
        assert spawn_sites({"dpc_client_core/planted.py": source}) == []

    def test_the_enclosing_function_is_named_with_its_class(self):
        source = "import subprocess\nclass A:\n    def go(self):\n        subprocess.run(['x'])\n"
        (site,) = spawn_sites({"dpc_client_core/planted.py": source})
        assert site.function == "A.go"


class TestTheRule:
    def test_no_spawn_outside_the_process_module_but_the_allow_list(self, tree_sites):
        found = violations(tree_sites)
        assert found == [], (
            "these start a process outside dpc_agent/tools/process.py, so the process "
            "registry never sees them: use run_probe (a short command) or run_supervised, "
            "or add an allow-list entry with its reason (ADR-046 point 5): "
            + "; ".join(f"{s.path}:{s.line} in {s.function} ({s.call})" for s in found)
        )

    def test_every_allow_list_entry_still_matches_a_call(self, tree_sites):
        assert stale_entries(tree_sites) == [], "an entry that matches no call is a rotten entry: remove it"

    def test_a_stale_entry_is_reported(self, tree_sites):
        allowed = dict(ALLOWED)
        allowed[("dpc_client_core/service.py", "CoreService._get_local_ips")] = {"reason": "moved"}
        assert stale_entries(tree_sites, allowed) == [("dpc_client_core/service.py", "CoreService._get_local_ips")]

    def test_every_entry_gives_a_reason_or_an_owner_and_a_due_date(self):
        for key, entry in ALLOWED.items():
            assert entry.get("reason") or (entry.get("owner") and entry.get("due")), key
