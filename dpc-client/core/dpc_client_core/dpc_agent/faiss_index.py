"""FAISS vector index manager (ADR-010, MEM-3.4).

IndexFlatIP for <100K chunks, upgradeable to HNSW later.
Persists to disk with metadata header for model swap detection.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class IndexHeader:
    model_name: str = ""
    dimensions: int = 0
    chunk_count: int = 0
    created_at: str = ""
    max_chars: int = 1500
    overlap_chars: int = 200


class FaissIndex:
    """FAISS vector index with disk persistence."""

    def __init__(self, index_dir: Optional[pathlib.Path], model_name: str = "", dimensions: int = 384):
        self.index_dir = index_dir
        self._index = None
        self._chunks: List[dict] = []
        self._header = IndexHeader(model_name=model_name, dimensions=dimensions)
        self._index_path = index_dir / "vectors.faiss" if index_dir else None
        self._meta_path = index_dir / "index_meta.json" if index_dir else None

    def _ensure_index(self):
        if self._index is None:
            import faiss
            self._index = faiss.IndexFlatIP(self._header.dimensions)

    def add(self, vectors: np.ndarray, chunk_metas: List[dict]) -> None:
        self._ensure_index()
        import faiss
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        faiss.normalize_L2(vectors)
        self._index.add(vectors)
        self._chunks.extend(chunk_metas)
        self._header.chunk_count = self._index.ntotal

    def search(self, query_vector: np.ndarray, top_k: int = 5) -> List[Tuple[dict, float]]:
        if self._index is None or self._index.ntotal == 0:
            return []
        if query_vector.ndim == 1:
            query_vector = query_vector.reshape(1, -1)
        import faiss as _faiss
        _faiss.normalize_L2(query_vector)
        scores, indices = self._index.search(query_vector, min(top_k, self._index.ntotal))
        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx >= 0 and idx < len(self._chunks):
                results.append((self._chunks[idx], float(score)))
        return results

    def save(self) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        if self._index is not None:
            import faiss
            faiss.write_index(self._index, str(self._index_path))
        self._meta_path.write_text(json.dumps({
            "header": asdict(self._header),
            "chunks": self._chunks,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Saved FAISS index: %d vectors", self._header.chunk_count)

    def load(self) -> bool:
        if not self._meta_path.exists():
            return False
        try:
            data = json.loads(self._meta_path.read_text(encoding="utf-8"))
            h = data.get("header", {})
            self._header = IndexHeader(**{k: v for k, v in h.items() if k in IndexHeader.__dataclass_fields__})
            self._chunks = data.get("chunks", [])
            if self._index_path.exists():
                import faiss
                self._index = faiss.read_index(str(self._index_path))
            return True
        except Exception as e:
            log.warning("Failed to load FAISS index: %s", e)
            return False

    def needs_rebuild(self, model_name: str) -> bool:
        return self._header.model_name != model_name

    def clear(self) -> None:
        self._index = None
        self._chunks = []
        self._header.chunk_count = 0

    @property
    def total_vectors(self) -> int:
        return self._index.ntotal if self._index else 0
