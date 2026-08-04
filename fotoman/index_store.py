from __future__ import annotations

import json
from pathlib import Path

import faiss
import numpy as np

from fotoman.config import FAISS_PATH, IDS_PATH, ensure_dirs


class IndexStore:
    def __init__(self, dim: int = 768):
        ensure_dirs()
        self.dim = dim
        self.index = faiss.IndexFlatIP(dim)
        self.ids: list[int] = []
        if FAISS_PATH.exists() and IDS_PATH.exists():
            self.index = faiss.read_index(str(FAISS_PATH))
            self.ids = json.loads(IDS_PATH.read_text())
            self.dim = self.index.d

    def save(self) -> None:
        ensure_dirs()
        faiss.write_index(self.index, str(FAISS_PATH))
        IDS_PATH.write_text(json.dumps(self.ids))

    def clear(self) -> None:
        self.index = faiss.IndexFlatIP(self.dim)
        self.ids = []
        self.save()

    def add(self, photo_ids: list[int], vectors: np.ndarray) -> None:
        if len(photo_ids) == 0:
            return
        assert vectors.shape[0] == len(photo_ids)
        if vectors.dtype != np.float32:
            vectors = vectors.astype(np.float32)
        faiss.normalize_L2(vectors)
        self.index.add(vectors)
        self.ids.extend(int(i) for i in photo_ids)

    def remove_ids(self, remove: set[int]) -> None:
        if not remove or not self.ids:
            return
        keep_pos = [i for i, pid in enumerate(self.ids) if pid not in remove]
        if len(keep_pos) == len(self.ids):
            return
        if not keep_pos:
            self.clear()
            return
        vectors = np.vstack([self.index.reconstruct(i) for i in keep_pos]).astype(np.float32)
        self.ids = [self.ids[i] for i in keep_pos]
        self.index = faiss.IndexFlatIP(self.dim)
        self.index.add(vectors)

    def search(self, vector: np.ndarray, top_k: int = 24) -> list[tuple[int, float]]:
        if self.index.ntotal == 0:
            return []
        q = vector.astype(np.float32).reshape(1, -1)
        faiss.normalize_L2(q)
        k = min(top_k, self.index.ntotal)
        scores, idxs = self.index.search(q, k)
        out: list[tuple[int, float]] = []
        for score, idx in zip(scores[0], idxs[0]):
            if idx < 0:
                continue
            out.append((self.ids[idx], float(score)))
        return out

    def all_vectors(self) -> tuple[list[int], np.ndarray]:
        if self.index.ntotal == 0:
            return [], np.zeros((0, self.dim), dtype=np.float32)
        vecs = np.vstack([self.index.reconstruct(i) for i in range(self.index.ntotal)]).astype(np.float32)
        return list(self.ids), vecs


_store: IndexStore | None = None


def get_store() -> IndexStore:
    global _store
    if _store is None:
        _store = IndexStore()
    return _store


def reload_store() -> IndexStore:
    global _store
    _store = IndexStore()
    return _store