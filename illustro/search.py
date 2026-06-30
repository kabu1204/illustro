"""Hybrid search: exact tag filtering (with Chinese query parsing) + vector nearest neighbor (similarity).

- Text/Chinese query -> parse into danbooru tags -> exact filter in image_tags, sorted by match count/confidence
- Find similar -> kNN on tagger image vectors
(This "anime-native" approach has no text encoder, so text search goes through tags and similarity goes through vectors.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import Config
from .db import DB
from .index import VectorStore
from .tags_zh import build_reverse, load_zh_table, parse_query


@dataclass
class SearchResult:
    total: int
    matched_tags: list[str]
    residual: list[str]
    mode: str
    images: list[dict]


class Searcher:
    def __init__(self, cfg: Config, db: DB, store: Optional[VectorStore] = None):
        self.cfg = cfg
        self.db = db
        self.store = store
        table = load_zh_table(cfg.tags_zh_path)
        self.zh2en = build_reverse(table)
        self.known_en = {r["name"] for r in db.conn.execute("SELECT name FROM tags")}

    # ---------- Text/Tag search ----------
    def search(
        self,
        query: str = "",
        include: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        rating: Optional[list[str]] = None,
        page: int = 1,
        page_size: Optional[int] = None,
    ) -> SearchResult:
        page_size = page_size or self.cfg.server.page_size
        matched, residual = parse_query(query, self.zh2en, self.known_en) if query else ([], [])
        include = list(dict.fromkeys((include or []) + matched))
        exclude = exclude or []
        offset = (page - 1) * page_size

        rating_clause, rating_params = "", []
        if rating:
            rating_clause = " AND i.rating IN (%s)" % ",".join("?" * len(rating))
            rating_params = list(rating)

        exclude_clause, exclude_params = "", []
        if exclude:
            exclude_clause = (
                " AND i.id NOT IN (SELECT it2.image_id FROM image_tags it2 "
                "JOIN tags t2 ON t2.id=it2.tag_id WHERE t2.name IN (%s))"
                % ",".join("?" * len(exclude))
            )
            exclude_params = list(exclude)

        if not include:
            # No usable tags -> browse mode (sorted by import time descending)
            base = f"FROM images i WHERE 1=1 {rating_clause} {exclude_clause}"
            params = rating_params + exclude_params
            total = self.db.conn.execute(f"SELECT COUNT(*) c {base}", params).fetchone()["c"]
            rows = self.db.conn.execute(
                f"SELECT i.* {base} ORDER BY i.added_at DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()
            return SearchResult(total, matched, residual, "browse", [self._img(r) for r in rows])

        # Tag search: try AND first (match all); if empty, fall back to OR (match any)
        n = len(include)
        in_ph = ",".join("?" * n)
        join = (
            "FROM images i "
            "JOIN image_tags it ON it.image_id=i.id "
            f"JOIN tags t ON t.id=it.tag_id AND t.name IN ({in_ph}) "
        )
        where = f"WHERE 1=1 {rating_clause} {exclude_clause} "
        match_expr = "COUNT(DISTINCT it.tag_id)"
        params = list(include) + rating_params + exclude_params

        for mode in ("all", "any"):
            having = f"HAVING {match_expr} = {n} " if mode == "all" else ""
            grouped = f"{join}{where}GROUP BY i.id {having}"
            total = self.db.conn.execute(
                f"SELECT COUNT(*) c FROM (SELECT i.id {grouped})", params
            ).fetchone()["c"]
            if total > 0 or mode == "any":
                rows = self.db.conn.execute(
                    f"SELECT i.*, {match_expr} AS match_n, SUM(it.confidence) AS score "
                    f"{grouped} ORDER BY {match_expr} DESC, SUM(it.confidence) DESC "
                    f"LIMIT ? OFFSET ?",
                    params + [page_size, offset],
                ).fetchall()
                return SearchResult(total, matched, residual, mode, [self._img(r) for r in rows])
        return SearchResult(0, matched, residual, "all", [])

    # ---------- Similar ----------
    def similar(self, image_id: int, k: int = 30) -> list[dict]:
        if not self.store:
            return []
        self.store.maybe_reload()  # In long-running service, auto-pick up new vectors from worker
        img = self.db.get_image(image_id)
        if not img or img["vec_id"] is None:
            return []
        out = []
        for vec_id, sim in self.store.query_by_vec_id(img["vec_id"], k=k + 1):
            r = self.db.conn.execute("SELECT * FROM images WHERE vec_id=?", (vec_id,)).fetchone()
            if r and r["id"] != image_id:
                d = self._img(r)
                d["similarity"] = round(sim, 4)
                out.append(d)
        return out[:k]

    # ---------- Helpers ----------
    def _img(self, row) -> dict:
        d = {
            "id": row["id"],
            "path": row["path"],
            "width": row["width"],
            "height": row["height"],
            "rating": row["rating"],
            "avg_color": row["avg_color"],
        }
        tags = self.db.tags_for_image(row["id"])
        d["tags"] = [
            {
                "name": t["name"],
                "zh": t["name_zh"],
                "cat": t["category"],
                "conf": round(t["confidence"] or 0, 3),
            }
            for t in tags[:30]
        ]
        return d


def autocomplete(db: DB, cfg: Config, prefix: str, limit: int = 12) -> list[dict]:
    """Tag autocomplete: supports both Chinese and English prefix matching."""
    p = f"%{prefix}%"
    rows = db.conn.execute(
        """SELECT t.name, t.name_zh, COUNT(it.image_id) n
           FROM tags t LEFT JOIN image_tags it ON it.tag_id=t.id
           WHERE t.name LIKE ? OR IFNULL(t.name_zh,'') LIKE ?
           GROUP BY t.id ORDER BY n DESC LIMIT ?""",
        (p, p, limit),
    ).fetchall()
    return [{"name": r["name"], "zh": r["name_zh"], "count": r["n"]} for r in rows]
