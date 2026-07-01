"""Bilingual tag table loading and query parsing.

Chinese search approach (avoids FTS Chinese tokenization pitfalls):
  Maintain an {english_tag: chinese} table, reverse it to {chinese: english}. User's Chinese query
  is scanned against the reverse table using "longest match" to extract known tags, which are
  converted to English danbooru tag sets for exact filtering in image_tags.
  Unrecognized tokens are ignored (or left for direct English tag matching).
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def load_zh_table(path: str | Path) -> dict[str, str]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def load_merged_zh_table(base_path: str | Path, extra_paths: list[str | Path]) -> dict[str, str]:
    """Load the base zh table, then layer each extra file on top (later wins on conflict).

    Missing extra files are silently skipped — callers should pre-filter, but this
    adds a safety net. The base file is required.
    """
    table = load_zh_table(base_path)
    for p in extra_paths:
        p = Path(p)
        if not p.exists():
            continue
        extra = load_zh_table(p)
        table.update(extra)
    return table


def build_reverse(table: dict[str, str]) -> dict[str, str]:
    """Chinese/alias -> English tag. If multiple English tags map to the same Chinese, the last one wins."""
    rev: dict[str, str] = {}
    for en, zh in table.items():
        rev[zh] = en
    return rev


_TOKEN = re.compile(r"[ ,，、;；/]+")


def parse_query(query: str, zh2en: dict[str, str], known_en: set[str]) -> tuple[list[str], list[str]]:
    """Parse query into (matched English tags, unrecognized tokens).

    - First split by delimiters; each token is matched via longest match against the reverse table if Chinese,
      or normalized to underscores and checked against known_en if English.
    - Also supports longest-match scanning for Chinese input without spaces.
    """
    matched: list[str] = []
    residual: list[str] = []

    parts = [p for p in _TOKEN.split(query.strip()) if p]
    # Prepare Chinese keys for longest match (sorted by length descending)
    zh_keys = sorted(zh2en.keys(), key=len, reverse=True)

    def match_chinese_run(s: str):
        i = 0
        n = len(s)
        while i < n:
            hit = None
            for k in zh_keys:
                if k and s.startswith(k, i):
                    hit = k
                    break
            if hit:
                matched.append(zh2en[hit])
                i += len(hit)
            else:
                i += 1  # Skip unrecognized character

    for p in parts:
        if re.search(r"[一-鿿]", p):       # Contains Chinese
            if p in zh2en:
                matched.append(zh2en[p])
            else:
                match_chinese_run(p)
        else:                                       # English / romanized
            en = p.lower().replace(" ", "_")
            if en in known_en:
                matched.append(en)
            else:
                residual.append(p)

    # Deduplicate while preserving order
    seen = set()
    uniq = [m for m in matched if not (m in seen or seen.add(m))]
    return uniq, residual
