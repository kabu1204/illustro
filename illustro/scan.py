"""Scan and import: traverse image directories, record metadata (dimensions, size, hash, perceptual hash, dominant color).

Incremental strategy: skip files already in the DB with unchanged mtime.
Hashes: SHA-256 (content dedup) + dHash (near-duplicate / rescaled / lightly edited detection).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from PIL import Image
from tqdm import tqdm

from .config import Config
from .db import DB

Image.MAX_IMAGE_PIXELS = None  # Allow very large images


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _first_frame(img: Image.Image) -> Image.Image:
    """Extract first frame from animated images (e.g. GIF); convert to RGB."""
    if getattr(img, "is_animated", False):
        img.seek(0)
    return img.convert("RGB")


def dhash(img: Image.Image, size: int = 8) -> str:
    """Difference hash: compare adjacent pixel brightness, output hex string. Robust to scaling/compression."""
    g = img.convert("L").resize((size + 1, size), Image.BILINEAR)
    a = np.asarray(g, dtype=np.int16)
    diff = a[:, 1:] > a[:, :-1]
    bits = np.packbits(diff.flatten())
    return bits.tobytes().hex()


def avg_color_hex(img: Image.Image) -> str:
    small = img.convert("RGB").resize((16, 16), Image.BILINEAR)
    arr = np.asarray(small).reshape(-1, 3).mean(axis=0).astype(int)
    return "#{:02x}{:02x}{:02x}".format(*arr)


def iter_image_files(cfg: Config):
    exts = set(cfg.extensions)
    for d in cfg.image_dirs:
        root = Path(d)
        if not root.exists():
            print(f"[warn] Directory does not exist, skipping: {root}")
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in exts:
                yield p


def scan(cfg: Config, db: DB, stop_check=None, progress_cb=None) -> int:
    """Returns the number of newly added images. Stops ASAP when stop_check() returns True (committed items are preserved)."""
    known = db.known_paths()
    files = list(iter_image_files(cfg))
    total = len(files)
    added = 0
    for idx, p in enumerate(tqdm(files, desc="scanning", unit="img")):
        if stop_check and idx % 50 == 0 and stop_check():
            db.commit()
            break
        sp = str(p)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if sp in known and abs(known[sp] - mtime) < 1e-6:
            continue  # Unchanged, skip
        try:
            with Image.open(p) as im:
                w, h = im.size
                frame = _first_frame(im)
                dh = dhash(frame)
                ac = avg_color_hex(frame)
        except Exception as e:
            print(f"[warn] Cannot open {p}: {e}")
            continue
        db.upsert_image(
            path=sp,
            sha256=sha256_file(p),
            dhash=dh,
            width=w,
            height=h,
            bytes=p.stat().st_size,
            mtime=mtime,
            avg_color=ac,
        )
        added += 1
        if added % 500 == 0:
            db.commit()
        if progress_cb and idx % 200 == 0:
            progress_cb("scanning", idx + 1, total)
    db.commit()
    return added
