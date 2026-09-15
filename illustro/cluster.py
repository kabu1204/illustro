"""Cluster analysis: spherical KMeans on image embeddings + PCA 2D projection.

Pure numpy, no sklearn/umap: at the ~20k scale Lloyd's algorithm on PCA-reduced
vectors takes seconds, and the result is cached on disk keyed by the embedding
matrix's mtime, so repeat views are instant.

Pipeline: PCA to REDUCED_DIM (covariance trick, cheap when N >> D) -> spherical
KMeans (cosine assignment, kmeans++ init) -> per-cluster characterization from
the tag DB (top overlapping general tags = the cluster's "style") -> 2D coords
from the first two PCA components of the full matrix.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

from .db import DB
from .index import VectorStore

REDUCED_DIM = 128      # PCA pre-reduction before clustering (speed + noise)
MAX_ITERS = 30
TOP_TAGS = 8           # tags shown per cluster
REP_COUNT = 6          # representative images per cluster
GLOBAL_SHARE_CUTOFF = 0.5  # drop near-ubiquitous tags (e.g. "1girl") from labels


def _pca_basis(X: np.ndarray, n_components: int) -> tuple[np.ndarray, np.ndarray]:
    """Returns (mean, basis) where basis is (D, n_components). Covariance-trick PCA:
    eigendecompose X^T X (D x D) instead of X X^T (N x N) since N >> D."""
    mean = X.mean(axis=0)
    Xc = X - mean
    C = Xc.T @ Xc
    eigvals, eigvecs = np.linalg.eigh(C.astype(np.float64))
    idx = np.argsort(eigvals)[::-1][:n_components]
    return mean.astype(np.float32), eigvecs[:, idx].astype(np.float32)


def _spherical_kmeans(X: np.ndarray, k: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """KMeans with cosine assignment on L2-normalized rows. Returns (assign (N,), centroids (k, d))."""
    rng = np.random.default_rng(seed)
    n = X.shape[0]
    # kmeans++ init with cosine distance
    centroids = [X[rng.integers(n)]]
    closest = X @ centroids[0]  # best (highest) similarity to any chosen centroid
    for _ in range(k - 1):
        dist2 = np.maximum(1.0 - closest, 1e-12) ** 2
        total = float(dist2.sum())
        nxt = int(rng.choice(n, p=dist2 / total)) if total > 0 else int(rng.integers(n))
        centroids.append(X[nxt])
        closest = np.maximum(closest, X @ centroids[-1])
    C = np.stack(centroids)

    assign = np.full(n, -1, dtype=np.int32)
    for _ in range(MAX_ITERS):
        new_assign = (X @ C.T).argmax(axis=1).astype(np.int32)
        if np.array_equal(new_assign, assign):
            break
        assign = new_assign
        for j in range(k):
            members = X[assign == j]
            if len(members) == 0:
                # Reinit empty cluster at the point worst served by its centroid
                worst = int((X @ C.T).max(axis=1).argmin())
                C[j] = X[worst]
            else:
                C[j] = members.sum(axis=0)
        C /= np.linalg.norm(C, axis=1, keepdims=True).clip(min=1e-12)
    return assign, C


def _hex_mean(colors: list[str]) -> str | None:
    vals = []
    for c in colors:
        if c and len(c) == 7 and c.startswith("#"):
            try:
                vals.append(tuple(int(c[i : i + 2], 16) for i in (1, 3, 5)))
            except ValueError:
                pass
    if not vals:
        return None
    r, g, b = (round(sum(v[i] for v in vals) / len(vals)) for i in range(3))
    return f"#{r:02x}{g:02x}{b:02x}"


def compute_clusters(store: VectorStore, db: DB, k: int, seed: int = 42) -> dict:
    """Full cluster computation. Expensive (seconds at 20k) — call get_clusters() instead,
    which caches the result on disk."""
    store._ensure_norm()
    X = store._norm  # (N, D) L2-normalized rows; row i == vec_id i
    n = store.count
    k = max(2, min(k, n))

    # Map vec_id -> image row (id, rating, avg_color). Skip rows whose vec_id is
    # out of range (can only happen after external DB tampering; no delete API exists).
    image_ids = np.zeros(n, dtype=np.int64)
    rating: list[str | None] = [None] * n
    colors: list[str | None] = [None] * n
    for row in db.conn.execute("SELECT id, vec_id, rating, avg_color FROM images WHERE embedded=1"):
        v = row["vec_id"]
        if v is not None and 0 <= v < n:
            image_ids[v] = row["id"]
            rating[v] = row["rating"]
            colors[v] = row["avg_color"]

    # PCA basis on the full matrix; reduced view for clustering, 2D for display
    n_comp = min(REDUCED_DIM, store.dim)
    mean, basis = _pca_basis(X, n_comp)
    proj = (X - mean) @ basis          # (N, n_comp)
    Xr = proj / np.linalg.norm(proj, axis=1, keepdims=True).clip(min=1e-12)
    coords = proj[:, :2]

    assign, centroids = _spherical_kmeans(Xr, k, seed)

    # Tag aggregation: one bulk query over all general tags
    tag_counts = [Counter() for _ in range(k)]
    global_counts: Counter = Counter()
    tag_zh: dict[str, str | None] = {}
    id_to_row = {int(iid): i for i, iid in enumerate(image_ids) if iid}
    for row in db.conn.execute(
        "SELECT it.image_id, t.name, t.name_zh FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE t.category=0"
    ):
        tag_zh[row["name"]] = row["name_zh"]
        r = id_to_row.get(row["image_id"])
        if r is None:
            continue
        tag_counts[assign[r]][row["name"]] += 1
        global_counts[row["name"]] += 1

    clusters = []
    for j in range(k):
        mask = assign == j
        size = int(mask.sum())
        if size == 0:
            continue
        sims = Xr[mask] @ centroids[j]
        member_rows = np.flatnonzero(mask)
        reps = [int(image_ids[member_rows[i]]) for i in np.argsort(-sims)[:REP_COUNT]]
        top = [
            {"name": name, "zh": tag_zh.get(name), "share": round(cnt / size, 3)}
            for name, cnt in tag_counts[j].most_common()
            if global_counts[name] / n <= GLOBAL_SHARE_CUTOFF
        ][:TOP_TAGS]
        clusters.append(
            {
                "id": j,
                "count": size,
                "avg_color": _hex_mean([colors[i] for i in member_rows[:500]]),
                "top_tags": top,
                "rating_dist": dict(Counter(rating[i] or "?" for i in member_rows)),
                "reps": reps,
            }
        )
    clusters.sort(key=lambda c: -c["count"])

    return {
        "k": k,
        "n": n,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "emb_mtime": store.emb_path.stat().st_mtime,
        "points": [
            [int(image_ids[i]), round(float(coords[i, 0]), 4), round(float(coords[i, 1]), 4), int(assign[i])]
            for i in range(n)
            if image_ids[i]
        ],
        "clusters": clusters,
    }


def get_clusters(store: VectorStore, db: DB, data_path: Path, k: int, refresh: bool = False) -> dict:
    """Cluster payload, disk-cached per k in <data_dir>/clusters_k{k}.json. The cache is
    valid while the embedding matrix is unchanged (mtime + row count match). The worker's
    incremental tagging bumps the mtime, so the next view recomputes once and re-caches."""
    store.maybe_reload()
    if store.count == 0:
        return {"k": k, "n": 0, "points": [], "clusters": []}
    mtime = store.emb_path.stat().st_mtime
    cache_path = data_path / f"clusters_k{k}.json"
    if not refresh and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("emb_mtime") == mtime and cached.get("n") == store.count:
                return cached
        except (ValueError, OSError):
            pass
    t0 = time.perf_counter()
    result = compute_clusters(store, db, k)
    tmp = cache_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache_path)
    print(f"[clusters] k={k} n={result['n']} computed in {time.perf_counter() - t0:.1f}s", flush=True)
    return result
