"""Download ffdkj's Danbooru tag Chinese-translation SQLite DB and extract a
{english: chinese} JSON filtered to the WD14 tag set.

Source: https://github.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table
  - Daily updated, 315K+ tags (post_count >= 10), Gemini 3 Flash + human review.
  - SQLite schema: tags(name TEXT PK, category INTEGER, cn_name TEXT, post_count INTEGER)

Output: illustro/data/tags_ffdkj.json  — same format as tags_zh.json, ready to
feed into DB.apply_zh_table or to merge with the built-in starter table.

Usage:
  python -m illustro.import_ffdkj                 # download + extract
  python -m illustro.import_ffdkj --no-download   # reuse already-downloaded DB
  python -m illustro.import_ffdkj --merge         # merge into tags_zh.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .config import ROOT
from . import config as cfgmod

REPO_URL = "https://github.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table"
DB_URL = "https://github.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table/raw/main/tag.sqlite"

DEFAULT_CSV = ROOT / "illustro" / "data" / "models" / "SmilingWolf" / "wd-swinv2-tagger-v3" / "tags_info.csv"


def download_db(dest: Path) -> None:
    """Download the ffdkj SQLite DB with a streaming request + progress bar."""
    import requests
    from tqdm import tqdm

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    print(f"[*] Downloading {DB_URL}")
    with requests.get(DB_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(tmp, "wb") as f, tqdm(total=total, unit="B", unit_scale=True, desc="ffdkj.db") as pbar:
            for chunk in r.iter_content(chunk_size=1 << 20):  # 1 MiB
                f.write(chunk)
                pbar.update(len(chunk))
    tmp.replace(dest)
    print(f"    -> {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def load_wd14_tags(csv_path: Path) -> set[str]:
    """Read tag names from the WD14 tags_info.csv."""
    import csv
    names: set[str] = set()
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row["name"].strip()
            if name:
                names.add(name)
    return names


def extract_translations(db_path: Path, wd14_tags: set[str]) -> dict[str, str]:
    """Query the ffdkj SQLite for translations of tags in wd14_tags."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # Check schema to find the right table/columns
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "tags" in tables:
            table = "tags"
        elif len(tables) == 1:
            table = next(iter(tables))
        else:
            raise RuntimeError(f"Unexpected schema, tables: {tables}")

        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        # Expected: name, cn_name
        if "name" not in cols or "cn_name" not in cols:
            raise RuntimeError(f"Unexpected columns in {table}: {cols}")

        # Build a placeholder list for the IN clause (sqlite has a limit ~999 for
        # direct params, but we only have ~10K tags — use a temp table to be safe).
        conn.execute("CREATE TEMP TABLE wd14 (name TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO wd14 (name) VALUES (?)", [(t,) for t in wd14_tags])
        rows = conn.execute(
            f"SELECT t.name, t.cn_name FROM {table} t "
            "JOIN wd14 w ON t.name = w.name "
            "WHERE t.cn_name IS NOT NULL AND t.cn_name != ''"
        ).fetchall()
    finally:
        conn.close()

    return {name: cn for name, cn in rows}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="illustro.import_ffdkj",
        description="Download ffdkj Danbooru tag translations and extract WD14 subset.",
    )
    p.add_argument("--config", default=None, help="Path to config.yaml (determines data_dir for output)")
    p.add_argument("--csv", type=Path, default=None, help="WD14 tags_info.csv (default: from config model_dir)")
    p.add_argument("--db-cache", type=Path, default=None, help="Local cache for downloaded SQLite")
    p.add_argument("--output", "-o", type=Path, default=None, help="Output JSON path")
    p.add_argument("--no-download", action="store_true", help="Reuse the cached DB instead of re-downloading")
    p.add_argument("--merge", action="store_true", help="Merge result into tags_zh.json (ffdkj wins on conflict)")
    args = p.parse_args(argv)

    # Load config to resolve data_dir and model paths
    try:
        cfg = cfgmod.load(args.config) if args.config else cfgmod.load()
    except FileNotFoundError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1
    data_path = cfg.data_path

    # Resolve defaults from config
    csv_path = args.csv or (cfg.model_dir / cfg.tagger.tags_csv)
    db_cache = args.db_cache or (data_path / "ffdkj_tag.sqlite")
    output = args.output or (data_path / "tags_ffdkj.json")

    if not csv_path.exists():
        print(f"[!] WD14 CSV not found: {csv_path}", file=sys.stderr)
        print("    Run: python -m illustro.cli download-models", file=sys.stderr)
        return 1

    # --- Download (or reuse) ---
    if args.no_download:
        if not db_cache.exists():
            print(f"[!] --no-download but cache missing: {db_cache}", file=sys.stderr)
            return 1
        print(f"[*] Reusing cached DB: {db_cache}")
    else:
        download_db(db_cache)

    # --- Load WD14 tag set ---
    wd14_tags = load_wd14_tags(csv_path)
    print(f"[*] WD14 tags: {len(wd14_tags)}")

    # --- Extract translations ---
    print(f"[*] Querying ffdkj DB for matching translations...")
    mapping = extract_translations(db_cache, wd14_tags)
    print(f"    Matched: {len(mapping)} / {len(wd14_tags)} ({100 * len(mapping) / len(wd14_tags):.1f}%)")
    miss = len(wd14_tags) - len(mapping)
    if miss:
        print(f"    Untranslated: {miss} (mostly obscure / low-frequency tags)")

    # --- Write output ---
    output.parent.mkdir(parents=True, exist_ok=True)
    out = {"_comment": f"Danbooru 英文标签 -> 中文，来自 {REPO_URL}，已按 WD14 tag 集合过滤。"}
    out.update(mapping)
    output.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[*] Wrote {len(mapping)} entries -> {output}")

    # --- Optional merge into tags_zh.json ---
    if args.merge:
        tags_zh = cfg.tags_zh_path
        if not tags_zh.exists():
            print(f"[!] tags_zh.json not found at {tags_zh}, skipping merge", file=sys.stderr)
            return 0
        existing = json.loads(tags_zh.read_text(encoding="utf-8"))
        comment = existing.get("_comment")
        existing = {k: v for k, v in existing.items() if not k.startswith("_")}
        before = len(existing)
        existing.update(mapping)  # ffdkj wins on conflict
        if comment:
            existing = {"_comment": comment, **existing}
        tags_zh.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        added = len(existing) - before
        print(f"[*] Merged into {tags_zh}: +{added} new, {len(existing)} total")

    return 0


if __name__ == "__main__":
    sys.exit(main())
