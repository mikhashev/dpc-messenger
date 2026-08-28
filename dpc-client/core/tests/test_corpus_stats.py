"""Per-corpus evidence for the question of what belongs in the index.

The decision was nearly taken from a one-off script and a single day of data. What the
report has to do, therefore, is not recommend but state — including how much of its own
evidence it had to discard, so nobody reads a thin sample as a firm one.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from dpc_client_core.dpc_agent.corpus_stats import corpus_of, corpus_stats


def _agent(tmp_path: pathlib.Path, keys) -> pathlib.Path:
    root = tmp_path / "dpc" / "agents" / "agent_x"
    (root / "state" / "memory_index").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (root / "knowledge").mkdir(parents=True)
    (root / "state" / "memory_index" / "index_meta.json").write_text(
        json.dumps({"file_hashes": {k: "h" for k in keys},
                    "header": {"model_name": "m", "key_format": "x"}}),
        encoding="utf-8")
    return root


def _injections(root: pathlib.Path, entries) -> None:
    (root / "state" / "knowledge_access.jsonl").write_text(
        "".join(json.dumps({"ts": ts, "mode": "full", "files": files,
                            "addresses": [], "useful": None}) + "\n"
                for ts, files in entries),
        encoding="utf-8")


def _reads(root: pathlib.Path, entries) -> None:
    (root / "logs" / "tools.jsonl").write_text(
        "".join(json.dumps({"ts": ts, "tool": "read_file", "args": {"path": p}}) + "\n"
                for ts, p in entries),
        encoding="utf-8")


# --------------------------------------------------------------------------
# Which corpus a key belongs to
# --------------------------------------------------------------------------

@pytest.mark.parametrize("key,expected", [
    ("knowledge/note.md", "knowledge"),
    ("L6/shared.md", "L6"),
    ("EXT/dpc-messenger/README.md", "EXT/dpc-messenger"),
    ("EXT/dpc-messenger/docs/deep/file.md", "EXT/dpc-messenger"),
])
def test_a_key_names_the_corpus_a_decision_would_be_about(key, expected):
    """The unit is the root, because the root is what the indexed-paths setting turns
    on and off — grouping by anything else produces a report nobody can act on."""
    assert corpus_of(key) == expected


# --------------------------------------------------------------------------
# The three columns
# --------------------------------------------------------------------------

def test_documents_come_from_the_index_not_from_the_logs(tmp_path):
    root = _agent(tmp_path, ["knowledge/a.md", "L6/b.md", "L6/c.md"])

    report = corpus_stats(root, None, "agent_x")

    by_name = {c["corpus"]: c for c in report["corpora"]}
    assert by_name["L6"]["documents"] == 2
    assert by_name["knowledge"]["documents"] == 1
    assert report["documents_total"] == 3


def test_reads_are_attributed_by_path_because_the_key_scheme_moved(tmp_path):
    """Reads survive a respelling of keys — they name a location, not a key. That is
    what makes this column the trustworthy half of the report."""
    root = _agent(tmp_path, ["knowledge/a.md"])
    doc = root / "knowledge" / "a.md"
    doc.write_text("x", encoding="utf-8")
    _reads(root, [("2026-08-02T10:00:00+00:00", str(doc))])

    report = corpus_stats(root, None, "agent_x")

    assert {c["corpus"]: c["opened"] for c in report["corpora"]}["knowledge"] == 1


def test_a_corpus_nobody_opens_shows_up_as_zero_rather_than_absent(tmp_path):
    """The whole point of the report: a root with documents and no reads has to be
    visible, and it is invisible if empty rows are dropped."""
    root = _agent(tmp_path, ["EXT/unused/a.md", "EXT/unused/b.md"])
    doc = root / "knowledge" / "a.md"
    doc.write_text("x", encoding="utf-8")
    _reads(root, [("2026-08-02T10:00:00+00:00", str(doc))])

    report = corpus_stats(root, None, "agent_x")

    unused = [c for c in report["corpora"] if c["corpus"] == "EXT/unused"]
    assert unused and unused[0]["documents"] == 2 and unused[0]["opened"] == 0


# --------------------------------------------------------------------------
# Saying how thin the evidence is
# --------------------------------------------------------------------------

def test_injections_under_a_previous_key_scheme_are_excluded(tmp_path):
    """`EXT/README.md` was a key before the roots grew tails; no document answers to
    it now. Counting it would credit slots to a corpus by name only."""
    root = _agent(tmp_path, ["EXT/project/README.md"])
    doc = root / "knowledge" / "a.md"
    doc.write_text("x", encoding="utf-8")
    _reads(root, [("2026-08-02T08:00:00+00:00", str(doc))])
    _injections(root, [
        ("2026-08-02T09:00:00+00:00", ["EXT/README.md", "EXT/project/README.md"]),
    ])

    report = corpus_stats(root, None, "agent_x")

    by_name = {c["corpus"]: c for c in report["corpora"]}
    assert by_name["EXT/project"]["shown"] == 1


def test_the_discarded_injections_are_reported_not_hidden(tmp_path):
    """A reader has to be able to see that the sample is thin. Silently dropping 80%
    of the log and printing the rest is how a thin measurement passes for a firm one."""
    root = _agent(tmp_path, ["EXT/project/README.md"])
    doc = root / "knowledge" / "a.md"
    doc.write_text("x", encoding="utf-8")
    _reads(root, [("2026-08-02T08:00:00+00:00", str(doc))])
    _injections(root, [
        ("2026-08-02T09:00:00+00:00", ["EXT/README.md", "EXT/old.md",
                                       "EXT/project/README.md"]),
    ])

    report = corpus_stats(root, None, "agent_x")

    assert report["injections_counted"] == 1
    assert report["injections_ignored_old_scheme"] == 2


def test_an_agent_without_an_index_reports_nothing_rather_than_failing(tmp_path):
    root = tmp_path / "agents" / "fresh"
    root.mkdir(parents=True)

    report = corpus_stats(root, None, "fresh")

    assert report["corpora"] == [] and report["documents_total"] == 0


def test_corpora_are_ordered_by_what_they_contribute(tmp_path):
    """Largest first: a decision about the corpus starts with what occupies it."""
    root = _agent(tmp_path, ["L6/a.md", "L6/b.md", "L6/c.md", "knowledge/x.md"])

    report = corpus_stats(root, None, "agent_x")

    assert [c["corpus"] for c in report["corpora"]] == ["L6", "knowledge"]
