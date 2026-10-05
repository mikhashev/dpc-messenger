"""BM25 keyword index (ADR-010, MEM-3.5, ADR-020 Layer 1).

Character bigram tokenization for CJK/Arabic/Thai per DDA #12.
Whitespace tokenization for Latin/Cyrillic scripts.
Stop words via stopwordsiso (57 languages, Rule 14 Solution Check).
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import shutil
import unicodedata
from typing import List, Optional, Set, Tuple

from .index_meta import atomic_write_text, replace_when_the_readers_let_go

log = logging.getLogger(__name__)

_CJK_RANGES = re.compile(
    r"[\u2E80-\u9FFF\uF900-\uFAFF\U00020000-\U0002A6DF"
    r"\u0600-\u06FF\u0750-\u077F"  # Arabic
    r"\u0E00-\u0E7F]"  # Thai
)


def _detect_script(text: str) -> str:
    sample = text[:500]
    cjk_count = len(_CJK_RANGES.findall(sample))
    return "bigram" if cjk_count > len(sample) * 0.15 else "whitespace"


def _load_stopwords(langs: list[str] = ("ru", "en")) -> frozenset:
    """Load stop words from stopwordsiso JSON (bypasses pkg_resources)."""
    import importlib.util
    spec = importlib.util.find_spec("stopwordsiso")
    if spec and spec.submodule_search_locations:
        data_path = pathlib.Path(spec.submodule_search_locations[0]) / "stopwords-iso.json"
        if data_path.exists():
            data = json.loads(data_path.read_text(encoding="utf-8"))
            combined: set = set()
            for lang in langs:
                combined.update(data.get(lang, []))
            log.info("Loaded %d stop words for %s from stopwordsiso", len(combined), "+".join(langs))
            return frozenset(combined)
    log.warning("stopwordsiso not available, using empty stop words")
    return frozenset()


_STOP_WORDS: frozenset = _load_stopwords(["ru", "en"])


def _tokenize_whitespace(text: str, extra_stops: frozenset = frozenset()) -> List[str]:
    stops = _STOP_WORDS | extra_stops if extra_stops else _STOP_WORDS
    return [w.lower() for w in text.split() if len(w) > 1 and w.lower() not in stops]


def _tokenize_bigram(text: str) -> List[str]:
    text = text.lower()
    return [text[i:i+2] for i in range(len(text) - 1) if not text[i].isspace()]


def tokenize(text: str, extra_stops: frozenset = frozenset()) -> List[str]:
    script = _detect_script(text)
    if script == "bigram":
        return _tokenize_bigram(text)
    return _tokenize_whitespace(text, extra_stops)


TEXTS_FILE = "bm25_texts.json"
# Written into TEXTS_FILE. A store without it, or with another number, is the old
# format: its rows were built from previews and every row's full text is unknown.
CORPUS_VERSION = 2


def _texts_document(texts) -> str:
    return json.dumps({"corpus_version": CORPUS_VERSION, "texts": texts}, ensure_ascii=False)


class BM25Index:
    """BM25 keyword search index with disk persistence.

    Every add and remove rebuilds the whole corpus, so the index keeps the full text
    of every row in its own file (TEXTS_FILE), aligned with the chunk list.
    `meta["text"]` is the preview recall prints and is never a rebuild source: a row
    rebuilt from it is searchable on its first 500 characters only. The texts are read
    only when a mutation needs them, because recall loads this index on every turn.
    A row whose full text is unknown (an index older than the file) is held as None
    and falls back to its preview until the startup sync supplies the text.
    """

    CORPUS_MAX_DF = 0.8

    def __init__(self, index_dir: Optional[pathlib.Path] = None):
        self.index_dir = index_dir
        self._retriever = None
        self._chunk_metas: List[dict] = []
        # None as a whole: not read from disk yet. None as an element: unknown.
        self._texts: Optional[List[Optional[str]]] = []
        self._corpus_stop_words: frozenset = frozenset()
        self._batching = False
        self._pending_texts: List[str] = []
        self._pending_metas: List[dict] = []

    def _full_texts(self) -> List[Optional[str]]:
        """The stored full texts, read on first need, always as long as the metas."""
        if self._texts is None:
            texts: list = []
            path = self.index_dir / TEXTS_FILE if self.index_dir is not None else None
            if path is not None and path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and loaded.get("corpus_version") == CORPUS_VERSION:
                        texts = loaded.get("texts") or []
                except Exception as e:
                    log.warning("BM25 full texts unreadable, rows fall back to previews: %s", e)
            if len(texts) != len(self._chunk_metas):
                if texts:
                    log.warning("BM25 full texts disagree with the chunk list (%d vs %d) — "
                                "every row's full text treated as unknown",
                                len(texts), len(self._chunk_metas))
                texts = [None] * len(self._chunk_metas)
            self._texts = texts
        return self._texts

    def _rebuild(self, texts: List[Optional[str]], metas: List[dict]) -> None:
        """Rebuild from full texts; a row without one falls back to its preview."""
        if not metas:
            self.clear()
            return
        corpus = [t if t is not None else m.get("text", "") for t, m in zip(texts, metas)]
        self.build(corpus, metas)
        self._texts = list(texts)

    def clear(self) -> None:
        self._retriever = None
        self._chunk_metas = []
        self._texts = []
        self._corpus_stop_words = frozenset()

    def sources_missing_full_text(self) -> Set[str]:
        """Keys whose keyword row was built from the preview, not the document."""
        texts = self._full_texts()
        return {m.get("source_file", "") for t, m in zip(texts, self._chunk_metas) if t is None}

    def _compute_corpus_stops(self, texts: List[str]) -> frozenset:
        """Layer 2: words appearing in >80% of documents are corpus-specific noise."""
        from collections import Counter
        if len(texts) < 5:
            return frozenset()
        doc_freq: Counter = Counter()
        for text in texts:
            tokens = set(tokenize(text))
            doc_freq.update(tokens)
        n_docs = len(texts)
        stops = frozenset(
            tok for tok, freq in doc_freq.items()
            if freq / n_docs > self.CORPUS_MAX_DF
        )
        if stops:
            log.info("Layer 2: %d corpus-adaptive stop words (max_df=%.1f, %d docs)", len(stops), self.CORPUS_MAX_DF, n_docs)
        return stops

    def build(self, texts: List[str], chunk_metas: List[dict]) -> None:
        import bm25s
        self._corpus_stop_words = self._compute_corpus_stops(texts)
        corpus_tokens = [tokenize(t, self._corpus_stop_words) for t in texts]
        self._retriever = bm25s.BM25()
        self._retriever.index(corpus_tokens)
        self._chunk_metas = chunk_metas
        self._texts = list(texts)

    def add(self, texts: List[str], chunk_metas: List[dict]) -> None:
        """Append new documents and rebuild the BM25 index.

        In batch mode (between begin_batch/end_batch), defers the rebuild.
        """
        if self._batching:
            self._pending_texts.extend(texts)
            self._pending_metas.extend(chunk_metas)
            return
        self._rebuild(self._full_texts() + list(texts), self._chunk_metas + list(chunk_metas))

    def begin_batch(self) -> None:
        """Start accumulating add() calls without rebuilding."""
        self._batching = True
        self._pending_texts: List[str] = []
        self._pending_metas: List[dict] = []

    def end_batch(self) -> None:
        """Flush accumulated chunks and rebuild BM25 once."""
        self._batching = False
        if self._pending_texts:
            self._rebuild(self._full_texts() + list(self._pending_texts),
                          self._chunk_metas + self._pending_metas)
        self._pending_texts = []
        self._pending_metas = []

    def search(self, query: str, top_k: int = 5) -> List[Tuple[dict, float]]:
        if self._retriever is None or not self._chunk_metas:
            return []
        import bm25s
        query_tokens = tokenize(query, self._corpus_stop_words)
        results, scores = self._retriever.retrieve(
            bm25s.tokenize([" ".join(query_tokens)]),
            k=min(top_k, len(self._chunk_metas)),
        )
        out = []
        seen_files: set = set()
        for idx, score in zip(results[0], scores[0]):
            if 0 <= idx < len(self._chunk_metas) and score > 0:
                fname = self._chunk_metas[idx].get("source_file", "")
                if fname not in seen_files:
                    seen_files.add(fname)
                    out.append((self._chunk_metas[idx], float(score)))
        return out

    def remove_by_source(self, source_file: str) -> int:
        """Remove all documents from a specific source file and rebuild."""
        removed = self._remove_where(lambda key: key == source_file)
        if removed:
            log.info("Removed %d BM25 docs for %s, %d remaining", removed, source_file, len(self._chunk_metas))
        return removed

    def _remove_where(self, drop) -> int:
        if not self._chunk_metas:
            return 0
        keep_texts, keep_metas = [], []
        for text, meta in zip(self._full_texts(), self._chunk_metas):
            if not drop(meta.get("source_file")):
                keep_texts.append(text)
                keep_metas.append(meta)
        removed = len(self._chunk_metas) - len(keep_metas)
        if removed:
            self._rebuild(keep_texts, keep_metas)
        return removed

    def remove_by_sources(self, source_files) -> int:
        """One pass over the corpus, one build. See FaissIndex.remove_by_sources."""
        drop = {s for s in source_files if s}
        if not drop:
            return 0
        removed = self._remove_where(lambda key: key in drop)
        if removed:
            log.info("Removed %d BM25 docs for %d sources, %d remaining",
                     removed, len(drop), len(self._chunk_metas))
        return removed

    def save(self) -> None:
        """Persist the index without ever leaving a half-written one on disk.

        Readers are not serialised against writers — `load()` answers a truncated file
        by returning False, which is not an error anybody sees but one turn of recall
        returning nothing. The two side files are replaced in one step. The index
        itself is a *directory*, which cannot be replaced in one step on Windows, so it
        is built beside the live one and swapped: the window shrinks from "a reader may
        parse half a file" to "a reader may find no directory for a moment", and the
        second is a case `load()` already handles by name.
        """
        if self.index_dir is None:
            return
        if self._retriever is None:
            # Emptied by removals: the last saved corpus must not come back on load.
            if not self._chunk_metas and (self.index_dir / "bm25_chunks.json").exists():
                shutil.rmtree(self.index_dir / "bm25", ignore_errors=True)
                atomic_write_text(self.index_dir / TEXTS_FILE, _texts_document([]))
                atomic_write_text(self.index_dir / "bm25_chunks.json", "[]")
            return
        self.index_dir.mkdir(parents=True, exist_ok=True)
        live = self.index_dir / "bm25"
        staged = self.index_dir / "bm25.new"
        previous = self.index_dir / "bm25.old"
        shutil.rmtree(staged, ignore_errors=True)
        self._retriever.save(str(staged))
        shutil.rmtree(previous, ignore_errors=True)
        # Both renames go through the same patient replace as the files: Windows
        # refuses to rename a directory while anything inside it is open, and a reader
        # is inside it for the length of three `np.load` calls.
        if live.exists():
            replace_when_the_readers_let_go(live, previous)
        replace_when_the_readers_let_go(staged, live)
        shutil.rmtree(previous, ignore_errors=True)

        # Texts before the chunk list: a cut between the two leaves them disagreeing in
        # length, which `_full_texts` reads as "unknown" rather than misaligned. Skipped
        # when this instance never read them, because then it did not change them.
        if self._texts is not None:
            atomic_write_text(self.index_dir / TEXTS_FILE, _texts_document(self._texts))
        atomic_write_text(self.index_dir / "bm25_chunks.json",
                          json.dumps(self._chunk_metas, ensure_ascii=False))
        # Written even when empty: skipping it left the previous corpus's stop words on
        # disk, and the next load would tokenise this corpus through them.
        atomic_write_text(self.index_dir / "bm25_corpus_stops.json",
                          json.dumps(sorted(self._corpus_stop_words), ensure_ascii=False))
        log.info("Saved BM25 index: %d documents, %d corpus stops", len(self._chunk_metas), len(self._corpus_stop_words))

    def load(self) -> bool:
        if self.index_dir is None:
            return False
        bm25_dir = self.index_dir / "bm25"
        chunks_path = self.index_dir / "bm25_chunks.json"
        if not bm25_dir.exists() or not chunks_path.exists():
            return False
        try:
            import bm25s
            self._retriever = bm25s.BM25.load(str(bm25_dir))
            self._chunk_metas = json.loads(chunks_path.read_text(encoding="utf-8"))
            self._texts = None  # read on the first mutation, see _full_texts
            # The same refusal the vector channel makes, for the same reason: `search`
            # maps a retrieved row number into `_chunk_metas`, so a list that disagrees
            # with the corpus does not fail — it answers, with another document's name.
            # The directory and the chunk list are two files written one after the other,
            # so a process cut between them leaves exactly that. Reproduced by an external
            # reviewer in three lines: three rows against a two-item list returned f2 for
            # "kotler" and f0 for "warren".
            rows = (self._retriever.scores or {}).get("num_docs")
            if rows is not None and rows != len(self._chunk_metas):
                log.warning(
                    "BM25 index and chunk list disagree (%s rows, %d metas) — refusing to load",
                    rows, len(self._chunk_metas),
                )
                self.clear()
                return False
            stops_path = self.index_dir / "bm25_corpus_stops.json"
            if stops_path.exists():
                self._corpus_stop_words = frozenset(json.loads(stops_path.read_text(encoding="utf-8")))
            return True
        except Exception as e:
            log.warning("Failed to load BM25 index: %s", e)
            return False

    @property
    def total_documents(self) -> int:
        return len(self._chunk_metas)
