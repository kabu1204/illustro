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
class Config:
    image_dirs: list[str] = field(default_factory=list)
    extensions: list[str] = field(
        default_factory=lambda: [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"]
    )
    data_dir: str = "./data"
    device: str = "cuda"
    tagger: TaggerCfg = field(default_factory=TaggerCfg)
    index: IndexCfg = field(default_factory=IndexCfg)
    server: ServerCfg = field(default_factory=ServerCfg)

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
    def tags_zh_path(self) -> Path:
        # Prefer user-customized table under data/, fall back to built-in starter table
        custom = self.data_path / "tags_zh.json"
        return custom if custom.exists() else (ROOT / "illustro" / "data" / "tags_zh.json")


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
        for k in ("image_dirs", "extensions", "data_dir", "device"):
            if k in raw:
                setattr(cfg, k, raw[k])
    else:
        raise FileNotFoundError(
            f"Config file not found: {p}. Copy config.example.yaml to config.yaml and set your image directories."
        )
    cfg.extensions = [e.lower() if e.startswith(".") else "." + e.lower() for e in cfg.extensions]
    return cfg
