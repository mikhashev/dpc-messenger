"""Two files must never answer to one index key.

A shared key is not a duplicate — the second file overwrites the first in the hash
map, is deleted along with it by remove_by_source, and never appears in a hint. It
happens quietly, which is why these tests are about uniqueness first and looks second.
"""
import pathlib

import pytest

from dpc_client_core.dpc_agent.index_keys import (
    KEY_FORMAT,
    build_ext_roots,
    ext_key,
    l5_key,
    l6_key,
)


def _roots(tmp_path, *names):
    made = []
    for n in names:
        p = tmp_path / n
        p.mkdir(parents=True, exist_ok=True)
        made.append(str(p))
    return made


# --- L5 / L6 ---


def test_l5_key_is_a_working_read_file_argument(tmp_path):
    """read_file resolves relative paths against the sandbox, and knowledge/ is in it."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    f = kdir / "protocol-13.md"
    f.write_text("x", encoding="utf-8")
    assert l5_key(f, kdir) == "knowledge/protocol-13.md"


def test_l5_key_keeps_subdirectories(tmp_path):
    kdir = tmp_path / "knowledge"
    (kdir / "archive").mkdir(parents=True)
    f = kdir / "archive" / "old.md"
    assert l5_key(f, kdir) == "knowledge/archive/old.md"


def test_l6_key_is_prefixed(tmp_path):
    l6 = tmp_path / "knowledge"
    l6.mkdir()
    assert l6_key(l6 / "commit.md", l6) == "L6/commit.md"


def test_key_outside_its_own_layer_falls_back_to_the_name(tmp_path):
    """Config drift must not raise inside an indexing pass."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    assert l5_key(tmp_path / "elsewhere.md", kdir) == "knowledge/elsewhere.md"


# --- EXT: the collision this whole change exists for ---


def test_same_relative_path_under_two_roots_gets_two_keys(tmp_path):
    a, b = _roots(tmp_path, "project-a", "project-b")
    roots = build_ext_roots([a, b])
    ka = ext_key(pathlib.Path(a) / "README.md", roots)
    kb = ext_key(pathlib.Path(b) / "README.md", roots)
    assert ka != kb
    assert ka == "EXT/project-a/README.md"
    assert kb == "EXT/project-b/README.md"


def test_root_tail_grows_only_for_the_roots_that_clash(tmp_path):
    """Adding a clashing root must not rename the keys of unrelated ones."""
    a, b, c = _roots(tmp_path, "alpha/docs", "beta/docs", "gamma")
    roots = {r.tail for r in build_ext_roots([a, b, c])}
    assert roots == {"alpha/docs", "beta/docs", "gamma"}


def test_a_root_that_is_the_parent_of_the_clash_grows_too(tmp_path):
    """`<tmp>/docs` shares the depth-1 tail with the other two, so it cannot keep it.

    Its own parent then distinguishes it. The three tails carry the same number of
    separators here, but that is incidental — what is asserted is that they differ.
    """
    a, b, c = _roots(tmp_path, "alpha/docs", "beta/docs", "docs")
    tails = [r.tail for r in build_ext_roots([a, b, c])]
    assert len(set(tails)) == 3
    assert all(t.endswith("docs") for t in tails)


def test_indexed_path_pointing_at_a_single_file(tmp_path):
    """The old scheme keyed every such file as `EXT/.` — all of them, one key."""
    d = tmp_path / "notes"
    d.mkdir()
    one, two = d / "one.md", d / "two.md"
    one.write_text("1", encoding="utf-8")
    two.write_text("2", encoding="utf-8")
    roots = build_ext_roots([str(one), str(two)])
    assert ext_key(one, roots) == "EXT/notes/one.md"
    assert ext_key(two, roots) == "EXT/notes/two.md"


def test_nested_roots_use_the_nearer_one(tmp_path):
    outer, inner = _roots(tmp_path, "repo", "repo/docs")
    roots = build_ext_roots([outer, inner])
    f = pathlib.Path(inner) / "guide.md"
    assert ext_key(f, roots) == "EXT/docs/guide.md"


def test_file_under_no_root_keeps_its_whole_path(tmp_path):
    """Ugly is recoverable; colliding loses a document without saying so."""
    (a,) = _roots(tmp_path, "project-a")
    roots = build_ext_roots([a])
    key = ext_key(tmp_path / "orphan" / "stray.md", roots)
    assert key.startswith("EXT/")
    assert key.endswith("orphan/stray.md")


def test_separators_never_leak_into_a_segment(tmp_path):
    """A root at the filesystem root would otherwise split one segment into two."""
    roots = build_ext_roots([str(pathlib.Path(tmp_path.anchor))])
    assert roots[0].tail and "\\" not in roots[0].tail
    assert ":" not in roots[0].tail


def test_duplicate_configuration_of_one_root_is_one_root(tmp_path):
    """The same path may be listed under both read_only and read_write."""
    (a,) = _roots(tmp_path, "project-a")
    assert len(build_ext_roots([a, a])) == 1


def test_no_two_files_share_a_key_across_layers(tmp_path):
    """The property that matters, stated directly."""
    kdir = tmp_path / "sandbox" / "knowledge"
    kdir.mkdir(parents=True)
    l6 = tmp_path / "dpc" / "knowledge"
    l6.mkdir(parents=True)
    a, b = _roots(tmp_path, "ext/project-a", "ext/project-b")

    roots = build_ext_roots([a, b])
    keys = [
        l5_key(kdir / "README.md", kdir),
        l6_key(l6 / "README.md", l6),
        ext_key(pathlib.Path(a) / "README.md", roots),
        ext_key(pathlib.Path(b) / "README.md", roots),
    ]
    assert len(set(keys)) == len(keys)


def test_key_format_marker_is_set():
    """agent_manager compares this against the stored marker to force a rebuild."""
    assert KEY_FORMAT == "layer_addressed_v3"
