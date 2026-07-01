#!/bin/sh
# Single-container entrypoint: ensure model is ready, then start web + worker in one process.
# Uses exec so serve-all becomes the container's main process, directly receiving SIGTERM for graceful shutdown.
set -e

# Config priority: /data/config.yaml (user-mounted/custom) > /app/config.yaml (image default)
CFG=/data/config.yaml
[ -f "$CFG" ] || CFG=/app/config.yaml
echo "[entrypoint] Using config: $CFG"

echo "[entrypoint] Preparing model (first run downloads ~468MB; skips if already present)..."
python -m illustro.cli --config "$CFG" download-models || \
  echo "[entrypoint] Model download failed for now; web will still start. Worker will retry automatically once online."

echo "[entrypoint] Fetching ffdkj Chinese tag translations (~30MB SQLite, filtered to WD14 tag set)..."
FFDKJ_FLAGS=""
[ "$FFDKJ_REFRESH" = "1" ] && FFDKJ_FLAGS="--force" && echo "[entrypoint] FFDKJ_REFRESH=1: forcing re-download"
python -m illustro.import_ffdkj --config "$CFG" $FFDKJ_FLAGS 2>&1 | sed 's/^/[ffdkj] /' || \
  echo "[entrypoint] ffdkj download failed; falling back to built-in tags_zh.json only."

echo "[entrypoint] Starting web + background worker"
exec python -m illustro.cli --config "$CFG" serve-all
