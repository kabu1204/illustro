"""FastAPI local server: search / similar / thumbnails / stats / monitor.

Start: python -m illustro.cli serve   then open http://127.0.0.1:8000
"""
from __future__ import annotations

import io
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from illustro.analyze import duplicate_clusters, overview
from illustro.config import Config
from illustro.db import DB
from illustro.index import VectorStore
from illustro.search import Searcher, autocomplete

STATIC_DIR = Path(__file__).parent / "static"

# Maximum number of per-query latency samples kept in memory
MAX_QUERY_SAMPLES = 100


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
    ):
        t0 = time.perf_counter()
        inc = [x for x in include.split(",") if x]
        exc = [x for x in exclude.split(",") if x]
        rat = [x for x in rating.split(",") if x]
        res = searcher.search(q, include=inc, exclude=exc, rating=rat or None, page=page)
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
