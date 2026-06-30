"""SQLite storage: image metadata, tags, and image-tag associations.

Design notes:
- images.tagged / embedded / vec_id support incremental processing: only unfinished rows are processed.
- Tag-based search uses image_tags (filter by tag_id + sort by confidence). Chinese search
  relies on tags.name_zh mapping, avoiding FTS Chinese tokenization pitfalls.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    sha256      TEXT,
    dhash       TEXT,                -- Perceptual hash (dHash) for near-duplicate detection
    width       INTEGER,
    height      INTEGER,
    bytes       INTEGER,
    mtime       REAL,
    avg_color   TEXT,                -- Dominant color #rrggbb
    rating      TEXT,                -- general/sensitive/questionable/explicit
    rating_json TEXT,                -- Per-rating scores
    added_at    REAL,
    tagged      INTEGER DEFAULT 0,
    embedded    INTEGER DEFAULT 0,
    vec_id      INTEGER              -- Row index in the vector matrix / HNSW index
);
CREATE INDEX IF NOT EXISTS idx_images_tagged   ON images(tagged);
CREATE INDEX IF NOT EXISTS idx_images_embedded ON images(embedded);
CREATE INDEX IF NOT EXISTS idx_images_dhash    ON images(dhash);
CREATE INDEX IF NOT EXISTS idx_images_rating   ON images(rating);

CREATE TABLE IF NOT EXISTS tags (
    id       INTEGER PRIMARY KEY,
    name     TEXT UNIQUE NOT NULL,  -- Danbooru English tag name
    category INTEGER,               -- 0 general / 4 character / 9 rating
    name_zh  TEXT                   -- Chinese translation (from bilingual tag table)
);
CREATE INDEX IF NOT EXISTS idx_tags_cat ON tags(category);

CREATE TABLE IF NOT EXISTS image_tags (
    image_id   INTEGER NOT NULL,
    tag_id     INTEGER NOT NULL,
    confidence REAL,
    PRIMARY KEY (image_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_it_tag ON image_tags(tag_id);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    def __init__(self, path: str | Path):
        # check_same_thread=False: FastAPI handles requests in a thread pool, so cross-thread connection reuse is needed.
        # This tool is single-user with read-only access during serving (writes only during build), so sharing is safe.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- Images ----------
    def upsert_image(self, **f) -> int:
        """Insert a new image (skip if path already exists). Returns image id."""
        cur = self.conn.execute("SELECT id FROM images WHERE path=?", (f["path"],))
        row = cur.fetchone()
        if row:
            return row["id"]
        f.setdefault("added_at", time.time())
        cols = ",".join(f.keys())
        ph = ",".join("?" * len(f))
        cur = self.conn.execute(
            f"INSERT INTO images ({cols}) VALUES ({ph})", tuple(f.values())
        )
        return cur.lastrowid

    def known_paths(self) -> dict[str, float]:
        """Returns {path: mtime} for all imported images, used to skip unchanged files during incremental scan."""
        return {r["path"]: r["mtime"] for r in self.conn.execute("SELECT path, mtime FROM images")}

    def images_needing_tags(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM images WHERE tagged=0").fetchall()

    def get_image(self, image_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()

    def set_rating(self, image_id: int, rating: str, rating_json: dict):
        self.conn.execute(
            "UPDATE images SET rating=?, rating_json=? WHERE id=?",
            (rating, json.dumps(rating_json), image_id),
        )

    def mark_tagged(self, image_id: int):
        self.conn.execute("UPDATE images SET tagged=1 WHERE id=?", (image_id,))

    def set_vec(self, image_id: int, vec_id: int):
        self.conn.execute(
            "UPDATE images SET embedded=1, vec_id=? WHERE id=?", (vec_id, image_id)
        )

    # ---------- Tags ----------
    def get_or_create_tag(self, name: str, category: int) -> int:
        cur = self.conn.execute("SELECT id FROM tags WHERE name=?", (name,))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO tags (name, category) VALUES (?,?)", (name, category)
        )
        return cur.lastrowid

    def add_image_tags(self, image_id: int, tags: Iterable[tuple[str, int, float]]):
        """tags: list of (name, category, confidence) tuples."""
        for name, cat, conf in tags:
            tid = self.get_or_create_tag(name, cat)
            self.conn.execute(
                "INSERT OR REPLACE INTO image_tags (image_id, tag_id, confidence) VALUES (?,?,?)",
                (image_id, tid, conf),
            )

    def apply_zh_table(self, table: dict[str, str]) -> int:
        """Write {english_tag: chinese} into tags.name_zh. Returns number of updated rows."""
        n = 0
        for en, zh in table.items():
            cur = self.conn.execute(
                "UPDATE tags SET name_zh=? WHERE name=?", (zh, en.replace(" ", "_"))
            )
            n += cur.rowcount
        self.conn.commit()
        return n

    def tags_for_image(self, image_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT t.name, t.name_zh, t.category, it.confidence
               FROM image_tags it JOIN tags t ON t.id=it.tag_id
               WHERE it.image_id=? ORDER BY it.confidence DESC""",
            (image_id,),
        ).fetchall()

    # ---------- Misc ----------
    def count(self, where: str = "") -> int:
        q = "SELECT COUNT(*) c FROM images" + (f" WHERE {where}" if where else "")
        return self.conn.execute(q).fetchone()["c"]

    def set_meta(self, k: str, v: str):
        self.conn.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)", (k, v))

    def get_meta(self, k: str, default=None):
        r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()
