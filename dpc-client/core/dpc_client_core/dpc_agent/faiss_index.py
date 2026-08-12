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

from .index_meta import read_meta, write_meta

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
        seen_files: set = set()
        for score, idx in zip(scores[0], indices[0]):
            if idx >= 0 and idx < len(self._chunks):
                fname = self._chunks[idx].get("source_file", "")
                if fname not in seen_files:
                    seen_files.add(fname)
                    results.append((self._chunks[idx], float(score)))
        return results

    def remove_by_source(self, source_file: str) -> int:
        """Remove all chunks from a specific source file and rebuild index."""
        if self._index is None or not self._chunks:
            return 0
        keep = [(i, c) for i, c in enumerate(self._chunks) if c.get("source_file") != source_file]
        removed = len(self._chunks) - len(keep)
        if removed == 0:
            return 0
        import faiss
        new_index = faiss.IndexFlatIP(self._header.dimensions)
        if keep:
            vectors = np.vstack([self._index.reconstruct(i).reshape(1, -1) for i, _ in keep])
            new_index.add(vectors)
        self._index = new_index
        self._chunks = [c for _, c in keep]
        self._header.chunk_count = self._index.ntotal
        log.info("Removed %d chunks for %s, %d remaining", removed, source_file, self._header.chunk_count)
        return removed

    def remove_by_sources(self, source_files) -> int:
        """One pass over the chunks, one index rebuilt, however many sources go.

        Per source this reconstructed every surviving vector one at a time and built a
        fresh IndexFlatIP from them — O(sources x index size), which for a few hundred
        deletions over a couple of thousand chunks is hundreds of thousands of single
        reconstructions to delete a few hundred rows.
        """
        drop = {s for s in source_files if s}
        if not drop or self._index is None or not self._chunks:
            return 0
        keep = [(i, c) for i, c in enumerate(self._chunks) if c.get("source_file") not in drop]
        removed = len(self._chunks) - len(keep)
        if removed == 0:
            return 0
        import faiss
        new_index = faiss.IndexFlatIP(self._header.dimensions)
        if keep:
            new_index.add(np.vstack([self._index.reconstruct(i).reshape(1, -1) for i, _ in keep]))
        self._index = new_index
        self._chunks = [c for _, c in keep]
        self._header.chunk_count = self._index.ntotal
        log.info("Removed %d chunks for %d sources, %d remaining",
                 removed, len(drop), self._header.chunk_count)
        return removed

    def save(self) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)
        if self._index is not None:
            import faiss
            faiss.write_index(self._index, str(self._index_path))
        # This file is shared with agent_manager, which keeps file_hashes and
        # header.key_format in it. Writing the document whole dropped both.
        doc = read_meta(self._meta_path)
        stored_header = doc.get("header") or {}
        foreign = {k: v for k, v in stored_header.items()
                   if k not in IndexHeader.__dataclass_fields__}
        doc["header"] = {**foreign, **asdict(self._header)}
        doc["chunks"] = self._chunks
        write_meta(self._meta_path, doc)
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
            # A row number is only meaningful against the chunk list: `search` maps
            # `idx` into it, so a list shorter than the index makes later rows
            # unreachable and earlier rows answer for other documents. That state has
            # happened — 328 vectors against 23 chunks — and it looked like a healthy
            # index from every angle except this comparison. Refusing here is the same
            # answer as a missing file, which the caller already handles by rebuilding.
            stored_rows = self._index.ntotal if self._index is not None else 0
            if stored_rows != len(self._chunks):
                log.warning(
                    "Index and chunk list disagree (%d vectors, %d chunks) — refusing to load",
                    stored_rows, len(self._chunks),
                )
                self.clear()
                return False
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
