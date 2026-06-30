"""Image vector storage + retrieval (numpy brute-force cosine).

Why not hnswlib: at the 20k scale, a plain numpy matrix-multiply cosine search takes only
a few milliseconds per query, which is fast enough. hnswlib is a C++ extension compiled with
-march=native by default; building it on CI (with AVX-512) and running on an N100 (no AVX-512)
crashes with SIGILL. Pure numpy has no native compilation, is cross-CPU safe, and is fast enough.

Convention: each image's vec_id == its row number in embeddings.npy, matching SQLite's images.vec_id 1:1.
"""
from __future__ import annotations

import numpy as np

from .config import Config


class VectorStore:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.emb_path = cfg.emb_path
        self.mat: np.ndarray | None = None      # raw vectors (N, D) float32
        self._norm: np.ndarray | None = None     # normalized vectors (cached for cosine)
        self._loaded_mtime = None
        self._load_matrix()

    # ---------- Matrix ----------
    def _load_matrix(self):
        if self.emb_path.exists():
            self.mat = np.load(self.emb_path).astype(np.float32)
            self._loaded_mtime = self.emb_path.stat().st_mtime
        else:
            self.mat = None
            self._loaded_mtime = None
        self._norm = None

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
                raise ValueError(f"Vector dimension mismatch: existing {self.mat.shape[1]}, new {vecs.shape[1]}")
            self.mat = np.concatenate([self.mat, vecs], axis=0)
        self._norm = None  # invalidate; recomputed on next query
        return list(range(start, self.count))

    def save_matrix(self):
        if self.mat is not None:
            np.save(self.emb_path, self.mat)
            self._loaded_mtime = self.emb_path.stat().st_mtime

    # ---------- Retrieval ----------
    def _ensure_norm(self):
        if self.mat is None:
            self._norm = None
            return
        if self._norm is None or self._norm.shape[0] != self.mat.shape[0]:
            norms = np.linalg.norm(self.mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._norm = (self.mat / norms).astype(np.float32)

    def build_hnsw(self):
        """Kept for API compatibility (pipeline's step_index calls it). Under the numpy scheme, only warms up the normalized matrix."""
        if self.count == 0:
            print("[index] No vectors, skipping.")
            return
        self._ensure_norm()
        print(f"[index] Vector store ready: {self.count} x {self.dim} (numpy cosine retrieval)")

    def query_vec(self, vec: np.ndarray, k: int = 30) -> list[tuple[int, float]]:
        """Returns [(vec_id, similarity)] where similarity = cosine, sorted descending."""
        if self.mat is None or self.count == 0:
            return []
        self._ensure_norm()
        v = np.asarray(vec, dtype=np.float32).ravel()
        n = float(np.linalg.norm(v))
        if n == 0.0:
            return []
        v = v / n
        sims = self._norm @ v                      # (N,) cosine similarity
        k = min(k, self.count)
        top = np.argpartition(-sims, k - 1)[:k]     # unordered top-k
        top = top[np.argsort(-sims[top])]           # then sort
        return [(int(i), float(sims[i])) for i in top]

    def query_by_vec_id(self, vec_id: int, k: int = 30) -> list[tuple[int, float]]:
        if self.mat is None or vec_id >= self.count:
            return []
        return self.query_vec(self.mat[vec_id], k=k)

    def maybe_reload(self) -> bool:
        """Reload matrix if the worker has incrementally written new vectors; used by the long-running web service. Returns True if a reload occurred."""
        try:
            m = self.emb_path.stat().st_mtime
        except FileNotFoundError:
            return False
        if self._loaded_mtime == m:
            return False
        self._load_matrix()
        return True


def benchmark_cosine(n: int = 20000, dim: int = 1024, iterations: int = 50, k: int = 30) -> dict:
    """Benchmark numpy brute-force cosine similarity search latency with synthetic data.

    Generates a random (n, dim) float32 matrix and measures the time for a single
    top-k cosine query. Returns a dict with mean/p50/p95 latency in milliseconds.
    """
    import time

    rng = np.random.default_rng(42)
    mat = rng.standard_normal((n, dim)).astype(np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    norm_mat = (mat / norms).astype(np.float32)

    # Warmup (first call allocates/cache-loads)
    q = rng.standard_normal(dim).astype(np.float32)
    q = q / np.linalg.norm(q)
    _ = norm_mat @ q

    times = []
    for _ in range(iterations):
        q = rng.standard_normal(dim).astype(np.float32)
        q = q / np.linalg.norm(q)
        t0 = time.perf_counter()
        sims = norm_mat @ q
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        times.append(time.perf_counter() - t0)

    times.sort()
    return {
        "n": n,
        "dim": dim,
        "iterations": iterations,
        "k": k,
        "mean_ms": sum(times) / len(times) * 1000,
        "p50_ms": times[len(times) // 2] * 1000,
        "p95_ms": times[min(int(len(times) * 0.95), len(times) - 1)] * 1000,
    }
