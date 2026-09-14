"""Configuration loader. Reads config.yaml and provides access with defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.yaml"


@dataclass
class TaggerCfg:
    hf_repo: str = "deepghs/wd14_tagger_with_embeddings"
    onnx_file: str = "SmilingWolf/wd-swinv2-tagger-v3/model.onnx"
    tags_csv: str = "SmilingWolf/wd-swinv2-tagger-v3/tags_info.csv"
    general_threshold: float = 0.35
    character_threshold: float = 0.75
    batch_size: int = 8
    openvino_device: str = "AUTO"  # Used when device=openvino: GPU (iGPU) / CPU / AUTO / NPU


@dataclass
class IndexCfg:
    space: str = "cosine"
    ef_construction: int = 200
    M: int = 16
    ef_search: int = 64


@dataclass
class ServerCfg:
    host: str = "127.0.0.1"
    port: int = 8000
    thumbnail_size: int = 360
    page_size: int = 60


@dataclass
class SyncCfg:
    """Mobile -> server one-way upload sync (see /api/sync/*)."""
    enabled: bool = True
    # If set, /api/sync/* endpoints require this token in the X-API-Token header.
    # Empty = no auth (fine on a trusted LAN; set one when reachable over VPN).
    token: str = ""
    # Where uploaded files land before the scanner picks them up.
    # Empty = <data_dir>/inbox. Always appended to image_dirs automatically.
    inbox_dir: str = ""
    max_upload_mb: int = 100


@dataclass
class Config:
    image_dirs: list[str] = field(default_factory=list)
    extensions: list[str] = field(
        default_factory=lambda: [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"]
    )
    data_dir: str = "./data"
    device: str = "cuda"
    # Extra Chinese translation files (under data_dir) layered on top of the base
    # tags_zh.json at runtime. Later files override earlier ones. Files that don't
    # exist are silently skipped. Use this to load large NSFW tables (e.g. ffdkj)
    # without committing them to the repo.
    tags_zh_extra: list[str] = field(default_factory=lambda: ["tags_ffdkj.json"])
    tagger: TaggerCfg = field(default_factory=TaggerCfg)
    index: IndexCfg = field(default_factory=IndexCfg)
    server: ServerCfg = field(default_factory=ServerCfg)
    sync: SyncCfg = field(default_factory=SyncCfg)

    # ---- Derived paths ----
    @property
    def data_path(self) -> Path:
        p = (ROOT / self.data_dir).resolve() if not os.path.isabs(self.data_dir) else Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path / "illustro.db"

    @property
    def emb_path(self) -> Path:
        return self.data_path / "embeddings.npy"

    @property
    def hnsw_path(self) -> Path:
        return self.data_path / "hnsw.index"

    @property
    def model_dir(self) -> Path:
        p = self.data_path / "models"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def thumb_dir(self) -> Path:
        p = self.data_path / "thumbs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def inbox_path(self) -> Path:
        """Upload landing zone. Created eagerly; auto-watched by the scanner."""
        raw = self.sync.inbox_dir.strip()
        if not raw:
            p = self.data_path / "inbox"
        elif os.path.isabs(raw):
            p = Path(raw)
        else:
            p = (ROOT / raw).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def tags_zh_path(self) -> Path:
        # Prefer user-customized table under data/, fall back to built-in starter table
        custom = self.data_path / "tags_zh.json"
        return custom if custom.exists() else (ROOT / "illustro" / "data" / "tags_zh.json")

    @property
    def tags_zh_extra_paths(self) -> list[Path]:
        """Resolve extra translation file paths under data_dir, skipping missing ones."""
        return [self.data_path / name for name in self.tags_zh_extra if (self.data_path / name).exists()]


def _merge(dc: Any, raw: dict) -> Any:
    """Merge a yaml dict into a dataclass instance (one level of nesting)."""
    for k, v in (raw or {}).items():
        if not hasattr(dc, k):
            continue
        cur = getattr(dc, k)
        if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
            _merge(cur, v)
        else:
            setattr(dc, k, v)
    return dc


def load(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    p = Path(path) if path else DEFAULT_CONFIG
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        # Instantiate nested dataclasses before merging
        cfg.tagger = _merge(TaggerCfg(), raw.get("tagger", {}))
        cfg.index = _merge(IndexCfg(), raw.get("index", {}))
        cfg.server = _merge(ServerCfg(), raw.get("server", {}))
        cfg.sync = _merge(SyncCfg(), raw.get("sync", {}))
        for k in ("image_dirs", "extensions", "data_dir", "device", "tags_zh_extra"):
            if k in raw:
                setattr(cfg, k, raw[k])
    else:
        raise FileNotFoundError(
            f"Config file not found: {p}. Copy config.example.yaml to config.yaml and set your image directories."
        )
    cfg.extensions = [e.lower() if e.startswith(".") else "." + e.lower() for e in cfg.extensions]
    # Uploaded files land in the inbox; make sure the scanner watches it so the
    # background worker picks them up without the user editing image_dirs.
    if cfg.sync.enabled:
        inbox = str(cfg.inbox_path)
        if inbox not in cfg.image_dirs:
            cfg.image_dirs.append(inbox)
    return cfg
