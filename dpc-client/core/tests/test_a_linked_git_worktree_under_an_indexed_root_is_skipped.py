"""A linked git worktree below an indexed root is skipped, not re-indexed.

A linked worktree's `.git` is a FILE (`gitdir: <main>/.git/worktrees/<name>`),
so DEFAULT_EXCLUDED_DIRS — which matches path parts — never sees it, and a full
second checkout of a repository sitting inside an indexed path was collected as
brand-new content by every agent: 250 files, six agents, ~56 min of CPU spent
embedding a copy of what the index already had.

The neighbours that also carry a `.git` file — submodules (`.../.git/modules/`)
and a worktree that IS the indexed root — stay indexed, and a `.git` file that
cannot be read or parsed drops nothing. No `git worktree` subprocess is needed:
the check reads exactly the `.git` file, so the structure is fabricated in
tmp_path.
"""
import logging
import pathlib

from dpc_client_core.dpc_agent.extended_paths_index import collect_extended_files

MD = frozenset({".md"})
_LOGGER = "dpc_client_core.dpc_agent.extended_paths_index"


def _collect(root, **kwargs):
    kwargs.setdefault("allowed_extensions", MD)
    return collect_extended_files({"read_only": [str(root)]}, **kwargs)


def _names(files):
    return sorted(f.name for f in files)


def _plain_file(parent, name):
    parent.mkdir(parents=True, exist_ok=True)
    (parent / name).write_text(f"# {name}\ncontent", encoding="utf-8")


def _main_repo(parent, name="main"):
    """The checkout a worktree hangs off: a `.git` directory plus one file."""
    main = parent / name
    main.mkdir(parents=True, exist_ok=True)
    (main / ".git").mkdir(exist_ok=True)
    (main / "readme.md").write_text("# main\ncontent", encoding="utf-8")
    return main


def _worktree(parent, name, gitdir, *files):
    """A linked worktree: a plain folder whose `.git` FILE carries the gitdir.

    `gitdir` is written verbatim — forward or back slashes, absolute or relative."""
    wt = parent / name
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    for f in files:
        p = wt / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {f}\ncontent", encoding="utf-8")
    return wt


def _git_file_dir(parent, name, content, *files):
    """A folder with a `.git` file that is NOT a worktree's — odd content."""
    folder = parent / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".git").write_text(content, encoding="utf-8")
    for f in files:
        (folder / f).write_text(f"# {f}\ncontent", encoding="utf-8")
    return folder


def test_a_worktree_with_a_forward_slashed_absolute_gitdir_is_skipped(tmp_path):
    """The incident's shape: a worktree nested one level under the indexed root."""
    root = tmp_path / "workspace"
    root.mkdir()
    _plain_file(root, "keep.md")
    main = _main_repo(root, name="proj")
    _worktree(root / "tools", "wt", main.as_posix() + "/.git/worktrees/wt", "dup.md")

    files = _collect(root)
    assert _names(files) == ["keep.md", "readme.md"]


