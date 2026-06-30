"""FastAPI local server: search / similar / thumbnails / stats.

Start: python -m illustro.cli serve   then open http://127.0.0.1:8000
"""
from __future__ import annotations

import io
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


def create_app(cfg: Config, worker=None) -> FastAPI:
    app = FastAPI(title="illustro", version="0.1.0")
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
        inc = [x for x in include.split(",") if x]
        exc = [x for x in exclude.split(",") if x]
        rat = [x for x in rating.split(",") if x]
        res = searcher.search(q, include=inc, exclude=exc, rating=rat or None, page=page)
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
        return JSONResponse({"images": searcher.similar(id, k=k)})

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
