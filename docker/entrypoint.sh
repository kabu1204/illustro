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

echo "[entrypoint] Starting web + background worker"
exec python -m illustro.cli --config "$CFG" serve-all
