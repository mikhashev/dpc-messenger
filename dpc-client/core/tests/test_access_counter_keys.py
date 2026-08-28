"""A document's standing must come from its own history, not from its filename.

Measured on the live agents before this change: `README.md` was a single counter
bucket holding 49 different files with 4109 accesses between them, and that number
normalised everything else — 1791 of agent_001's 1855 documents sat on the decay
floor, so decay did not rank, it divided everything by ten.
"""
import json
import os
import pathlib

import pytest

from dpc_client_core.dpc_agent.active_recall import (
    GRACE_PERIOD_DAYS,
    DECAY_FLOOR,
    _apply_decay,
    _build_access_counts,
)
from dpc_client_core.dpc_agent.hybrid_search import SearchResult


@pytest.fixture
def agent_root(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "logs").mkdir()
    return tmp_path


def _injections(agent_root, *keys_per_entry):
    path = agent_root / "state" / "knowledge_access.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for files in keys_per_entry:
            f.write(json.dumps({"ts": "now", "mode": "full", "files": list(files)}) + "\n")


def _reads(agent_root, *paths):
    path = agent_root / "logs" / "tools.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        for p in paths:
            f.write(json.dumps({"tool": "read_file", "args": {"path": p}}) + "\n")


def _doc(key, source_path="", score=1.0):
    return SearchResult(
        chunk_meta={"source_file": key, "source_path": source_path}, score=score, source="hybrid"
    )


# --- the collision itself ---


def test_namesakes_in_different_layers_count_separately(agent_root):
    """The acceptance criterion: a pair of README.md that share nothing but a name."""
    _injections(agent_root, ["EXT/dpc-messenger/README.md"] * 1)
    _injections(agent_root, *[["EXT/brainbake/README.md"]] * 5)

    counts = _build_access_counts(agent_root)
    assert counts.injections_for({"source_file": "EXT/dpc-messenger/README.md"}) == 1
    assert counts.injections_for({"source_file": "EXT/brainbake/README.md"}) == 5


def test_a_new_file_inherits_nothing_from_its_namesakes(agent_root):
    _injections(agent_root, *[["EXT/other/README.md"]] * 40)
    counts = _build_access_counts(agent_root)
    assert counts.for_document({"source_file": "knowledge/README.md"}) == 0


# --- the two vocabularies ---


def test_a_read_by_absolute_path_credits_the_indexed_document(agent_root):
    """tools.jsonl records the address the agent used, not the index key."""
    real = str(pathlib.Path(agent_root) / "projects" / "repo" / "backlog.md")
    _reads(agent_root, real)
    counts = _build_access_counts(agent_root)
    assert counts.reads_for({"source_file": "EXT/repo/backlog.md", "source_path": real}) == 1


def test_the_same_place_spelled_differently_is_the_same_place(agent_root):
    real = pathlib.Path(agent_root) / "projects" / "repo" / "backlog.md"
    awkward = str(pathlib.Path(agent_root) / "projects" / "x" / ".." / "repo" / "backlog.md")
    _reads(agent_root, awkward)
    counts = _build_access_counts(agent_root)
    assert counts.reads_for({"source_file": "EXT/repo/backlog.md", "source_path": str(real)}) == 1


def test_a_sandbox_read_credits_by_key(agent_root):
    """The agent's own layer is addressed by its key, so the read arrives as one."""
    _reads(agent_root, "knowledge/protocol-13.md")
    counts = _build_access_counts(agent_root)
    assert counts.reads_for({"source_file": "knowledge/protocol-13.md"}) == 1


def test_a_relative_address_typed_with_the_native_separator_still_matches(agent_root):
    """Index keys are always forward-slashed; an agent on Windows may not be."""
    _reads(agent_root, os.path.join("knowledge", "protocol-13.md"))
    _reads(agent_root, "./knowledge/other.md")
    counts = _build_access_counts(agent_root)
    assert counts.reads_for({"source_file": "knowledge/protocol-13.md"}) == 1
    assert counts.reads_for({"source_file": "knowledge/other.md"}) == 1


def test_reads_of_files_that_are_not_indexed_match_nothing(agent_root):
    """Dropping the "knowledge" substring filter must not credit unrelated documents."""
    _reads(agent_root, str(pathlib.Path(agent_root) / "src" / "main.py"))
    counts = _build_access_counts(agent_root)
    assert counts.for_document({"source_file": "knowledge/main.py"}) == 0


