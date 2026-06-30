"""CLI entry point.

  python -m illustro.cli init               # Generate config.yaml
  python -m illustro.cli download-models     # Download WD14 model
  python -m illustro.cli build               # Scan + tag + index (incremental, all-in-one)
  python -m illustro.cli scan|tag|index      # Step-by-step
  python -m illustro.cli search "blue_hair school_uniform"  # CLI search
  python -m illustro.cli stats               # Print statistics
  python -m illustro.cli serve               # Start local web server
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import config as cfgmod


def _load(args):
    return cfgmod.load(args.config)


def cmd_init(args):
    root = cfgmod.ROOT
    src, dst = root / "config.example.yaml", root / "config.yaml"
    if dst.exists():
        print(f"config.yaml already exists: {dst}")
    else:
        shutil.copy(src, dst)
        print(f"Generated {dst}. Edit image_dirs to point to your illustration directories.")


def cmd_download(args):
    from .download import download_models
    download_models(_load(args))


def cmd_scan(args):
    from .db import DB
    from .pipeline import step_scan
    cfg = _load(args); db = DB(cfg.db_path); step_scan(cfg, db); db.close()


def cmd_tag(args):
    from .db import DB
    from .index import VectorStore
    from .pipeline import step_apply_zh, step_tag
    cfg = _load(args); db = DB(cfg.db_path); store = VectorStore(cfg)
    step_tag(cfg, db, store); step_apply_zh(cfg, db); db.close()


def cmd_index(args):
    from .index import VectorStore
    from .pipeline import step_index
    step_index(_load(args), VectorStore(_load(args)))


def cmd_build(args):
    from .pipeline import build
    build(_load(args))


def cmd_reset(args):
    """Clear all imported data (db / vectors / index / thumbnails), keeping models for re-testing."""
    import shutil
    cfg = _load(args)
    db = cfg.db_path
    targets = [db, db.parent / (db.name + "-wal"), db.parent / (db.name + "-shm"),
               cfg.emb_path, cfg.hnsw_path]
    removed = []
    for p in targets:
        if p.exists():
            p.unlink(); removed.append(p.name)
    if cfg.thumb_dir.exists():
        shutil.rmtree(cfg.thumb_dir); removed.append("thumbs/")
    print("Removed: " + (", ".join(removed) if removed else "(none)"))
    print(f"Kept: models/ and tags_zh table. Model directory: {cfg.model_dir}")
    print("Run `build` again to re-scan and re-tag from scratch.")


def cmd_search(args):
    from .db import DB
    from .index import VectorStore
    from .search import Searcher
    cfg = _load(args); db = DB(cfg.db_path)
    s = Searcher(cfg, db, VectorStore(cfg))
    res = s.search(args.query, page=1)
    print(f"Matched tags: {res.matched_tags}  Total: {res.total}  Mode={res.mode}")
    for img in res.images[:args.limit]:
        tags = ",".join(t["zh"] or t["name"] for t in img["tags"][:8])
        print(f"  #{img['id']:>6}  {img['rating']:<12} {img['path']}\n           {tags}")
    db.close()


def cmd_stats(args):
    import json
    from .db import DB
    from .analyze import overview
    cfg = _load(args); db = DB(cfg.db_path)
    print(json.dumps(overview(db), ensure_ascii=False, indent=2)); db.close()


def cmd_serve(args):
    import uvicorn
    from server.app import create_app
    cfg = _load(args)
    app = create_app(cfg)
    print(f"Open http://{cfg.server.host}:{cfg.server.port}")
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port)


def cmd_serve_all(args):
    """Single-container entry point: runs web server + background worker (incremental tagging) in one process. Graceful worker shutdown on exit."""
    import os
    import uvicorn
    from server.app import create_app
    from .worker import Worker
    cfg = _load(args)
    interval = args.interval if args.interval is not None else int(os.environ.get("SCAN_INTERVAL", "1800"))
    worker = None if args.no_worker else Worker(cfg, interval=interval)
    app = create_app(cfg, worker=worker)
    if worker is not None:
        # uvicorn triggers shutdown on SIGTERM/SIGINT; gracefully stop the worker (interrupt current batch + sleep)
        app.add_event_handler("shutdown", lambda: worker.stop())
    wmsg = "disabled" if args.no_worker else f"enabled (interval {interval}s)"
    print(f"Open http://{cfg.server.host}:{cfg.server.port}  Background processing: {wmsg}")
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port)


def build_parser():
    p = argparse.ArgumentParser(prog="illustro")
    p.add_argument("--config", default=None, help="Path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("download-models").set_defaults(func=cmd_download)
    sub.add_parser("scan").set_defaults(func=cmd_scan)
    sub.add_parser("tag").set_defaults(func=cmd_tag)
    sub.add_parser("index").set_defaults(func=cmd_index)
    sub.add_parser("build").set_defaults(func=cmd_build)
    sub.add_parser("reset").set_defaults(func=cmd_reset)
    sub.add_parser("stats").set_defaults(func=cmd_stats)
    sub.add_parser("serve").set_defaults(func=cmd_serve)
    sa = sub.add_parser("serve-all")  # Single container: web + background worker
    sa.add_argument("--interval", type=int, default=None, help="Incremental scan interval in seconds (default: 1800 or $SCAN_INTERVAL)")
    sa.add_argument("--no-worker", action="store_true", help="Run web server only, no background processing")
    sa.set_defaults(func=cmd_serve_all)
    sp = sub.add_parser("search"); sp.add_argument("query"); sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_search)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
