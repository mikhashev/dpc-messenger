"""Index flags must survive an edit to the path they were copied from.

The UI stores the flag as a copy of the access-path string, so a rename strands the
old spelling where it matches nothing and the root stops being indexed in silence.
"""
import os

import pytest

from dpc_client_core.dpc_agent.extended_paths_index import reconcile_indexed_paths


def _ext(read_only=(), read_write=()):
    return {"read_only": list(read_only), "read_write": list(read_write)}


def test_exact_match_is_left_alone(tmp_path):
    live = str(tmp_path)
    repaired, changes = reconcile_indexed_paths(_ext([live]), [live])
    assert repaired == [live]
    assert changes == []


def test_other_spelling_of_the_same_place_is_repointed(tmp_path):
    live = str(tmp_path)
    awkward = os.path.join(live, "sub", "..")  # same directory, spelled the long way
    repaired, changes = reconcile_indexed_paths(_ext([live]), [awkward])
    assert repaired == [live]
    assert any("other spelling" in c for c in changes)


def test_renamed_parent_is_repointed_by_tail(tmp_path):
    """`…/old_user/project` -> `…/new_user/project`: two trailing segments still differ."""
    live_dir = tmp_path / "new_user" / "project"
    live_dir.mkdir(parents=True)
    stale = str(tmp_path / "old_user" / "project")
    repaired, changes = reconcile_indexed_paths(_ext([str(live_dir)]), [stale])
    assert repaired == [str(live_dir)]
    assert any("segment(s) match" in c for c in changes)


def test_moved_and_renamed_is_repointed_by_final_segment(tmp_path):
    """The real case: `…/mike/ai-studio` -> `…/mikha/Documents/ai-studio`.

    Only the last segment agrees, so the two-segment tail must not be the only rule.
    """
    live_dir = tmp_path / "mikha" / "Documents" / "ai-studio"
    live_dir.mkdir(parents=True)
    stale = str(tmp_path / "mike" / "ai-studio")
    repaired, changes = reconcile_indexed_paths(_ext([str(live_dir)]), [stale])
    assert repaired == [str(live_dir)]


def test_ambiguous_tail_is_dropped_rather_than_guessed(tmp_path):
    a = tmp_path / "one" / "docs"
    b = tmp_path / "two" / "docs"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    stale = str(tmp_path / "gone" / "docs")
    repaired, changes = reconcile_indexed_paths(_ext([str(a), str(b)]), [stale])
    assert repaired == []
    assert any("dropped" in c for c in changes)


def test_unreachable_candidate_is_not_a_repoint_target(tmp_path):
    """Moving a flag onto another dead path would hide the breakage, not fix it."""
    dead_live = str(tmp_path / "absent" / "project")  # in the access list, not on disk
    stale = str(tmp_path / "other" / "project")
    repaired, changes = reconcile_indexed_paths(_ext([dead_live]), [stale])
    assert repaired == []
    assert any("dropped" in c for c in changes)


def test_same_path_at_two_access_levels_is_one_candidate(tmp_path):
    """A path listed under both read_only and read_write is one location, not two."""
    live_dir = tmp_path / "mikha" / "Documents" / "ai-studio"
    live_dir.mkdir(parents=True)
    stale = str(tmp_path / "mike" / "ai-studio")
    ext = _ext(read_only=[str(live_dir)], read_write=[str(live_dir)])
    repaired, _ = reconcile_indexed_paths(ext, [stale])
    assert repaired == [str(live_dir)]


def test_unmatched_entry_is_dropped(tmp_path):
    live = str(tmp_path)
    repaired, changes = reconcile_indexed_paths(_ext([live]), [str(tmp_path / "nowhere" / "x")])
    assert repaired == []
    assert any("dropped" in c for c in changes)


def test_read_write_paths_count_as_live(tmp_path):
    live = str(tmp_path)
    repaired, _ = reconcile_indexed_paths(_ext(read_write=[live]), [live])
    assert repaired == [live]


def test_result_has_no_duplicates(tmp_path):
    live = str(tmp_path)
    awkward = os.path.join(live, "sub", "..")
    repaired, _ = reconcile_indexed_paths(_ext([live]), [live, awkward])
    assert repaired == [live]


def test_input_list_is_not_mutated(tmp_path):
    original = [str(tmp_path / "nowhere")]
    snapshot = list(original)
    reconcile_indexed_paths(_ext([str(tmp_path)]), original)
    assert original == snapshot


def test_summary_counts_both_kinds(tmp_path):
    """A renamed machine drifts every entry at once; the boot log gets one line."""
    from dpc_client_core.dpc_agent.extended_paths_index import summarise_repairs

    live_dir = tmp_path / "mikha" / "Documents" / "project"
    live_dir.mkdir(parents=True)
    stale = str(tmp_path / "mike" / "project")
    gone = str(tmp_path / "mike" / "nothing-like-this")

    repaired, changes = reconcile_indexed_paths(_ext([str(live_dir)]), [stale, gone])
    assert repaired == [str(live_dir)]
    assert summarise_repairs(2, changes) == "2 entries, 1 re-pointed, 1 dropped (no reachable path)"
