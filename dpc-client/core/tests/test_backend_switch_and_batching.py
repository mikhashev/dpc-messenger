"""What the stored index says about itself, and what it costs to write.

Two defects with one shape: a fact the index knows but nobody wrote down.

The first is who built it. Flip `retrieval_vector` and the file hashes still match
every document, so the incremental pass finds nothing to do and the new backend is
handed an index it never wrote — empty, and permanently so, because every later
start reads the same agreeing map.

The second is that the wrapper the indexing pass talks to did not forward the two
methods that exist precisely to stop the text index rebuilding once per batch. The
request went through `hasattr` and was answered "no" in silence.
"""

import json
import pathlib

import numpy as np
import pytest

from dpc_client_core.dpc_agent.faiss_index import FaissIndex
from dpc_client_core.dpc_agent.index_keys import KEY_FORMAT
from dpc_client_core.dpc_agent.index_meta import read_meta, write_meta
from dpc_client_core.dpc_agent.indexing_pipeline import map_outlives_index, rebuild_decision
from dpc_client_core.dpc_agent.retrieval import (
    backend_id_from_config,
    resolve_backend_id,
)
from dpc_client_core.dpc_agent.retrieval.native import NativeTextIndex
from dpc_client_core.dpc_agent.retrieval.base import TextAddItem


def _write_header(index_dir: pathlib.Path, **fields) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    header = {"model_name": "BAAI/bge-m3", "key_format": KEY_FORMAT}
    header.update(fields)
    write_meta(index_dir / "index_meta.json", {"header": header, "chunks": []})


# --- who built the index ----------------------------------------------------


def test_an_index_built_by_another_backend_is_rebuilt(tmp_path):
    """The hashes cannot say this. They describe the corpus, not the writer."""
    index_dir = tmp_path / "memory_index"
    _write_header(index_dir, backend="grafeo+grafeo")

    decision = rebuild_decision(index_dir, "BAAI/bge-m3", "native+native")

    assert decision.needed
    assert "grafeo+grafeo" in decision.message  # the log has to name what it found
    assert "native+native" in decision.message


def test_an_index_built_by_this_backend_is_left_alone(tmp_path):
    index_dir = tmp_path / "memory_index"
    _write_header(index_dir, backend="native+native")

    decision = rebuild_decision(index_dir, "BAAI/bge-m3", "native+native")

    assert not decision.needed
    assert decision.message == ""


def test_an_index_written_before_the_marker_existed_is_not_rebuilt(tmp_path):
    """Absent is not a mismatch.

    Every index on disk today predates this field. Reading absence as "some other
    backend" would re-embed every pool on the fleet's next start, for nothing — the
    sync stamps the field instead and the comparison protects from then on.
    """
    index_dir = tmp_path / "memory_index"
    _write_header(index_dir)  # no `backend` key at all

    assert not rebuild_decision(index_dir, "BAAI/bge-m3", "native+native").needed


def test_the_id_names_both_channels_and_ignores_the_fuser(tmp_path):
    """Only the two channels write. A fuser cannot make a stored index answer wrongly."""
    assert backend_id_from_config({}) == "native+native"
    assert backend_id_from_config({"retrieval_vector": "grafeo"}) == "grafeo+native"
    assert backend_id_from_config(
        {"retrieval_vector": "grafeo", "retrieval_text": "grafeo", "retrieval_fusion": "grafeo"}
    ) == "grafeo+grafeo"


