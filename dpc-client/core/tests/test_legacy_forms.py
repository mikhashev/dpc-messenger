"""Run the migration paths over the state earlier versions wrote.

The forms come from tests/legacy_forms.py, where each one is recorded with the
measurement it was taken from. What is asserted here is the half no clean-state test
can reach: not that new code writes what new code reads, but what becomes of the rows
that were already on disk when the code changed under them.

Acceptance for AR-TESTS-HAVE-NO-LEGACY-STATE: a change that breaks on rows written by
a previous version fails here, rather than on the first restart.
"""

from __future__ import annotations

import pathlib

import pytest

from dpc_client_core.dpc_agent.active_recall import (
    _build_access_counts,
    _is_within_grace,
    hint_address,
)
from dpc_client_core.dpc_agent.index_keys import KEY_FORMAT
from dpc_client_core.dpc_agent.indexing_pipeline import rebuild_decision
from dpc_client_core.dpc_agent.memory import last_touched, read_all_meta

from .legacy_forms import (  # noqa: F401 — legacy_agent_root is a fixture
    LEGACY_KEY_FORMAT,
    legacy_agent_root,
    legacy_chunk_meta,
    legacy_graph_node,
    write_legacy_index_header,
)


# --------------------------------------------------------------------------
# F5 — the header that announces a previous scheme
# --------------------------------------------------------------------------

def test_an_index_built_by_a_previous_scheme_is_rebuilt_not_extended(legacy_agent_root):
    """The marker is the only thing that can tell us the rows are old.

    Their hashes are current — the documents did not change, the code did — so an
    incremental pass would walk past every one of them and report success.
    """
    decision = rebuild_decision(legacy_agent_root / "state" / "memory_index", "BAAI/bge-m3")

    assert decision.needed
    assert LEGACY_KEY_FORMAT in decision.message  # the log has to name what it found


def test_an_index_built_by_this_scheme_is_left_alone(legacy_agent_root):
    index_dir = legacy_agent_root / "state" / "memory_index"
    write_legacy_index_header(index_dir, key_format=KEY_FORMAT)

    decision = rebuild_decision(index_dir, "BAAI/bge-m3")

    assert not decision.needed
    assert decision.message == ""  # nothing happened; nothing to say


def test_an_unreadable_header_is_rebuilt_rather_than_guessed_at(tmp_path):
    index_dir = tmp_path / "memory_index"
    index_dir.mkdir()
    (index_dir / "index_meta.json").write_text("{not json", encoding="utf-8")

    assert rebuild_decision(index_dir, "BAAI/bge-m3").needed


def test_a_missing_index_rebuilds_without_claiming_a_migration(tmp_path):
    """Absent is not legacy. A first run should not log as though it found old rows."""
    decision = rebuild_decision(tmp_path / "memory_index", "BAAI/bge-m3")

    assert decision.needed
    assert decision.message == ""


# --------------------------------------------------------------------------
# F4 — stored meta from before the store kept source_path
# --------------------------------------------------------------------------

def test_a_row_without_a_stored_path_says_so_instead_of_offering_an_address():
    """This is the 102-day bug in one assertion.

    For rows outside the agent's own layer there is nothing to resolve to, and the
    honest answer is None — the caller prints "not readable from here". Returning
    anything else is how a hint stayed helpful-looking while every call behind it
    failed and nothing counted the failures.
    """
    assert hint_address(legacy_chunk_meta("L6/commit-note.md", source_layer="L6")) is None
    assert hint_address(legacy_chunk_meta("EXT/some-project/README.md")) is None


def test_a_row_without_a_stored_path_is_still_addressable_in_the_agents_own_layer():
    """L5 keys are sandbox-relative paths, so they never needed the field."""
    assert hint_address(legacy_chunk_meta("knowledge/alpha.md")) == "knowledge/alpha.md"


def test_a_row_without_a_stored_path_is_not_granted_grace():
    """Grace is read from the file's mtime, and a row with no path has no file.

    Treating it as new would hand a promotion to precisely the rows we cannot verify.
    """
    assert _is_within_grace(legacy_chunk_meta("EXT/some-project/README.md")) is False


# --------------------------------------------------------------------------
# F3 — _meta.json from before reads and writes were told apart
# --------------------------------------------------------------------------

def test_legacy_access_numbers_move_to_the_column_that_described_them(legacy_agent_root):
    data = read_all_meta(legacy_agent_root / "knowledge")

    alpha = data["alpha.md"]
    assert alpha["write_count"] == 7           # every one of them was a write
    assert alpha["last_written"] == "2026-04-20T11:00:00+00:00"
    assert alpha["access_count"] == 0          # nobody was ever recorded reading it
    assert alpha["last_accessed"] == ""


def test_a_migrated_entry_is_not_migrated_again(legacy_agent_root):
    """The migration keys on the absence of a write date, so a second pass must be a
    no-op — otherwise every read of _meta.json would zero the reads recorded since."""
    knowledge = legacy_agent_root / "knowledge"
    read_all_meta(knowledge)                    # first pass migrates and persists

    again = read_all_meta(knowledge)

    assert again["alpha.md"]["write_count"] == 7
    assert again["alpha.md"]["last_written"] == "2026-04-20T11:00:00+00:00"


def test_a_migrated_entry_still_has_a_date_to_be_judged_by(legacy_agent_root):
    """Staleness and archive proposals ask when anyone last wanted the document.

    Moving the number to the write column must not make it undatable — that would
    present every pre-migration document as never touched.
    """
    data = read_all_meta(legacy_agent_root / "knowledge")

    assert last_touched(data["alpha.md"]) is not None
    assert last_touched(data["beta.md"]) is None   # this one genuinely has no date


