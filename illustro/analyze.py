"""Collection analytics: aggregates tags/metadata into visualization-ready statistics."""
from __future__ import annotations

from .db import DB


def overview(db: DB) -> dict:
    total = db.count()
    tagged = db.count("tagged=1")
    embedded = db.count("embedded=1")

    ratings = {
        r["rating"] or "unknown": r["c"]
        for r in db.conn.execute(
            "SELECT rating, COUNT(*) c FROM images GROUP BY rating ORDER BY c DESC"
        )
    }

    top_general = [
        {"name": r["name"], "zh": r["name_zh"], "count": r["c"]}
        for r in db.conn.execute(
            """SELECT t.name, t.name_zh, COUNT(it.image_id) c
               FROM tags t JOIN image_tags it ON it.tag_id=t.id
               WHERE t.category=0 GROUP BY t.id ORDER BY c DESC LIMIT 40"""
        )
    ]
    top_char = [
        {"name": r["name"], "zh": r["name_zh"], "count": r["c"]}
        for r in db.conn.execute(
            """SELECT t.name, t.name_zh, COUNT(it.image_id) c
               FROM tags t JOIN image_tags it ON it.tag_id=t.id
               WHERE t.category=4 GROUP BY t.id ORDER BY c DESC LIMIT 30"""
        )
    ]

    # Orientation distribution
    orient = {"portrait": 0, "landscape": 0, "square": 0}
    for r in db.conn.execute("SELECT width, height FROM images WHERE width>0 AND height>0"):
        w, h = r["width"], r["height"]
        if abs(w - h) / max(w, h) < 0.05:
            orient["square"] += 1
        elif h > w:
            orient["portrait"] += 1
        else:
            orient["landscape"] += 1

    # Dominant color buckets (coarse H/S/L grouping)
    palette = _color_buckets(db)

    # Near-duplicates (grouped by identical dhash)
    dup_groups = db.conn.execute(
        "SELECT COUNT(*) g FROM (SELECT dhash FROM images WHERE dhash IS NOT NULL "
        "GROUP BY dhash HAVING COUNT(*)>1)"
    ).fetchone()["g"]
    dup_images = db.conn.execute(
        "SELECT IFNULL(SUM(c),0) s FROM (SELECT COUNT(*) c FROM images "
        "WHERE dhash IS NOT NULL GROUP BY dhash HAVING c>1)"
    ).fetchone()["s"]

    return {
        "total": total,
        "tagged": tagged,
        "embedded": embedded,
        "ratings": ratings,
        "top_general": top_general,
        "top_characters": top_char,
        "orientation": orient,
        "palette": palette,
        "duplicates": {"groups": dup_groups, "images": dup_images},
    }


def _color_buckets(db: DB) -> list[dict]:
    import colorsys

    buckets: dict[str, int] = {}
    names = ["red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink"]
    for r in db.conn.execute("SELECT avg_color FROM images WHERE avg_color IS NOT NULL"):
        hexv = r["avg_color"]
        try:
            rr = int(hexv[1:3], 16) / 255
            gg = int(hexv[3:5], 16) / 255
            bb = int(hexv[5:7], 16) / 255
        except (ValueError, TypeError):
            continue
        h, l, s = colorsys.rgb_to_hls(rr, gg, bb)
        if l < 0.12:
            key = "black"
        elif l > 0.9:
            key = "white"
        elif s < 0.12:
            key = "gray"
        else:
            idx = int((h * 8) + 0.5) % 8
            key = names[idx]
        buckets[key] = buckets.get(key, 0) + 1
    return [{"color": k, "count": v} for k, v in sorted(buckets.items(), key=lambda x: -x[1])]


def duplicate_clusters(db: DB, limit: int = 50) -> list[dict]:
    """Returns near-duplicate image groups (identical dhash) for cleanup."""
    groups = db.conn.execute(
        "SELECT dhash, COUNT(*) c FROM images WHERE dhash IS NOT NULL "
        "GROUP BY dhash HAVING c>1 ORDER BY c DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for g in groups:
        imgs = db.conn.execute(
            "SELECT id, path, width, height, bytes FROM images WHERE dhash=?", (g["dhash"],)
        ).fetchall()
        out.append({"dhash": g["dhash"], "count": g["c"], "images": [dict(i) for i in imgs]})
    return out
