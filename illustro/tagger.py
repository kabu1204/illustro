"""WD14 tagging + image embeddings (both from a single ONNX model).

Uses deepghs' WD14 variant with embedding output: a single forward pass yields
  - Confidence scores for each danbooru tag (sigmoid)
  - Penultimate-layer feature vector (anime-domain image embedding for similarity search)

Preprocessing follows the SmilingWolf WD14 convention:
  Composite onto white background -> pad to square -> resize to model input size -> RGB to BGR -> float32 (0-255, no normalization)
The model uses NHWC input. Input size is auto-detected from the session, and outputs
are split into "tag predictions" vs "embedding" based on shape, for robustness across sub-models.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .config import Config


@dataclass
class TagResult:
    rating: str
    rating_scores: dict[str, float]
    general: list[tuple[str, float]]    # (tag, conf)
    character: list[tuple[str, float]]
    embedding: np.ndarray               # float32 vector


def _providers(device: str) -> list:
    device = (device or "cuda").lower()
    if device == "cuda":
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    if device in ("dml", "directml"):
        return ["DmlExecutionProvider", "CPUExecutionProvider"]
    if device in ("openvino", "ov"):
        return ["OpenVINOExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def _preload_cuda_libs() -> None:
    """Preload CUDA libraries bundled with torch / nvidia pip packages so onnxruntime can find them.

    torch works on GPU because it ships CUDA runtime libs (cublas/cudnn/...) in
    site-packages/nvidia/*/lib or torch/lib and loads them itself. onnxruntime by
    default only searches system library paths, so it reports missing libcublasLt.so.12 / libcudnn.so.9.
    Here we use ctypes to preload those .so files with RTLD_GLOBAL into the current process,
    so onnxruntime's provider dlopen can reuse them without manually setting LD_LIBRARY_PATH.
    """
    import ctypes
    import glob
    import os

    libdirs: list[str] = []
    try:
        import nvidia  # torch's CUDA dependencies (nvidia-*-cu12) live in this namespace
        base = os.path.dirname(nvidia.__file__)
        libdirs += glob.glob(os.path.join(base, "*", "lib"))
    except ImportError:
        pass
    try:
        import torch  # Legacy layout: libs are directly in torch/lib
        libdirs.append(os.path.join(os.path.dirname(torch.__file__), "lib"))
    except ImportError:
        pass

    sofiles: list[str] = []
    for d in libdirs:
        sofiles += glob.glob(os.path.join(d, "*.so*"))
    # Load twice to resolve inter-library dependency ordering; silently skip failures
    for _ in range(2):
        for so in sofiles:
            try:
                ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


class Wd14Tagger:
    def __init__(self, cfg: Config):
        import onnxruntime as ort  # Lazy import so other commands work without onnxruntime installed

        self.cfg = cfg
        model_path = cfg.model_dir / cfg.tagger.onnx_file
        csv_path = cfg.model_dir / cfg.tagger.tags_csv
        if not model_path.exists() or not csv_path.exists():
            raise FileNotFoundError(
                f"Model or tag file missing:\n  {model_path}\n  {csv_path}\n"
                f"Run: python -m illustro.cli download-models"
            )
        dev = (cfg.device or "cuda").lower()
        if dev == "cuda":
            _preload_cuda_libs()  # Reuse torch's bundled CUDA libs, no need to set LD_LIBRARY_PATH
        so = ort.SessionOptions()
        so.log_severity_level = 3  # Suppress benign batch dimension shape warnings
        provider_options = None
        if dev in ("openvino", "ov"):
            # OpenVINO EP: device_type selects iGPU (GPU) / CPU / AUTO / NPU; corresponds 1:1 with providers
            provider_options = [{"device_type": cfg.tagger.openvino_device}, {}]
        self.sess = ort.InferenceSession(
            str(model_path), sess_options=so,
            providers=_providers(cfg.device), provider_options=provider_options,
        )
        active = self.sess.get_providers()
        ep = active[0] if active else "?"
        accel = ep.startswith(("CUDA", "Dml", "ROCM", "OpenVINO"))
        tail = ep
        if ep.startswith("OpenVINO"):
            tail += f" (device_type={cfg.tagger.openvino_device})"
        print(f"[tagger] Execution backend: {tail}" + ("" if accel else "  <- WARNING: running on CPU, large batches will be slow"))
        self.inp = self.sess.get_inputs()[0]
        # Read input size and channel layout (NHWC: [N,H,W,C])
        shape = [d if isinstance(d, int) else -1 for d in self.inp.shape]
        if shape[-1] == 3:           # NHWC layout
            self.size = shape[1] if shape[1] > 0 else 448
            self.layout = "NHWC"
        else:                         # NCHW layout
            self.size = shape[2] if shape[2] > 0 else 448
            self.layout = "NCHW"
        self._load_tags(csv_path)
        self._resolve_outputs()

    def _load_tags(self, csv_path: Path):
        self.names: list[str] = []
        self.cats: list[int] = []
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                self.names.append(row["name"])
                self.cats.append(int(row["category"]))
        self.cats_arr = np.array(self.cats)
        self.rating_idx = np.where(self.cats_arr == 9)[0]
        self.general_idx = np.where(self.cats_arr == 0)[0]
        self.char_idx = np.where(self.cats_arr == 4)[0]

    def _resolve_outputs(self):
        """Distinguish two outputs: the one whose length equals tag count is predictions, the other is embedding."""
        n_tags = len(self.names)
        self.pred_out = None
        self.emb_out = None
        for o in self.sess.get_outputs():
            last = o.shape[-1] if o.shape else None
            if isinstance(last, int) and last == n_tags:
                self.pred_out = o.name
            else:
                self.emb_out = o.name
        if self.pred_out is None:
            # Fallback: treat first output as predictions
            self.pred_out = self.sess.get_outputs()[0].name

    # ---------- Preprocessing ----------
    def _preprocess(self, img: Image.Image) -> np.ndarray:
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg.convert("RGB")
        w, h = img.size
        s = max(w, h)
        square = Image.new("RGB", (s, s), (255, 255, 255))
        square.paste(img, ((s - w) // 2, (s - h) // 2))
        square = square.resize((self.size, self.size), Image.BILINEAR)
        arr = np.asarray(square, dtype=np.float32)      # HWC, RGB, 0-255
        arr = arr[:, :, ::-1]                           # RGB -> BGR
        if self.layout == "NCHW":
            arr = arr.transpose(2, 0, 1)
        return arr

    # ---------- Inference ----------
    def _run(self, batch: np.ndarray):
        outs = [self.pred_out] + ([self.emb_out] if self.emb_out else [])
        res = self.sess.run(outs, {self.inp.name: batch})
        preds = res[0]
        if preds.max() > 1.0 or preds.min() < 0.0:       # Safety: apply sigmoid if needed
            preds = 1.0 / (1.0 + np.exp(-preds))
        embs = res[1] if self.emb_out else None
        return preds, embs

    def tag_images(self, images: list[Image.Image]) -> list[TagResult]:
        batch = np.stack([self._preprocess(im) for im in images], axis=0)
        preds, embs = self._run(batch)
        gt = self.cfg.tagger.general_threshold
        ct = self.cfg.tagger.character_threshold
        results = []
        for i in range(len(images)):
            p = preds[i]
            rscores = {self.names[j]: float(p[j]) for j in self.rating_idx}
            rating = max(rscores, key=rscores.get) if rscores else "general"
            general = sorted(
                [(self.names[j], float(p[j])) for j in self.general_idx if p[j] >= gt],
                key=lambda x: x[1], reverse=True,
            )
            character = sorted(
                [(self.names[j], float(p[j])) for j in self.char_idx if p[j] >= ct],
                key=lambda x: x[1], reverse=True,
            )
            if embs is not None:
                emb = embs[i].astype(np.float32).flatten()
            else:
                # Fallback when model has no embedding output: use prediction probability vector (still works for similarity)
                emb = p.astype(np.float32)
            results.append(TagResult(rating, rscores, general, character, emb))
        return results
