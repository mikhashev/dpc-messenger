"""A file reachable through two indexed roots is still one file.

This is not the collision the key scheme fixes — both copies carry the *same* key, so
nothing is overwritten and nothing disappears. The document is simply indexed twice,
which doubles its weight in retrieval and its cost in the pass. Found in production
after the key scheme landed: 719 duplicate collections in one rebuild, every one of
them a root granted both read_only and read_write.
"""
import pathlib

from dpc_client_core.dpc_agent.extended_paths_index import collect_extended_files


def _tree(tmp_path, name, *files):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {f}\ncontent", encoding="utf-8")
    return str(root)


def _collect(ext_paths, indexed):
    return collect_extended_files(
        ext_paths, indexed_paths=indexed, allowed_extensions=frozenset({".md"})
    )


def test_root_granted_both_read_and_write_yields_each_file_once(tmp_path):
    """The access lists are separate; a path may legitimately sit in both."""
    root = _tree(tmp_path, "project", "a.md", "b.md")
    files = _collect({"read_only": [root], "read_write": [root]}, [root])
    assert sorted(f.name for f in files) == ["a.md", "b.md"]


def test_nested_indexed_roots_do_not_double_the_inner_files(tmp_path):
    outer = _tree(tmp_path, "repo", "top.md", "docs/guide.md")
    inner = str(pathlib.Path(outer) / "docs")
    files = _collect({"read_only": [outer, inner]}, [outer, inner])
    assert sorted(f.name for f in files) == ["guide.md", "top.md"]


def test_the_same_path_listed_twice_is_one_root(tmp_path):
    root = _tree(tmp_path, "project", "a.md")
    files = _collect({"read_only": [root, root]}, [root])
    assert len(files) == 1


def test_a_file_claimed_by_another_layer_is_still_skipped(tmp_path):
    """The pre-existing guarantee must survive the new one."""
    import os

    root = _tree(tmp_path, "project", "a.md", "b.md")
    claimed = {os.path.normcase(os.path.normpath(str(pathlib.Path(root) / "a.md")))}
    files = collect_extended_files(
        {"read_only": [root]}, indexed_paths=[root],
        allowed_extensions=frozenset({".md"}), already_indexed=claimed,
    )
    assert [f.name for f in files] == ["b.md"]


def test_the_caller_s_claimed_set_is_not_written_to(tmp_path):
    """It means "claimed by another layer" — not a scratch pad for this call."""
    root = _tree(tmp_path, "project", "a.md")
    claimed: set = set()
    _collect_with = collect_extended_files(
        {"read_only": [root]}, indexed_paths=[root],
        allowed_extensions=frozenset({".md"}), already_indexed=claimed,
    )
    assert len(_collect_with) == 1
    assert claimed == set()
