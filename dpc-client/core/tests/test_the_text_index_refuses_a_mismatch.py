"""The refusal the vector channel already makes, in the channel that lacked it.

`BM25Index.search` maps a retrieved row number into `_chunk_metas`, so a list that
disagrees with the corpus does not fail — it answers, with another document's name. An
external reviewer reproduced exactly that on 2026-08-12: three rows against a two-item
list returned f2 for "kotler" and f0 for "warren".

The state arrives on its own. The directory and the chunk list are two files written one
after the other, and the writer thread is a daemon that shutdown can cut between them.
"""

import json

from dpc_client_core.dpc_agent.bm25_index import BM25Index

def test_the_text_index_refuses_a_chunk_list_that_disagrees_with_its_rows(tmp_path):
    """`search` maps a row number into the chunk list, so a mismatch does not fail —
    it answers with another document's name. Reproduced by an external reviewer."""
    index = BM25Index(tmp_path)
    docs = ["kotler indexing", "warren indexing", "forge indexing"]
    index.build(docs, [{"source_file": f"f{i}.md", "text": t} for i, t in enumerate(docs)])
    index.save()

    chunks = tmp_path / "bm25_chunks.json"
    kept = json.loads(chunks.read_text(encoding="utf-8"))[:2]
    chunks.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")

    reopened = BM25Index(tmp_path)
    assert reopened.load() is False
    assert reopened.total_documents == 0  # and it keeps nothing of the mismatched state


def test_the_text_index_still_loads_when_they_agree(tmp_path):
    index = BM25Index(tmp_path)
    docs = ["kotler indexing", "warren indexing"]
    index.build(docs, [{"source_file": f"f{i}.md", "text": t} for i, t in enumerate(docs)])
    index.save()

    reopened = BM25Index(tmp_path)
    assert reopened.load() is True
    assert [m["source_file"] for m, _ in reopened.search("kotler", top_k=1)] == ["f0.md"]


def test_the_stop_words_of_a_previous_corpus_do_not_survive_a_save(tmp_path):
    """An empty stop set used to skip the write, leaving the last corpus's list to
    tokenise the next one."""
    index = BM25Index(tmp_path)
    # Five documents at least — below that the corpus-adaptive pass declines to guess.
    shared = ["indexing recall", "indexing memory", "indexing search",
              "indexing graph", "indexing vector", "indexing text"]
    index.build(shared, [{"source_file": f"f{i}.md", "text": t} for i, t in enumerate(shared)])
    index.save()
    assert json.loads((tmp_path / "bm25_corpus_stops.json").read_text(encoding="utf-8")) == ["indexing"]

    distinct = ["kotler", "warren"]
    index.build(distinct, [{"source_file": f"g{i}.md", "text": t} for i, t in enumerate(distinct)])
    index.save()

    assert json.loads((tmp_path / "bm25_corpus_stops.json").read_text(encoding="utf-8")) == []
