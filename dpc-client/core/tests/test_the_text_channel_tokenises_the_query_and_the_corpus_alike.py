"""The keyword index tokenises its corpus and its query with one function.

The corpus used to be split on whitespace only, so `(@razzant)` stayed one token
with its punctuation, while the query also went through `bm25s.tokenize` and became
`razzant`; the two never met. A store built that way is rebuilt from its own texts.
"""

import json

import pytest

from dpc_client_core.dpc_agent import bm25_index
from dpc_client_core.dpc_agent.bm25_index import TEXTS_FILE, BM25Index, tokenize

DOCS = {
    "at-razzant.md": "Review notes from Ivan (@razzant) on the scheduler patch.",
    "repo-link.md": "The upstream fork lives at github.com/razzant/ouroboros), cloned for the audit.",
    "at-disentinel.md": "Credit for the race report goes to (@disentinel, who found it first.",
    "celanair.md": "Meeting with celanair: agreed to split the parser in two.",
    "grable.md": "Bibliography entry [grable] cites the network paper.",
    "razzhigaev.md": "Paper by razzhigaev about sparse attention heads.",
    "ouroboros.md": "The ouroboros agent rewrites its own prompts nightly.",
    "zemstvo.md": "zemstvo is the sibling project that decided the queue.",
}


def _metas(docs):
    return [{"source_file": k, "text": v[:500]} for k, v in docs.items()]


def _built(docs) -> BM25Index:
    index = BM25Index()
    index.build(list(docs.values()), _metas(docs))
    return index


def _hits(index, query):
    return [m["source_file"] for m, _ in index.search(query, top_k=10)]


def _containing(docs, word):
    return {k for k, v in docs.items() if word in v.lower()}


@pytest.mark.parametrize("word", [
    "razzant", "disentinel", "celanair", "grable", "razzhigaev", "ouroboros", "zemstvo",
])
def test_a_name_glued_to_punctuation_is_found_like_a_plain_one(word):
    hits = _hits(_built(DOCS), word)
    # Every document that holds the word, and only those, so its own ranks first.
    assert set(hits) == _containing(DOCS, word) and hits


def test_a_hyphenated_and_an_underscored_key_find_their_own_document_verbatim():
    docs = {
        "keys.md": "The node id is dpc-node-7f3a and the setting is max_recall_results.",
        "node.md": "node node node dpc dpc results results max max recall",
        "other.md": "dpc node results recall max, spelled apart in a list",
        "misc.md": "nothing relevant to the question at all",
        "misc2.md": "another unrelated paragraph about the weather",
    }
    index = _built(docs)
    assert _hits(index, "dpc-node-7f3a")[0] == "keys.md"
    assert _hits(index, "max_recall_results")[0] == "keys.md"
    # The words of a key answer too, so a key is not hidden behind its joiners.
    assert "keys.md" in _hits(index, "7f3a")


def test_a_cyrillic_word_beside_a_bracket_is_found():
    docs = dict(DOCS, **{"ru.md": "Встреча с Разжигаевым (о памяти агента) прошла хорошо."})
    assert _hits(_built(docs), "агента") == ["ru.md"]
    assert _hits(_built(docs), "Разжигаевым") == ["ru.md"]


def test_stop_words_are_dropped_on_both_sides_alike(monkeypatch):
    assert tokenize("the of and") == [] and tokenize("и в на") == []
    assert tokenize("The ouroboros, and the queue") == tokenize("ouroboros queue")

    # The corpus-adaptive stops apply to the query through the same function.
    shared = ["indexing recall", "indexing memory", "indexing search",
              "indexing graph", "indexing vector", "indexing text"]
    index = _built({f"f{i}.md": t for i, t in enumerate(shared)})
    assert index._corpus_stop_words == {"indexing"}
    assert _hits(index, "indexing") == []

    # No second tokenizer on the query side.
    import bm25s

    def second_tokenizer(*a, **k):
        raise AssertionError("bm25s.tokenize must not run on the query")

    monkeypatch.setattr(bm25s, "tokenize", second_tokenizer)
    assert _hits(index, "recall") == ["f0.md"]


def test_a_query_with_no_known_word_answers_nothing():
    index = _built(DOCS)
    assert _hits(index, "absentword") == []
    assert _hits(index, "the and") == []


# A store built by the old tokenizer --------------------------------------------

def _old_store(d, docs):
    """What a store saved before this change holds: a whitespace-split corpus, no
    tokenizer marker, full texts aligned with the chunk list."""
    import bm25s

    texts = list(docs.values())
    retriever = bm25s.BM25()
    retriever.index([[w.lower() for w in t.split() if len(w) > 1] for t in texts])
    retriever.save(str(d / "bm25"))
    chunks_json = json.dumps(_metas(docs), ensure_ascii=False)
    (d / TEXTS_FILE).write_bytes(bm25_index._texts_document(texts, chunks_json).encode("utf-8"))
    (d / "bm25_chunks.json").write_bytes(chunks_json.encode("utf-8"))
    (d / "bm25_corpus_stops.json").write_bytes(b"[]")


def test_an_old_store_is_rebuilt_from_its_texts_once_and_touches_no_embedding(tmp_path, monkeypatch):
    from dpc_client_core.dpc_agent import faiss_index, memory
    from dpc_client_core.dpc_agent.index_writer import write_index

    def no_embedding(*a, **k):
        raise AssertionError("a keyword rebuild must not reach the vector side")

    monkeypatch.setattr(memory, "get_embedding_provider", no_embedding)
    monkeypatch.setattr(faiss_index.FaissIndex, "__init__", no_embedding)

    _old_store(tmp_path, DOCS)
    builds = []
    real_build = BM25Index.build
    monkeypatch.setattr(BM25Index, "build",
                        lambda self, *a, **k: (builds.append(1), real_build(self, *a, **k))[1])

    index = BM25Index(tmp_path)
    assert index.load()
    # Answers at once, from the rebuilt corpus, before anything is saved.
    assert set(_hits(index, "razzant")) == {"at-razzant.md", "repo-link.md"}

    write_index(tmp_path, lambda: None)   # the save was queued on the index writer
    marker = json.loads((tmp_path / "bm25_tokenize.json").read_text(encoding="utf-8"))
    assert marker == {"tokenize_version": bm25_index.TOKENIZE_VERSION}
    stored = json.loads((tmp_path / TEXTS_FILE).read_text(encoding="utf-8"))
    assert stored["texts"] == list(DOCS.values())     # full texts kept, not previews
    assert not list(tmp_path.glob("*.faiss")) and not (tmp_path / "index_meta.json").exists()

    builds.clear()
    reopened = BM25Index(tmp_path)
    assert reopened.load()
    write_index(tmp_path, lambda: None)
    assert builds == []                               # once: the saved store is current
    assert _hits(reopened, "celanair") == ["celanair.md"]


def test_a_store_loaded_on_its_index_writer_is_saved_there(tmp_path):
    from dpc_client_core.dpc_agent.index_writer import write_index

    _old_store(tmp_path, DOCS)
    assert write_index(tmp_path, lambda: BM25Index(tmp_path).load())
    assert (tmp_path / "bm25_tokenize.json").exists()
