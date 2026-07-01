"""Pipeline orchestration: scan -> tag (+embed) -> index. Fully incremental, supports step-by-step execution, resumption, and interruption.

Interruption design: pass stop_check() (return True to stop ASAP). Tagging commits per batch,
so processed items are preserved on stop; the next run continues from untagged images.
progress_cb(phase, done, total) reports progress.
"""
from __future__ import annotations

from typing import Callable, Optional

import numpy as np
from PIL import Image
from tqdm import tqdm

from .analyze import overview
from .config import Config
from .db import DB
from .index import VectorStore
from .scan import scan
from .tags_zh import load_merged_zh_table

StopCheck = Optional[Callable[[], bool]]
ProgressCb = Optional[Callable[[str, int, int], None]]


def _noop_stop() -> bool:
    return False


def step_scan(cfg: Config, db: DB, stop_check: StopCheck = None, progress_cb: ProgressCb = None) -> int:
    n = scan(cfg, db, stop_check=stop_check, progress_cb=progress_cb)
    print(f"[scan] Added {n} new images. Total in DB: {db.count()}.")
    return n


def step_tag(
    cfg: Config,
    db: DB,
    store: VectorStore,
    stop_check: StopCheck = None,
    progress_cb: ProgressCb = None,
) -> int:
    stop_check = stop_check or _noop_stop
    todo = db.images_needing_tags()
    if not todo:
        print("[tag] No images pending tagging.")
        return 0
    from .tagger import Wd14Tagger

    tagger = Wd14Tagger(cfg)
    bs = cfg.tagger.batch_size
    total = len(todo)
    done = 0
    stopped = False
    for i in tqdm(range(0, total, bs), desc="tagging", unit="batch"):
        if stop_check():  # Check at batch start for safe interruption
            stopped = True
            break
        chunk = todo[i : i + bs]
        imgs, rows = [], []
        for r in chunk:
            try:
                im = Image.open(r["path"])
                im.load()
                imgs.append(im)
                rows.append(r)
            except Exception as e:
                print(f"[warn] Skipping {r['path']}: {e}")
                db.mark_tagged(r["id"])  # Mark as tagged to avoid repeated retries
        if imgs:
            results = tagger.tag_images(imgs)
            vec_ids = store.add_many(np.stack([res.embedding for res in results]))
            for r, res, vid in zip(rows, results, vec_ids):
                db.set_rating(r["id"], res.rating, res.rating_scores)
                tags = [(n, 0, c) for n, c in res.general] + [(n, 4, c) for n, c in res.character]
                db.add_image_tags(r["id"], tags)
                db.set_vec(r["id"], vid)
                db.mark_tagged(r["id"])
                done += 1
        db.commit()
        store.save_matrix()
        if progress_cb:
            progress_cb("tagging", done, total)
    print(f"[tag] {'Interrupted. ' if stopped else ''}Completed {done}/{total} images this run.")
    return done


def step_apply_zh(cfg: Config, db: DB) -> int:
    table = load_merged_zh_table(cfg.tags_zh_path, cfg.tags_zh_extra_paths)
    n = db.apply_zh_table(table)
    extra_count = len(cfg.tags_zh_extra_paths)
    suffix = f" (+{extra_count} extra table{'s' if extra_count != 1 else ''})" if extra_count else ""
    print(f"[zh] Wrote {n} Chinese tag translations{suffix}.")
    return n


def step_index(cfg: Config, store: VectorStore) -> None:
    store.build_hnsw()


def build(cfg: Config, stop_check: StopCheck = None, progress_cb: ProgressCb = None) -> dict:
    """All-in-one: apply zh -> scan -> tag -> apply zh (new tags) -> build index.

    step_apply_zh runs twice: once at the start (updates translations for
    already-existing tags, so new translations take effect immediately even
    if tagging takes hours or gets interrupted), and once after tagging
    (catches tags created during this round).
    """
    stop_check = stop_check or _noop_stop
    db = DB(cfg.db_path)
    store = VectorStore(cfg)
    try:
        # Apply zh translations early so they're visible even if tagging is slow/interrupted
        if stop_check():
            return overview(db)
        if progress_cb:
            progress_cb("applying_zh", 0, 0)
        step_apply_zh(cfg, db)

        if stop_check():
            return overview(db)
        step_scan(cfg, db, stop_check=stop_check, progress_cb=progress_cb)

        if stop_check():
            return overview(db)
        step_tag(cfg, db, store, stop_check=stop_check, progress_cb=progress_cb)

        # Re-apply zh for any tags created during this round's tagging
        if stop_check():
            return overview(db)
        if progress_cb:
            progress_cb("applying_zh", 0, 0)
        step_apply_zh(cfg, db)

        # Rebuild index if any new vectors exist (even if interrupted, processed items are indexed)
        if stop_check():
            return overview(db)
        if progress_cb:
            progress_cb("indexing", store.count, store.count)
        step_index(cfg, store)

        stats = overview(db)
        print(
            f"[build] Round complete: {stats['total']} images, tagged {stats['tagged']}, "
            f"embedded {stats['embedded']}."
        )
        return stats
    finally:
        db.close()
