"""The third damaged state: a short index behind a full map.

Two guards already exist. `FaissIndex.load()` refuses when the rows and the chunk list
disagree, and `map_outlives_index` rebuilds when a full map sits over an empty index.
Neither sees this one: the index is neither torn nor empty, just short, and the map says
everything is present — so the next pass finds nothing to do and the missing documents
stay missing until something else forces a rebuild.

It arrives through shutdown. The embedding loop `break`s on the stop event and the meta
write below it still runs. Found by both external reviewers, independently, on
2026-08-12, in code that had been read several times that day without anybody seeing it.
"""

import pytest

from dpc_client_core.dpc_agent.indexing_pipeline import keep_only_what_landed


def _planned(n):
    return [(f"L5/doc{i}.md", f"text {i}", {"source_file": f"L5/doc{i}.md"}) for i in range(n)]


def test_an_interrupted_pass_leaves_the_rest_stale():
    planned = _planned(5)
    hashes = {src: "h" for src, _, _ in planned}
    hashes["L5/untouched.md"] = "kept"  # a document the pass never planned to embed

    keep_only_what_landed(hashes, planned, embedded=2)

    assert sorted(hashes) == ["L5/doc0.md", "L5/doc1.md", "L5/untouched.md"]


def test_a_pass_that_finished_keeps_every_hash():
    planned = _planned(3)
    hashes = {src: "h" for src, _, _ in planned}

    keep_only_what_landed(hashes, planned, embedded=3)

    assert len(hashes) == 3


def test_a_pass_that_embedded_nothing_claims_nothing():
    planned = _planned(3)
    hashes = {src: "h" for src, _, _ in planned}

    keep_only_what_landed(hashes, planned, embedded=0)

    assert hashes == {}


def test_the_sync_pass_asks_before_committing_its_map():
    """The loop it protects lives in a closure a unit test cannot reach."""
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1]
              / "dpc_client_core" / "managers" / "agent_manager.py").read_text(encoding="utf-8")

    assert "keep_only_what_landed(new_hashes, to_embed, embedded)" in source, (
        "the file map must be trimmed to what the pass actually embedded — writing it "
        "whole after an interrupted loop hides the missing documents from every later start"
    )