def test_the_decision_reads_the_backend_from_where_the_factory_reads_it(tmp_path):
    """The rebuild decision runs before the backend is built, so it has to ask the
    config — and it must ask through the same function, not a second opinion."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / "config.json").write_text(
        json.dumps({"retrieval_vector": "grafeo", "retrieval_text": "grafeo"}),
        encoding="utf-8",
    )

    assert resolve_backend_id(agent_root) == "grafeo+grafeo"
    # No config.json is the native default, not an error.
    assert resolve_backend_id(tmp_path / "nothing_here") == "native+native"


# --- the index and its chunk list have to agree -----------------------------


def test_an_index_whose_rows_outnumber_its_chunks_refuses_to_load(tmp_path):
    """328 vectors against 23 chunks looked healthy from every angle but this one.

    `search` maps a row number into the chunk list, so the surplus rows are
    unreachable and the survivors answer for documents that are not theirs.
    """
    index_dir = tmp_path / "memory_index"
    idx = FaissIndex(index_dir, model_name="m", dimensions=4)
    idx.add(np.ones((3, 4), dtype=np.float32),
            [{"source_file": f"f{i}.md", "text": "x"} for i in range(3)])
    idx.save()

    doc = read_meta(index_dir / "index_meta.json")
    doc["chunks"] = doc["chunks"][:1]  # the collision, reproduced
    write_meta(index_dir / "index_meta.json", doc)

    reopened = FaissIndex(index_dir, model_name="m", dimensions=4)
    assert reopened.load() is False
    assert reopened.total_vectors == 0  # and it does not keep the mismatched state


def test_an_index_that_agrees_with_its_chunks_still_loads(tmp_path):
    index_dir = tmp_path / "memory_index"
    idx = FaissIndex(index_dir, model_name="m", dimensions=4)
    idx.add(np.ones((3, 4), dtype=np.float32),
            [{"source_file": f"f{i}.md", "text": "x"} for i in range(3)])
    idx.save()

    reopened = FaissIndex(index_dir, model_name="m", dimensions=4)
    assert reopened.load() is True
    assert reopened.total_vectors == 3


def test_an_empty_index_is_legitimate_and_loads(tmp_path):
    """Zero rows against zero chunks is a fresh index, not damage."""
    index_dir = tmp_path / "memory_index"
    FaissIndex(index_dir, model_name="m", dimensions=4).save()

    assert FaissIndex(index_dir, model_name="m", dimensions=4).load() is True


# --- the map must not outlive the index it describes ------------------------


def test_a_full_map_over_an_empty_index_is_disagreement(tmp_path):
    """The marker cannot catch this one: every index on disk predates the marker.

    A grafeo agent switched to native today carries a map listing every document and
    no native index at all. Nothing is re-embedded, and the next start agrees again.
    """
    assert map_outlives_index(loaded=True, indexed_items=0, mapped_documents=2066)
    assert map_outlives_index(loaded=False, indexed_items=0, mapped_documents=2066)
    # a load that refused (rows against chunks) reads the same way
    assert map_outlives_index(loaded=False, indexed_items=328, mapped_documents=328)


def test_an_index_that_answers_is_left_alone(tmp_path):
    assert not map_outlives_index(loaded=True, indexed_items=2066, mapped_documents=2066)
    # Drift is not this failure. Rebuilding the fleet over an off-by-one is not the
    # cheaper mistake.
    assert not map_outlives_index(loaded=True, indexed_items=2065, mapped_documents=2066)
    # First run: nothing mapped, nothing indexed, nothing wrong.
    assert not map_outlives_index(loaded=False, indexed_items=0, mapped_documents=0)


def test_the_per_file_sync_asks_before_trusting_its_map():
    """A unit test cannot reach that closure; the property is narrow enough to state."""
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "dpc_client_core" / "managers" / "agent_manager.py").read_text(encoding="utf-8")

    assert "map_outlives_index(" in source, (
        "the incremental pass must check that the index behind its file map still "
        "exists — a map alone cannot tell an indexed corpus from a vanished index"
    )
    assert "resolve_backend_id(agent_root)" in source, (
        "the rebuild decision needs the backend the factory would build, read from "
        "the same place the factory reads it"
    )


# --- one rebuild per pass, not one per batch --------------------------------


_WORDS = ["kotler", "warren", "forge", "pulse", "scout", "muse", "atlas", "harbour"]


def _items(n, start=0):
    # Distinct wording per document: with the same words everywhere the corpus-adaptive
    # stop list (max_df=0.8) drops all of them and bm25s indexes an empty corpus.
    return [
        TextAddItem(text=f"{_WORDS[i % len(_WORDS)]} indexing", meta={"source_file": f"f{i}.md"})
        for i in range(start, start + n)
    ]


def test_the_wrapper_forwards_the_batching_the_indexing_pass_asks_for(tmp_path):
    """The pass asks behind `hasattr`, so a missing method is a silent "no"."""
    text = NativeTextIndex(tmp_path)
    assert hasattr(text, "begin_batch") and hasattr(text, "end_batch")


def test_adds_inside_a_batch_rebuild_once_not_once_each(tmp_path, monkeypatch):
    text = NativeTextIndex(tmp_path)
    builds = []
    inner = text._inner
    real_build = inner.build
    monkeypatch.setattr(inner, "build",
                        lambda texts, metas: (builds.append(len(texts)), real_build(texts, metas))[1])

    text.begin_batch()
    for i in range(0, 6, 2):
        text.add(_items(2, start=i))
    assert builds == []  # nothing rebuilt while the batch is open
    text.end_batch()

    assert builds == [6]  # one rebuild, over the whole corpus
    # and the one rebuild left an index that answers for every document in it
    assert [hit["source_file"] for hit, _ in text.search("kotler", top_k=1)] == ["f0.md"]
    assert [hit["source_file"] for hit, _ in text.search("scout", top_k=1)] == ["f4.md"]


def test_without_a_batch_every_add_still_rebuilds(tmp_path, monkeypatch):
    """The unbatched path is unchanged — this is what makes the batch worth asking for."""
    text = NativeTextIndex(tmp_path)
    builds = []
    inner = text._inner
    real_build = inner.build
    monkeypatch.setattr(inner, "build",
                        lambda texts, metas: (builds.append(len(texts)), real_build(texts, metas))[1])

    for i in range(0, 6, 2):
        text.add(_items(2, start=i))

    assert builds == [2, 4, 6]  # the whole corpus, three times over