def test_skill_invocations_do_not_enter_the_counter(agent_root):
    """They were counted under a key no document can match, so they only raised the
    normaliser — the same defect this change removes, in miniature."""
    with open(agent_root / "logs" / "tools.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"tool": "execute_skill", "args": {"skill_name": "deploy"}}) + "\n")
    counts = _build_access_counts(agent_root)
    assert not counts


# --- showing a document is not the same evidence as reading it ---


def test_no_number_of_injections_outranks_a_single_read(agent_root):
    """The loop this closes: the count that decides what to show was raised by showing.

    The shown document starts ahead on search score, because that is the case the
    counter has to survive. Ranking both from 1.0 only ever tested the counter's own
    arithmetic — and the counter is a multiplier, so with a credit of 0.9 a search
    advantage of 12% was enough to put a document shown 5000 times and never opened
    above one that was read. The margin, not the ordering of two equal numbers, is
    what this asserts.
    """
    _injections(agent_root, *[["knowledge/shown.md"]] * 5000)
    _reads(agent_root, "knowledge/read-once.md")

    counts = _build_access_counts(agent_root)
    assert counts.for_document({"source_file": "knowledge/read-once.md"}) > counts.for_document(
        {"source_file": "knowledge/shown.md"}
    )
    ranked = _apply_decay(
        [_doc("knowledge/shown.md", score=2.0), _doc("knowledge/read-once.md", score=1.0)],
        agent_root,
    )
    assert ranked[0].chunk_meta["source_file"] == "knowledge/read-once.md"


def test_the_margin_a_read_holds_is_bounded_and_the_bound_is_stated(agent_root):
    """The promise is a margin, not an absolute — so pin the margin rather than imply infinity.

    A read survives a search advantage up to 1/INJECTION_MAX_CREDIT. Beyond that the
    other document wins, which is intended: search disagreeing that strongly is a
    different claim from noise. The test exists so that lowering the credit without
    thinking about the margin fails here rather than in production.
    """
    from dpc_client_core.dpc_agent.active_recall import INJECTION_MAX_CREDIT

    _injections(agent_root, *[["knowledge/shown.md"]] * 5000)
    _reads(agent_root, "knowledge/read-once.md")
    margin = 1.0 / INJECTION_MAX_CREDIT

    inside = _apply_decay(
        [_doc("knowledge/shown.md", score=margin * 0.9), _doc("knowledge/read-once.md", score=1.0)],
        agent_root,
    )
    assert inside[0].chunk_meta["source_file"] == "knowledge/read-once.md"

    outside = _apply_decay(
        [_doc("knowledge/shown.md", score=margin * 1.1), _doc("knowledge/read-once.md", score=1.0)],
        agent_root,
    )
    assert outside[0].chunk_meta["source_file"] == "knowledge/shown.md"


def test_injections_still_order_documents_nobody_has_read(agent_root):
    """The weak signal is kept, not discarded — it is the only one such files have."""
    _injections(agent_root, *[["knowledge/often.md"]] * 15)
    _injections(agent_root, ["knowledge/once.md"])
    ranked = _apply_decay([_doc("knowledge/once.md"), _doc("knowledge/often.md")], agent_root)
    assert ranked[0].chunk_meta["source_file"] == "knowledge/often.md"


def test_repeated_injection_cannot_lift_a_file_indefinitely(agent_root):
    """Past saturation the only way up is to be read."""
    _injections(agent_root, *[["knowledge/a.md"]] * 20)
    _injections(agent_root, *[["knowledge/b.md"]] * 4000)
    counts = _build_access_counts(agent_root)
    assert counts.for_document({"source_file": "knowledge/a.md"}) == counts.for_document(
        {"source_file": "knowledge/b.md"}
    )


# --- what the normaliser is allowed to depend on ---


def test_an_unrelated_popular_file_cannot_push_the_candidates_onto_the_floor(agent_root):
    """This is what a project README did to 1791 documents."""
    _reads(agent_root, *["EXT/somewhere/README.md"] * 4000)
    _reads(agent_root, *["knowledge/used.md"] * 8)
    _reads(agent_root, "knowledge/rare.md")

    results = [_doc("knowledge/rare.md"), _doc("knowledge/used.md")]
    ranked = _apply_decay(results, agent_root)
    assert [r.chunk_meta["source_file"] for r in ranked] == ["knowledge/used.md", "knowledge/rare.md"]

    # And the gap between them is real, not both flattened to the floor.
    counts = _build_access_counts(agent_root)
    top = max(counts.for_document(r.chunk_meta) for r in results)
    assert counts.for_document({"source_file": "knowledge/rare.md"}) / top > DECAY_FLOOR


def test_a_document_nobody_touched_sinks_below_one_that_was(agent_root):
    _injections(agent_root, *[["knowledge/used.md"]] * 3)
    ranked = _apply_decay([_doc("knowledge/untouched.md"), _doc("knowledge/used.md")], agent_root)
    assert ranked[0].chunk_meta["source_file"] == "knowledge/used.md"


def test_the_reported_order_and_scores_are_the_ones_that_decided_it(agent_root):
    """The log printed the fusion score while the order came from decay, so the two
    contradicted each other on every line."""
    from dpc_client_core.dpc_agent.active_recall import get_recall_block

    _reads(agent_root, *["knowledge/used.md"] * 10)
    results = [_doc("knowledge/rare.md", score=0.9), _doc("knowledge/used.md", score=0.4)]

    injection = get_recall_block(results, context_usage_ratio=0.2, agent_root=agent_root)
    reported = injection.injected
    assert [r.chunk_meta["source_file"] for r in reported] == ["knowledge/used.md", "knowledge/rare.md"]
    # Ranked first, so its printed number must be the larger one.
    assert reported[0].score > reported[1].score


def test_no_access_data_leaves_the_order_alone(agent_root):
    results = [_doc("knowledge/a.md", score=0.4), _doc("knowledge/b.md", score=0.9)]
    assert _apply_decay(results, agent_root) == results


def test_candidates_nobody_ever_touched_keep_their_search_order(agent_root):
    """With nothing to rank by, decay must not invent an order."""
    _injections(agent_root, *[["knowledge/elsewhere.md"]] * 10)
    results = [_doc("knowledge/a.md", score=0.9), _doc("knowledge/b.md", score=0.4)]
    assert _apply_decay(results, agent_root) == results


def _aged_file(tmp_path, name, days_old):
    """A real file with a chosen mtime — grace reads the filesystem, so the test must too."""
    import os
    import time
    p = tmp_path / name
    p.write_text("# doc\n", encoding="utf-8")
    stamp = time.time() - days_old * 86400
    os.utime(p, (stamp, stamp))
    return p


def test_a_new_document_is_not_floored_for_having_no_history(agent_root, tmp_path):
    """Absence of a history is not evidence against a document; it is absence of a chance.

    Grace is 1.0 — no decay — not a promotion: a new document ranks with the busiest
    candidate in its result set, and above one whose history is thinner. Before it, the
    same document sat on DECAY_FLOOR, a tenth of what a document shown once and ignored
    scored for being that set's maximum.
    """
    fresh = _aged_file(tmp_path, "fresh.md", days_old=1)
    _injections(agent_root, *[["knowledge/often.md"]] * 15)
    _injections(agent_root, ["knowledge/thin.md"])

    ranked = _apply_decay(
        [_doc("knowledge/often.md", score=1.0),
         _doc("knowledge/thin.md", score=1.0),
         _doc("knowledge/fresh.md", source_path=str(fresh), score=1.0)],
        agent_root,
    )
    by_key = {r.chunk_meta["source_file"]: r.score for r in ranked}
    assert by_key["knowledge/fresh.md"] == 1.0                      # untouched, not floored
    assert by_key["knowledge/fresh.md"] > by_key["knowledge/thin.md"]
    assert by_key["knowledge/fresh.md"] == by_key["knowledge/often.md"]  # level, not ahead


def test_an_old_document_with_no_history_still_sinks(agent_root, tmp_path):
    """The window is a grace period, not an exemption — it expires on its own."""
    stale = _aged_file(tmp_path, "stale.md", days_old=GRACE_PERIOD_DAYS + 1)
    _injections(agent_root, *[["knowledge/shown.md"]] * 5)

    ranked = _apply_decay(
        [_doc("knowledge/shown.md", score=1.0),
         _doc("knowledge/stale.md", source_path=str(stale), score=1.0)],
        agent_root,
    )
    assert ranked[0].chunk_meta["source_file"] == "knowledge/shown.md"


def test_a_document_whose_file_cannot_be_read_is_treated_as_old(agent_root, tmp_path):
    """An unreadable path must not promote itself by being unreadable."""
    _injections(agent_root, *[["knowledge/shown.md"]] * 5)

    ranked = _apply_decay(
        [_doc("knowledge/shown.md", score=1.0),
         _doc("knowledge/gone.md", source_path=str(tmp_path / "not-here.md"), score=1.0)],
        agent_root,
    )
    assert ranked[0].chunk_meta["source_file"] == "knowledge/shown.md"