# --------------------------------------------------------------------------
# F2 — injection log written before keys and before addresses
# --------------------------------------------------------------------------

def test_a_log_without_addresses_still_loads(legacy_agent_root):
    """7161 of 7170 lines on the live agent have neither field. A reader that needs
    them would silently return nothing and take decay down with it."""
    counts = _build_access_counts(legacy_agent_root)

    assert counts  # not empty
    assert counts.injections_by_key["alpha.md"] == 2


def test_a_bare_name_in_the_old_log_does_not_credit_the_document_it_resembles(legacy_agent_root):
    """The old log says `alpha.md`; the index says `knowledge/alpha.md`.

    They are different strings and must stay different: matching them would revive the
    bucket where one name held 49 files, and it would do it as a silent gift of history
    to whichever document happens to share a basename.
    """
    counts = _build_access_counts(legacy_agent_root)
    current = {"source_file": "knowledge/alpha.md",
               "source_path": str(legacy_agent_root / "knowledge" / "alpha.md")}

    assert counts.injections_for(current) == 0
    assert counts.for_document(current) == 0


def test_a_read_recorded_before_keys_still_counts_by_its_path(legacy_agent_root):
    """Reads were always recorded as the agent typed them — an absolute path outside
    the sandbox — and that half of the history is not stale, because a path means the
    same thing now as it did then."""
    doc = legacy_agent_root / "knowledge" / "alpha.md"
    (legacy_agent_root / "logs" / "tools.jsonl").write_text(
        '{"ts": "2026-04-20T11:00:00+00:00", "tool": "read_file", "args": {"path": "%s"}}\n'
        % str(doc).replace("\\", "\\\\"),
        encoding="utf-8",
    )

    counts = _build_access_counts(legacy_agent_root)

    assert counts.reads_for({"source_file": "knowledge/alpha.md", "source_path": str(doc)}) == 1


# --------------------------------------------------------------------------
# F1 — graph nodes from before the key was the identity
# --------------------------------------------------------------------------

grafeo = pytest.importorskip("grafeo")

from dpc_client_core.dpc_agent.knowledge_graph import KnowledgeGraph  # noqa: E402


@pytest.fixture(params=["sqlite", "grafeo"])
def kg(request, tmp_path):
    agent_root = tmp_path / "graph_agent"
    agent_root.mkdir(parents=True)
    instance = KnowledgeGraph(agent_root, backend=request.param)
    yield instance
    instance.close()


def _knowledge_dir(tmp_path: pathlib.Path, name: str, stem: str) -> pathlib.Path:
    d = tmp_path / name
    d.mkdir()
    (d / f"{stem}.md").write_text(f"# {stem}\nbody", encoding="utf-8")
    return d


def test_importing_over_a_stem_node_produces_a_document_that_can_be_addressed(kg, tmp_path):
    """The node the old code wrote has no path, so nothing built from it is followable.

    Re-import must yield a node that does, at the key everything else uses — this is
    what the graph channel needs before it can print an address.
    """
    kg.backend.add_node(legacy_graph_node("alpha"))
    kdir = _knowledge_dir(tmp_path, "knowledge", "alpha")

    assert kg.bulk_import_knowledge_files(kdir, source_layer="L5") == 1

    current = kg.backend.get_node("kf:knowledge/alpha.md")
    assert current.properties["path"] == "knowledge/alpha.md"
    assert current.properties["source_path"] == str(kdir / "alpha.md")


def test_the_shared_layer_imports_over_a_node_that_calls_itself_L5(kg, tmp_path):
    """The production regression of 3841d66d, as a test.

    Every legacy node says L5, including the 303 documents of the shared layer. A guard
    that decided by the label refused all of them — `Bulk imported 0 L6` on six agents,
    1818 warnings, and it passed the suite because no test had a legacy row.
    """
    kg.backend.add_node(legacy_graph_node("commit-note"))
    l6 = _knowledge_dir(tmp_path, "shared-knowledge", "commit-note")

    assert kg.bulk_import_knowledge_files(l6, source_layer="L6") == 1

    current = kg.backend.get_node("kf:L6/commit-note.md")
    assert current.source_layer == "L6"
    assert current.properties["source_path"] == str(l6 / "commit-note.md")


def test_a_stem_node_is_not_a_seed_and_cannot_answer_for_the_document(kg, tmp_path):
    """Left behind, not adopted. A seed arrives as an index key; the stem is a name
    from a scheme that no longer identifies anything."""
    kg.backend.add_node(legacy_graph_node("alpha"))
    kg.bulk_import_knowledge_files(_knowledge_dir(tmp_path, "knowledge", "alpha"))

    assert kg.graph_expand(["alpha.md"], max_hops=1) == []
    assert kg.graph_expand(["alpha"], max_hops=1) == []
    assert kg.backend.get_node("kf:alpha") is not None      # still there, inert


def test_a_graph_result_carries_the_fields_a_hint_needs(kg, tmp_path):
    """What the graph channel hands the fuser has to be addressable like any other
    channel's, or it spends a slot on a name the agent cannot open."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "alpha.md").write_text("# Alpha\nsee [Beta](beta.md)", encoding="utf-8")
    (kdir / "beta.md").write_text("# Beta\nbody", encoding="utf-8")
    kg.backend.add_node(legacy_graph_node("alpha"))         # legacy row alongside
    kg.bulk_import_knowledge_files(kdir)
    kg.extract_structural_edges(kdir)

    results = kg.graph_expand(["knowledge/alpha.md"], max_hops=1)

    assert results, "the seed key should reach beta through the structural edge"
    meta, _score = results[0]
    assert meta["source_file"] == "knowledge/beta.md"
    assert hint_address(meta) == "knowledge/beta.md"
