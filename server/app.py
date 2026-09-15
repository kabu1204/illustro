"""FastAPI local server: search / similar / thumbnails / stats / monitor / mobile sync.

Start: python -m illustro.cli serve   then open http://127.0.0.1:8000
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel

from illustro.analyze import duplicate_clusters, overview
from illustro.cluster import get_clusters
from illustro.config import Config
from illustro.db import DB
from illustro.index import VectorStore
from illustro.search import Searcher, autocomplete

STATIC_DIR = Path(__file__).parent / "static"

# Maximum number of per-query latency samples kept in memory
MAX_QUERY_SAMPLES = 100


class SyncCheckBody(BaseModel):
    """POST /api/sync/check request: batch of sha256 hex digests."""
    hashes: list[str] = []


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(int(len(sorted_vals) * p), len(sorted_vals) - 1)
    return sorted_vals[idx]


def _latency_stats(samples: list[dict], endpoint: str) -> dict:
    vals = sorted(s["latency_ms"] for s in samples if s["endpoint"] == endpoint)
    if not vals:
        return {"count": 0, "mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0}
    return {
        "count": len(vals),
        "mean_ms": round(sum(vals) / len(vals), 2),
        "p50_ms": round(_percentile(vals, 0.5), 2),
        "p95_ms": round(_percentile(vals, 0.95), 2),
        "min_ms": round(vals[0], 2),
        "max_ms": round(vals[-1], 2),
    }


def create_app(cfg: Config, worker=None) -> FastAPI:
    from contextlib import asynccontextmanager

    # In-memory query latency ring buffer (shared across handlers via closure)
    query_latencies: deque[dict] = deque(maxlen=MAX_QUERY_SAMPLES)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # One-time startup benchmark: verify numpy cosine search latency at 20k scale
        bench_result = None
        try:
            from illustro.index import benchmark_cosine
            bench_result = benchmark_cosine()
            print(
                f"[startup] numpy cosine benchmark: {bench_result['n']}x{bench_result['dim']} top-{bench_result['k']} "
                f"mean={bench_result['mean_ms']:.2f}ms p50={bench_result['p50_ms']:.2f}ms p95={bench_result['p95_ms']:.2f}ms "
                f"({bench_result['iterations']} iterations)",
                flush=True,
            )
        except Exception as e:
            print(f"[startup] numpy cosine benchmark failed: {e}", flush=True)
        _app.state.benchmark = bench_result

        # uvicorn triggers shutdown on SIGTERM/SIGINT; gracefully stop the worker (interrupt current batch + sleep).
        # Newer FastAPI removed add_event_handler; use lifespan instead.
        yield
        if worker is not None:
            worker.stop()

    app = FastAPI(title="illustro", version="0.1.0", lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=100_000)  # /api/clusters payload is ~1MB at 20k images
    db = DB(cfg.db_path)
    store = VectorStore(cfg)
    searcher = Searcher(cfg, db, store)

    def thumb_path(image_id: int) -> Path:
        return cfg.thumb_dir / f"{image_id}.jpg"

    @app.get("/api/search")
    def api_search(
        q: str = "",
        include: str = "",
        exclude: str = "",
        rating: str = "",
        page: int = 1,
        sort: str = "new",
        seed: int = 0,
    ):
        t0 = time.perf_counter()
        inc = [x for x in include.split(",") if x]
        exc = [x for x in exclude.split(",") if x]
        rat = [x for x in rating.split(",") if x]
        if sort not in ("new", "old", "random"):
            sort = "new"
        res = searcher.search(q, include=inc, exclude=exc, rating=rat or None, page=page,
                              sort=sort, seed=max(0, min(seed, 2**31 - 1)))
        query_latencies.append({"endpoint": "search", "latency_ms": (time.perf_counter() - t0) * 1000, "ts": time.time()})
        return JSONResponse(
            {
                "total": res.total,
                "matched_tags": res.matched_tags,
                "residual": res.residual,
                "mode": res.mode,
                "page": page,
                "page_size": cfg.server.page_size,
                "images": res.images,
            }
        )

    @app.get("/api/similar")
    def api_similar(id: int, k: int = 30):
        t0 = time.perf_counter()
        result = searcher.similar(id, k=k)
        query_latencies.append({"endpoint": "similar", "latency_ms": (time.perf_counter() - t0) * 1000, "ts": time.time()})
        return JSONResponse({"images": result})

    @app.get("/api/autocomplete")
    def api_autocomplete(q: str = Query("")):
        if not q:
            return JSONResponse({"items": []})
        return JSONResponse({"items": autocomplete(db, cfg, q)})

    @app.get("/api/stats")
    def api_stats():
        return JSONResponse(overview(db))

    @app.get("/api/duplicates")
    def api_duplicates():
        return JSONResponse({"clusters": duplicate_clusters(db)})

    # ---- Cluster analysis ("style map") ----
    # Serialized: a cold compute takes seconds at 20k; concurrent identical
    # requests should queue behind one computation, then hit the disk cache.
    cluster_lock = threading.Lock()

    @app.get("/api/clusters")
    def api_clusters(k: int = 30, refresh: int = 0):
        k = max(5, min(k, 100))
        with cluster_lock:
            return JSONResponse(get_clusters(store, db, cfg.data_path, k, refresh=bool(refresh)))

    # ---- Background worker controls (available in single-container mode) ----
    def worker_payload() -> dict:
        total = db.count()
        tagged = db.count("tagged=1")
        base = {
            "enabled": worker is not None,
            "total": total,
            "tagged": tagged,
            "remaining": total - tagged,
        }
        if worker is not None:
            base.update(worker.status())
        return base

    @app.get("/api/worker")
    def api_worker():
        return JSONResponse(worker_payload())

    @app.post("/api/worker/{action}")
    def api_worker_action(action: str):
        if worker is None:
            return JSONResponse({"error": "Worker not enabled (running in serve-only mode)"}, status_code=400)
        if action == "pause":
            worker.pause()
        elif action == "resume":
            worker.resume()
        elif action == "run":
            worker.resume()
            worker.run_now()
        else:
            return JSONResponse({"error": f"Unknown action: {action}"}, status_code=400)
        return JSONResponse(worker_payload())

    # ---- Monitoring dashboard ----
    @app.get("/api/monitor")
    def api_monitor():
        samples = list(query_latencies)
        return JSONResponse({
            "worker": worker.monitor_status() if worker is not None else None,
            "db_total": db.count(),
            "db_tagged": db.count("tagged=1"),
            "benchmark": getattr(app.state, "benchmark", None),
            "query_latency": {
                "recent": samples,
                "stats": {
                    "search": _latency_stats(samples, "search"),
                    "similar": _latency_stats(samples, "similar"),
                },
            },
        })

    @app.get("/api/image/{image_id}")
    def api_image(image_id: int):
        row = db.get_image(image_id)
        if not row:
            return Response(status_code=404)
        return FileResponse(row["path"])

    @app.get("/api/image_info/{image_id}")
    def api_image_info(image_id: int):
        """Metadata + tags for one image (viewer deep links, duplicate inspection)."""
        info = searcher.image_info(image_id)
        if info is None:
            return Response(status_code=404)
        return JSONResponse(info)


    # ---- Mobile sync: one-way upload (phone -> server) ----
    # Files land in the inbox dir (auto-watched by the scanner); the worker tags
    # them on its next round. The UI pokes /api/worker/run to make it immediate.
    _UNSAFE_FILENAME = re.compile(r'[/\\:*?"<>|\x00-\x1f]')  # keep unicode names, strip path/Windows-unsafe chars

    def _check_sync_token(x_api_token: str | None = Header(default=None)) -> None:
        if cfg.sync.token and x_api_token != cfg.sync.token:
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Token")

    @app.get("/api/sync/info")
    def api_sync_info():
        # No token required: lets the UI discover whether sync is on / needs a token.
        return JSONResponse({
            "enabled": cfg.sync.enabled,
            "auth_required": bool(cfg.sync.token),
            "max_upload_mb": cfg.sync.max_upload_mb,
            "extensions": cfg.extensions,
        })

    @app.post("/api/sync/check")
    def api_sync_check(body: SyncCheckBody, _: None = Depends(_check_sync_token)):
        """Which of these sha256 digests does the server already have? (batch dedup check)"""
        if not cfg.sync.enabled:
            raise HTTPException(status_code=403, detail="Sync disabled")
        hashes = [h for h in body.hashes if isinstance(h, str) and len(h) == 64][:5000]
        known = db.known_hashes(hashes) if hashes else set()
        return JSONResponse({"known": sorted(known)})

    @app.post("/api/sync/upload")
    async def api_sync_upload(
        file: UploadFile = File(...),
        sha256: str = Form(default=""),
        mtime_ms: str = Form(default=""),
        _: None = Depends(_check_sync_token),
    ):
        """One file per request. Streamed to disk (never buffered in RAM), hash-verified,
        atomically renamed into the inbox. Exact duplicates are dropped (status=duplicate).
        Optional mtime_ms (epoch milliseconds from the client) preserves the source file's
        modification time so library date views reflect when images were saved, not uploaded."""
        if not cfg.sync.enabled:
            raise HTTPException(status_code=403, detail="Sync disabled")
        orig = Path(file.filename or "upload")
        ext = orig.suffix.lower()
        if ext not in cfg.extensions:
            raise HTTPException(status_code=415, detail=f"Unsupported extension: {ext or '(none)'}")
        limit_mb = cfg.sync.max_upload_mb
        limit = limit_mb * 1024 * 1024

        inbox = cfg.inbox_path
        tmp = inbox / f".{uuid.uuid4().hex}.part"
        h = hashlib.sha256()
        size = 0
        try:
            with open(tmp, "wb") as out:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(status_code=413, detail=f"File larger than max_upload_mb={limit_mb}")
                    h.update(chunk)
                    out.write(chunk)
            digest = h.hexdigest()
            if sha256 and sha256.lower() != digest:
                raise HTTPException(status_code=422, detail="sha256 mismatch: upload corrupted in transit")
            if digest in db.known_hashes([digest]):
                tmp.unlink(missing_ok=True)
                return JSONResponse({"status": "duplicate", "sha256": digest})
            # Keep the original filename (sanitized). The stem is byte-capped to stay
            # under NAME_MAX (255 bytes) after suffixing; collisions are resolved via
            # an atomic O_EXCL reservation so a name is never overwritten, and the
            # rename then replaces the empty placeholder atomically.
            name = _UNSAFE_FILENAME.sub("_", orig.stem).strip(". ") or "upload"
            while len(name.encode("utf-8")) > 180:
                name = name[:-1]
            dest = inbox / f"{name}{ext}"
            n = 1
            while True:
                try:
                    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    break
                except FileExistsError:
                    n += 1
                    dest = inbox / f"{name}_{n}{ext}"
            os.close(fd)
            try:
                tmp.replace(dest)
            except Exception:
                dest.unlink(missing_ok=True)
                raise
            # Preserve the client's original mtime (sanity-clamped); falls back to upload time.
            try:
                mtime = float(mtime_ms) / 1000.0
                if 0 < mtime <= time.time() + 7 * 86400:
                    os.utime(dest, (mtime, mtime))
            except (TypeError, ValueError, OverflowError):
                pass
            # Record after the rename lands: a crash here at worst allows a benign
            # duplicate on retry; recording first could reject content we never stored.
            db.record_sync_hash(digest)
            return JSONResponse({"status": "stored", "sha256": digest, "size": size, "name": dest.name})
        except HTTPException:
            tmp.unlink(missing_ok=True)
            raise
        except Exception:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail="Upload failed")

    @app.get("/api/thumb/{image_id}")
    def api_thumb(image_id: int):
        tp = thumb_path(image_id)
        if tp.exists():
            return FileResponse(tp)
        row = db.get_image(image_id)
        if not row:
            return Response(status_code=404)
        try:
            with Image.open(row["path"]) as im:
                if getattr(im, "is_animated", False):
                    im.seek(0)
                im = im.convert("RGB")
                im.thumbnail((cfg.server.thumbnail_size, cfg.server.thumbnail_size), Image.BILINEAR)
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=85)
                tp.write_bytes(buf.getvalue())
                return Response(buf.getvalue(), media_type="image/jpeg")
        except Exception:
            return Response(status_code=415)

    # Static frontend (mounted last to avoid catching /api routes). worker=None means serve-only mode.
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app
