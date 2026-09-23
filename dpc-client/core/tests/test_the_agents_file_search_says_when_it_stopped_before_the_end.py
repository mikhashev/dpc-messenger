"""search_files must not report absence over files it never read.

A 10 MB read cap used to stop the walk inside .venv without a word, and the
answer was a plain "No matches found"; the matched-files counter also stayed 0.
"""

from pathlib import Path

from dpc_client_core.dpc_agent.tools import core as core_tools
from dpc_client_core.dpc_agent.tools.core import search_files
from dpc_client_core.dpc_agent.tools.registry import ToolContext

NEEDLE = "_SHELL_WRAPPERS_NEEDLE"
MB = 1024 * 1024


def _fill(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = "x = 'filler text for the search cap'\n"
    # newline="\n" keeps Windows from growing each file past the 1 MB skip.
    path.write_text(line * (size // len(line)), encoding="utf-8", newline="\n")


def test_a_match_behind_a_large_venv_is_found(tmp_path):
    # Over 10 MB of vendor text. rglob walked breadth-first, so the real file
    # sits deeper than the vendor files to come after them in the walk.
    for i in range(12):
        _fill(tmp_path / ".venv" / "Lib" / "site-packages" / f"pkg{i:02d}.py", MB - 4096)
    real = tmp_path / "src" / "pkg" / "agent" / "tools" / "shell.py"
    real.parent.mkdir(parents=True)
    real.write_text(f"{NEEDLE} = ('cmd', 'sh')\n", encoding="utf-8")

    out = search_files(ToolContext(agent_root=tmp_path), NEEDLE)

    assert "## Search Results for" in out
    assert "shell.py" in out and NEEDLE in out


def test_the_match_count_names_the_real_number_of_files(tmp_path):
    (tmp_path / "a.py").write_text(f"{NEEDLE}\nnothing\n{NEEDLE}\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(f"{NEEDLE}\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.md").write_text(f"see {NEEDLE}\n", encoding="utf-8")
    (tmp_path / "sub" / "d.md").write_text("no hit here\n", encoding="utf-8")

    out = search_files(ToolContext(agent_root=tmp_path), NEEDLE)

    assert "Found 4 matches in 3 files (searched 4 files)" in out


def test_a_walk_stopped_by_the_read_cap_says_so_instead_of_claiming_absence(tmp_path, monkeypatch):
    # Shrink the cap if the module exposes it; the pre-fix code keeps 10 MB,
    # so the tree is sized to cross whichever cap applies.
    monkeypatch.setattr(core_tools, "SEARCH_MAX_TOTAL_BYTES", 2 * MB, raising=False)
    for i in range(12):
        _fill(tmp_path / "docs" / f"part{i:02d}.txt", MB - 4096)

    out = search_files(ToolContext(agent_root=tmp_path), NEEDLE)

    assert not out.startswith("No matches found")
    assert "stopped" in out
    assert "more files" in out
    assert "first unread" in out


def test_a_path_the_caller_points_at_inside_venv_is_searched(tmp_path):
    target = tmp_path / ".venv" / "Lib" / "site-packages" / "attr" / "_make.py"
    target.parent.mkdir(parents=True)
    target.write_text(f"def f():\n    return {NEEDLE}\n", encoding="utf-8")
    (tmp_path / ".venv" / "Lib" / "build").mkdir(parents=True)
    (tmp_path / ".venv" / "Lib" / "build" / "x.py").write_text(f"{NEEDLE}\n", encoding="utf-8")
    ctx = ToolContext(agent_root=tmp_path)

    inside = search_files(ctx, NEEDLE, path=".venv/Lib")
    at_venv = search_files(ctx, NEEDLE, path=".venv")

    assert "Found 2 matches in 2 files" in inside
    assert "Found 2 matches in 2 files" in at_venv


def test_a_vendor_directory_is_not_searched_from_above(tmp_path):
    for d in (".venv", "node_modules", ".git", "__pycache__", "target", "dist", "build"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "f.txt").write_text(f"{NEEDLE}\n", encoding="utf-8")
    (tmp_path / "keep.txt").write_text(f"{NEEDLE}\n", encoding="utf-8")

    out = search_files(ToolContext(agent_root=tmp_path), NEEDLE)

    assert "Found 1 matches in 1 files (searched 1 files)" in out


def test_include_pattern_still_filters_by_name_and_by_subpath(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "shell.py").write_text(f"{NEEDLE}\n", encoding="utf-8")
    (tmp_path / "tools" / "notes.md").write_text(f"{NEEDLE}\n", encoding="utf-8")
    (tmp_path / "other.py").write_text(f"{NEEDLE}\n", encoding="utf-8")
    ctx = ToolContext(agent_root=tmp_path)

    assert "Found 2 matches in 2 files" in search_files(ctx, NEEDLE, include_pattern="*.py")
    assert "Found 1 matches in 1 files" in search_files(ctx, NEEDLE, include_pattern="tools/*.py")