def test_a_worktree_nested_deeper_than_one_level_is_skipped(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    _plain_file(root, "keep.md")
    main = _main_repo(root, name="proj")
    _worktree(root / "tools" / "vendor", "wt",
              main.as_posix() + "/.git/worktrees/wt", "dup.md")

    files = _collect(root)
    assert _names(files) == ["keep.md", "readme.md"]


def test_a_submodule_is_still_collected(tmp_path):
    """Its `.git` file points into `.../.git/modules/`, not `.../worktrees/`."""
    root = tmp_path / "workspace"
    root.mkdir()
    main = _main_repo(root, name="proj")
    sub = root / "proj" / "sub"
    sub.mkdir(parents=True)
    (sub / ".git").write_text(
        f"gitdir: {main.as_posix()}/.git/modules/sub\n", encoding="utf-8"
    )
    (sub / "lib.md").write_text("# lib\ncontent", encoding="utf-8")

    files = _collect(root)
    assert _names(files) == ["lib.md", "readme.md"]


def test_a_worktree_that_is_the_indexed_root_itself_is_collected(tmp_path):
    """Only directories BELOW a root are candidates; the root is the user's pick."""
    main = _main_repo(tmp_path, name="main")
    root = _worktree(tmp_path, "root-wt", main.as_posix() + "/.git/worktrees/root-wt",
                     "a.md", "docs/b.md")

    files = _collect(root)
    assert _names(files) == ["a.md", "b.md"]


def test_a_relative_gitdir_is_resolved_against_the_worktree_folder(tmp_path):
    """`gitdir: ../../main/.git/worktrees/wt` — resolution, not existence, decides."""
    root = tmp_path / "workspace"
    root.mkdir()
    _plain_file(root, "keep.md")
    main = _main_repo(tmp_path, name="main")
    # From workspace/wt, ../../ lands on tmp_path, where main sits.
    _worktree(root, "wt", "../../main/.git/worktrees/wt", "dup.md")

    files = _collect(root)
    assert _names(files) == ["keep.md"]


def test_a_backslashed_gitdir_is_handled_like_a_forward_slashed_one(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    _plain_file(root, "keep.md")
    main = _main_repo(tmp_path, name="main")
    backslashed = (main.as_posix() + "/.git/worktrees/wt").replace("/", "\\")
    _worktree(root, "wt", backslashed, "dup.md")

    files = _collect(root, excluded_dirs=[])
    assert _names(files) == ["keep.md"]


def test_the_worktree_skip_holds_for_a_custom_excluded_dirs_and_the_default(tmp_path):
    """The check is independent of `excluded_dirs`: override and default both skip."""
    def build(parent):
        parent.mkdir()
        main = _main_repo(parent, name="main")
        _worktree(parent, "wt", main.as_posix() + "/.git/worktrees/wt", "dup.md")

    with_override = tmp_path / "custom"
    build(with_override)
    assert _names(_collect(with_override, excluded_dirs=["node_modules"])) == ["readme.md"]

    with_default = tmp_path / "default"
    build(with_default)
    assert _names(_collect(with_default, excluded_dirs=None)) == ["readme.md"]


def test_an_odd_git_file_drops_nothing(tmp_path):
    """Garbage, no prefix, an empty gitdir: none of it is a confirmed worktree."""
    root = tmp_path / "workspace"
    root.mkdir()
    _git_file_dir(root, "garbage", "this is not a gitdir\n", "g.md")
    _git_file_dir(root, "empty-value", "gitdir: \n", "e.md")
    _git_file_dir(root, "no-such-layout", "gitdir: ../somewhere/else\n", "n.md")

    files = _collect(root)
    assert _names(files) == ["e.md", "g.md", "n.md"]


def test_an_unreadable_git_file_drops_nothing(tmp_path, monkeypatch):
    """The OSError branch: a folder is skipped only on a confirmed gitdir."""
    real_read_text = pathlib.Path.read_text

    def refusing_read_text(self, *args, **kwargs):
        if self.name == ".git":
            raise OSError("permission denied (simulated)")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", refusing_read_text)

    root = tmp_path / "workspace"
    root.mkdir()
    main = _main_repo(root, name="main")
    _worktree(root, "wt", main.as_posix() + "/.git/worktrees/wt", "dup.md")

    files = _collect(root)
    assert _names(files) == ["dup.md", "readme.md"]


def test_a_nested_ordinary_repo_still_loses_only_its_git_dir(tmp_path):
    """The neighbouring input: `.git` as a directory keeps today's behaviour."""
    root = tmp_path / "workspace"
    root.mkdir()
    repo = root / "proj"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config.md").write_text("[core]\n", encoding="utf-8")
    (repo / "readme.md").write_text("# readme\ncontent", encoding="utf-8")

    files = _collect(root)
    assert _names(files) == ["readme.md"]


def test_the_worktree_skip_is_said_at_info_and_each_path_at_debug(tmp_path, caplog):
    """A silent disappearance of documents is what this card exists to prevent."""
    root = tmp_path / "workspace"
    root.mkdir()
    main = _main_repo(root, name="main")
    wt_one = _worktree(root, "one", main.as_posix() + "/.git/worktrees/one", "dup.md")
    wt_two = _worktree(root, "two", main.as_posix() + "/.git/worktrees/two", "dup.md")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _collect(root)
    assert "2 linked git worktree(s)" in caplog.text

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        _collect(root)
    paths_said = [r.getMessage() for r in caplog.records
                  if r.levelno == logging.DEBUG and "worktree" in r.getMessage()]
    assert any(str(wt_one) in msg for msg in paths_said)
    assert any(str(wt_two) in msg for msg in paths_said)
