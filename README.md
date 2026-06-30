# illustro - Anime illustration collection tagging + semantic search + analytics

Turn your "just saved" illustration collection into a **searchable, similar-findable, analyzable** local asset library.
Fully offline, anime-native. Runs on RTX 4060 (CUDA), Intel N100 iGPU (OpenVINO), CPU, or DirectML.

## Features

- **Chinese/tag search**: Search "blue_hair school_uniform" or Chinese equivalents, get instant results.
- **Find similar**: Click any image -> find the closest matches by art style/composition using image vectors.
- **Collection analytics**: Top tags / character rankings, rating distribution, dominant colors, orientation, near-duplicate detection.
- **Incremental**: Drop new images into your directory, re-run and only new files are processed.
- **Background worker**: In single-container mode, a background thread continuously scans and tags new images. Pause/resume/trigger from the UI.
- **Docker deployment**: Single-container image for TrueNAS SCALE / N100 with Intel iGPU acceleration via OpenVINO. See [docker/README.md](docker/README.md).

## Tech stack (anime-native)

| Capability | What | Notes |
|---|---|---|
| Tagging + image vectors | `wd14-with-embeddings` (ONNX) | One model, one forward pass: danbooru tags + anime-domain image embedding |
| Chinese exact search | Bilingual tag table + longest-match parsing | Chinese query -> danbooru English tags -> exact `image_tags` filter, avoiding FTS Chinese tokenization pitfalls |
| Similar / nearest neighbor | Tagger image vectors + hnswlib | In-domain vectors, better suited for anime than generic CLIP |
| Metadata / tags | SQLite | Incremental, queryable |
| UI | FastAPI + single-file frontend | Local web page, masonry gallery |
| Deployment | Docker (single container) | TrueNAS SCALE / N100 / OpenVINO iGPU |
| Background processing | Worker thread | Incremental auto-scan, pause/resume/trigger |

> No generic CLIP (jina/SigLIP/Chinese-CLIP): those are natural photo domain, out-of-domain for anime.
> Exact attributes go through tags (native imageboard annotations, most accurate), similarity goes through tagger vectors. Simple and more on-target.

## Directory structure

```
illustration/
├─ config.example.yaml      # Copy to config.yaml and set your image directories
├─ requirements.txt
├─ illustro/
│  ├─ __init__.py            # Package + version
│  ├─ __main__.py            # python -m illustro entry
│  ├─ cli.py                 # CLI entry point
│  ├─ config.py              # Configuration
│  ├─ scan.py                # Scan & import (hash/size/dHash/dominant color, incremental)
│  ├─ db.py                  # SQLite (images / tags / image_tags)
│  ├─ tagger.py              # WD14 ONNX inference: tags + vectors
│  ├─ tags_zh.py             # Chinese query parsing (longest match)
│  ├─ index.py               # hnswlib vector index
│  ├─ search.py              # Hybrid search (tags + vectors)
│  ├─ analyze.py             # Collection statistics
│  ├─ pipeline.py            # scan->tag->index orchestration
│  ├─ worker.py              # Background worker (pause/resume/stop, incremental auto-scan)
│  ├─ download.py            # Download WD14 model
│  └─ data/tags_zh.json      # Bilingual tag starter table (extensible)
├─ server/
│  ├─ app.py                 # FastAPI endpoints
│  └─ static/index.html      # Frontend gallery
└─ docker/
   ├─ Dockerfile             # Container image (N100 / OpenVINO)
   ├─ docker-compose.yml     # Compose for TrueNAS SCALE
   ├─ config.docker.yaml     # Container default config
   ├─ entrypoint.sh          # Container entrypoint
   ├─ requirements.docker.txt  # Container dependencies (onnxruntime-openvino)
   └─ README.md              # Deployment guide
```

## Installation

Requires Python 3.10+.

```bash
pip install -r requirements.txt
# Default requirements use onnxruntime-gpu (CUDA 12.x).
# CPU only: switch to onnxruntime; for DirectML: switch to onnxruntime-directml.
# For Intel iGPU (OpenVINO): see docker/requirements.docker.txt
```

## Usage

```bash
# 1) Generate config, edit image_dirs to point to your illustration directories
python -m illustro.cli init

# 2) Download WD14 (with embeddings) model -- weights go directly into data/models/ (no global HF cache).
#    The script lists repo files; if config paths don't match, follow the prompts to update config.yaml and re-run.
python -m illustro.cli download-models

# 3) All-in-one: scan -> tag+vectorize -> build index (incremental, resumable)
python -m illustro.cli build
#    Or step-by-step: scan / tag / index

# 4) Start local web server
python -m illustro.cli serve
#    Open http://127.0.0.1:8000 in your browser

# 5) Single-container mode: web + background worker (auto-scan every 30 min, pause/resume from UI)
python -m illustro.cli serve-all
#    Or with custom interval: python -m illustro.cli serve-all --interval 600
#    Or web only:             python -m illustro.cli serve-all --no-worker

# CLI search also works
python -m illustro.cli search "blue_hair school_uniform"
python -m illustro.cli stats

# Reset all imported data (keeps models), useful for re-testing
python -m illustro.cli reset
```

~15-30 min for 15k-20k images on a 4060 (tagging+vectorization, one-time). After that, search/similarity are millisecond-level.

### Docker deployment

For TrueNAS SCALE / N100 / OpenVINO deployment, see [docker/README.md](docker/README.md).

```bash
docker build -t illustro:latest -f docker/Dockerfile .
docker compose -f docker/docker-compose.yml up -d
```

## How Chinese search works

Danbooru tags are in English (`blue_hair`), so Chinese search relies on the bilingual table at
`illustro/data/tags_zh.json` (English tag -> Chinese). Chinese input is parsed using **longest match**
(even concatenated input like "蓝发和服" is split correctly), converted to English tag sets,
then filtered exactly in `image_tags`.

**To improve search coverage, extend this table**: add Chinese terms you search for often.
Placing a copy at `data/tags_zh.json` overrides the built-in table (survives upgrades).
The danbooru wiki has community-maintained Chinese tag translations you can bulk import.

## Configuration (config.yaml)

- `image_dirs`: Image directories, multiple allowed, scanned recursively.
- `device`: `cuda` / `cpu` / `dml` / `openvino`.
- `tagger.hf_repo / onnx_file / tags_csv`: Model repo and filenames. Confirm after running `download-models`.
- `tagger.general_threshold` (0.35) / `character_threshold` (0.75): Tag confidence thresholds.
- `tagger.openvino_device`: When `device: openvino`, selects `AUTO` / `GPU` / `CPU` / `NPU`.
- `index.M / ef_*`: HNSW parameters, defaults are sufficient for ~20k images.

## Design decisions

- **Vector index**: At 20k images, brute-force numpy cosine is fast enough; hnswlib is used for incremental-friendly growth and future scalability.
- **Deduplication**: dHash finds near-duplicates (robust to scaling/light compression); `/api/duplicates` and stats show grouped suspected duplicates for easy cleanup.
- **Rating**: WD14 outputs general/sensitive/questionable/explicit; UI can filter by rating.
- **Background worker**: In `serve-all` mode, a daemon thread runs incremental builds on a configurable interval. The web UI shows live progress and exposes pause/resume/trigger controls. Graceful shutdown on `docker stop` / Ctrl+C.

## Future plans (not yet implemented)

- True fuzzy semantics (e.g. "twilight backlight loneliness" that tags can't express) -> integrate **DanbooruCLIP** text-image model.
- **CCIP** (deepghs) for "find all images of this character".
- pixiv ID -> artist-level analytics (if filenames contain IDs).
