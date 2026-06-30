"""Image vector storage + hnswlib approximate nearest neighbor index.

Convention: each image's vec_id == its row number in embeddings.npy. New vectors are appended
sequentially, with vec_id incrementing, matching SQLite's images.vec_id 1:1. At the 20k scale,
building HNSW from the full matrix takes only seconds, so we use a simple strategy of
"append to npy + rebuild HNSW on demand".
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .config import Config


class VectorStore:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.emb_path = cfg.emb_path
        self.hnsw_path = cfg.hnsw_path
        self.mat: np.ndarray | None = None
        self._index = None
        self._load_matrix()

    # ---------- Matrix ----------
    def _load_matrix(self):
        if self.emb_path.exists():
            self.mat = np.load(self.emb_path)
            self._loaded_mtime = self.emb_path.stat().st_mtime
        else:
            self.mat = None
            self._loaded_mtime = None

    def maybe_reload(self) -> bool:
        """Reload matrix and index if the on-disk vector file has been updated (by worker incremental writes).
        Called by the long-running web service so new images are visible without restart.
        Returns True if a reload occurred."""
        try:
            m = self.emb_path.stat().st_mtime
        except FileNotFoundError:
            return False
        if getattr(self, "_loaded_mtime", None) == m:
            return False
        self._load_matrix()
        self._index = None  # Force HNSW reload on next query
        return True

    @property
    def count(self) -> int:
        return 0 if self.mat is None else self.mat.shape[0]

    @property
    def dim(self) -> int:
        return 0 if self.mat is None else self.mat.shape[1]

    def add_many(self, vecs: np.ndarray) -> list[int]:
        """Append vectors and return the assigned vec_id list."""
        vecs = np.asarray(vecs, dtype=np.float32)
        if vecs.ndim == 1:
            vecs = vecs[None, :]
        start = self.count
        if self.mat is None:
            self.mat = vecs.copy()
        else:
            if vecs.shape[1] != self.mat.shape[1]:
                raise ValueError(
                    f"Vector dimension mismatch: existing {self.mat.shape[1]}, new {vecs.shape[1]}"
                )
            self.mat = np.concatenate([self.mat, vecs], axis=0)
        return list(range(start, self.count))

    def save_matrix(self):
        if self.mat is not None:
            np.save(self.emb_path, self.mat)

    # ---------- HNSW ----------
    def build_hnsw(self):
        import hnswlib

        if self.count == 0:
            print("[warn] No vectors, skipping index build.")
            return
        idx = hnswlib.Index(space=self.cfg.index.space, dim=self.dim)
        idx.init_index(
            max_elements=self.count,
            ef_construction=self.cfg.index.ef_construction,
            M=self.cfg.index.M,
        )
        idx.add_items(self.mat, np.arange(self.count))
        idx.set_ef(self.cfg.index.ef_search)
        idx.save_index(str(self.hnsw_path))
        self._index = idx
        print(f"[*] HNSW index built: {self.count} items x {self.dim} dims -> {self.hnsw_path}")

    def _ensure_index(self):
        if self._index is not None:
            return
        import hnswlib

        if not self.hnsw_path.exists():
            self.build_hnsw()
            return
        idx = hnswlib.Index(space=self.cfg.index.space, dim=self.dim)
        idx.load_index(str(self.hnsw_path), max_elements=self.count)
        idx.set_ef(self.cfg.index.ef_search)
        self._index = idx

    def query_vec(self, vec: np.ndarray, k: int = 30) -> list[tuple[int, float]]:
        """Returns [(vec_id, similarity)] where similarity = 1 - cosine distance, sorted descending."""
        self._ensure_index()
        if self._index is None:
            return []
        vec = np.asarray(vec, dtype=np.float32)[None, :]
        k = min(k, self.count)
        labels, dists = self._index.knn_query(vec, k=k)
        return [(int(l), 1.0 - float(d)) for l, d in zip(labels[0], dists[0])]

    def query_by_vec_id(self, vec_id: int, k: int = 30) -> list[tuple[int, float]]:
        if self.mat is None or vec_id >= self.count:
            return []
        return self.query_vec(self.mat[vec_id], k=k)
