"""BM25 keyword index (ADR-010, MEM-3.5).

Character bigram tokenization for CJK/Arabic/Thai per DDA #12.
Whitespace tokenization for Latin/Cyrillic scripts.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import unicodedata
from typing import List, Optional, Tuple

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


def _tokenize_whitespace(text: str) -> List[str]:
    return [w.lower() for w in text.split() if len(w) > 1]


def _tokenize_bigram(text: str) -> List[str]:
    text = text.lower()
    return [text[i:i+2] for i in range(len(text) - 1) if not text[i].isspace()]


def tokenize(text: str) -> List[str]:
    script = _detect_script(text)
    if script == "bigram":
        return _tokenize_bigram(text)
    return _tokenize_whitespace(text)


class BM25Index:
    """BM25 keyword search index with disk persistence."""

    def __init__(self, index_dir: Optional[pathlib.Path] = None):
        self.index_dir = index_dir
        self._retriever = None
        self._chunk_metas: List[dict] = []

    def build(self, texts: List[str], chunk_metas: List[dict]) -> None:
        import bm25s
        corpus_tokens = [tokenize(t) for t in texts]
        self._retriever = bm25s.BM25()
        self._retriever.index(corpus_tokens)
        self._chunk_metas = chunk_metas

    def search(self, query: str, top_k: int = 5) -> List[Tuple[dict, float]]:
        if self._retriever is None or not self._chunk_metas:
            return []
        import bm25s
        query_tokens = tokenize(query)
        results, scores = self._retriever.retrieve(
            bm25s.tokenize([" ".join(query_tokens)]),
            k=min(top_k, len(self._chunk_metas)),
        )
        out = []
        for idx, score in zip(results[0], scores[0]):
            if 0 <= idx < len(self._chunk_metas) and score > 0:
                out.append((self._chunk_metas[idx], float(score)))
        return out

    def save(self) -> None:
        if self.index_dir is None or self._retriever is None:
            return
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self._retriever.save(str(self.index_dir / "bm25"))
        (self.index_dir / "bm25_chunks.json").write_text(
            json.dumps(self._chunk_metas, ensure_ascii=False), encoding="utf-8"
        )
        log.info("Saved BM25 index: %d documents", len(self._chunk_metas))

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
            return True
        except Exception as e:
            log.warning("Failed to load BM25 index: %s", e)
            return False

    @property
    def total_documents(self) -> int:
        return len(self._chunk_metas)
