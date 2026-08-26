"""A count built over a log nobody could open is unknown, not zero.

Three instances of this class landed in one day: `tool_calls` absent on 7 504
turns read as "made no calls", a knowledge metric that read zero for months
while its deserialiser crashed, and `sender_type` absent on 186 messages.
Every one of them was a consumer that could not tell absent from empty.

A file that was never created is the easy half — nothing could have been
written to it, so its emptiness is established. A file that exists and refuses
to open is the half that has to raise.
"""
import json
import os

import pytest

from dpc_client_core.dpc_agent import tool_ledger
from dpc_client_core.dpc_agent.active_recall import (
    EvidenceReadFailed,
    _apply_decay,
    _build_access_counts,
)
from dpc_client_core.dpc_agent.hybrid_search import SearchResult
from dpc_client_core.dpc_agent.tool_ledger import (
    record_attempt,
    sweep_unfinished,
    unfinished_calls,
)


def _unreadable(path):
    """An existing path that cannot be read as a file, on Windows and POSIX both."""
    path.mkdir(parents=True)
    return path


@pytest.fixture
def agent_root(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "logs").mkdir()
    return tmp_path


# --- the ledger ---


def test_a_ledger_that_was_never_written_answers_none_were_left_open(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    assert unfinished_calls(logs) == []


def test_a_ledger_that_cannot_be_read_refuses_to_answer(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _unreadable(logs / "tools.jsonl")
    with pytest.raises(EvidenceReadFailed):
        unfinished_calls(logs)


def test_the_sweep_says_unknown_rather_than_nothing_was_abandoned(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    _unreadable(logs / "tools.jsonl")
    tool_ledger._swept_dirs.discard(str(logs))

    assert sweep_unfinished(logs) is None, "an empty list would read as an all-clear"

    events = [
        json.loads(line)
        for line in (logs / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [e["type"] for e in events] == ["evidence_read_failed"]
    assert events[0]["evidence"] == "tools.jsonl"


def test_an_empty_sweep_and_an_unreadable_one_are_different_answers(tmp_path):
    readable = tmp_path / "a" / "logs"
    readable.mkdir(parents=True)
    record_attempt(readable, tool="run_shell", tool_call_id="c1", args={})
    tool_ledger._swept_dirs.discard(str(readable))
    assert sweep_unfinished(readable) == []  # in flight in this very process

    broken = tmp_path / "b" / "logs"
    broken.mkdir(parents=True)
    _unreadable(broken / "tools.jsonl")
    tool_ledger._swept_dirs.discard(str(broken))
    assert sweep_unfinished(broken) is None


# --- the access counts, where the partial read is the dangerous one ---


def test_reads_that_could_not_be_read_do_not_become_a_document_nobody_opened(agent_root):
    """The injections are readable and the reads are not: every document would
    look shown-and-never-opened, which is a ranking, not an absence."""
    with open(agent_root / "state" / "knowledge_access.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": "2026-08-26T10:00:00+00:00", "files": ["knowledge/a.md"]}) + "\n")
    _unreadable(agent_root / "logs" / "tools.jsonl")

    with pytest.raises(EvidenceReadFailed):
        _build_access_counts(agent_root)


def test_ranking_is_left_alone_when_the_counts_cannot_be_built(agent_root):
    _unreadable(agent_root / "logs" / "tools.jsonl")
    results = [
        SearchResult(chunk_meta={"source_file": "knowledge/a.md"}, score=0.9, source="hybrid"),
        SearchResult(chunk_meta={"source_file": "knowledge/b.md"}, score=0.4, source="hybrid"),
    ]

    ranked = _apply_decay(results, agent_root)

    assert [r.score for r in ranked] == [0.9, 0.4]


def test_a_log_that_was_never_written_still_yields_established_counts(agent_root):
    counts = _build_access_counts(agent_root)
    assert not counts
    assert counts.reads_for({"source_file": "knowledge/a.md"}) == 0


# --- the report a person reads ---


def test_corpus_stats_reports_none_rather_than_zero_slots(agent_root, monkeypatch):
    from dpc_client_core.dpc_agent import corpus_stats as corpus_stats_mod

    index_dir = agent_root / "state" / "memory_index"
    index_dir.mkdir(parents=True)
    (index_dir / "index_meta.json").write_text(
        json.dumps({"file_hashes": {"knowledge/a.md": "h"}}), encoding="utf-8"
    )
    _unreadable(agent_root / "logs" / "tools.jsonl")

    report = corpus_stats_mod.corpus_stats(agent_root, firewall=None, agent_id="agent_001")

    assert report["injections_counted"] is None
    assert report["injections_ignored_old_scheme"] is None
    assert "evidence_read_failed" in report
    assert report["documents_total"] == 1
