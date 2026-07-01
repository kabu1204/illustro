# Deploying illustro on TrueNAS SCALE (N100)

Single container: web search + background incremental tagging (OpenVINO on iGPU) in one process.
Processing can be **paused / resumed / triggered** anytime. `docker stop` performs a graceful shutdown (no data loss).

## 1. Build the image

From the project root (where `illustro/`, `server/`, `docker/` are visible):

```bash
docker build -t illustro:latest -f docker/Dockerfile .
```

## 2. Prepare persistent directory (config is optional)

```bash
mkdir -p /mnt/tank/apps/illustro
```

The image ships with a **built-in default config** and works out of the box. To customize (e.g. switch to the more accurate
`wd-eva02-large-tagger-v3`, adjust thresholds), place a `config.yaml` in the `/data` volume to override:

```bash
cp docker/config.docker.yaml /mnt/tank/apps/illustro/config.yaml
```
The in-container paths `/images` and `/data` are hardcoded; usually no need to change them.

## 3. Find the iGPU render group GID

```bash
stat -c '%g' /dev/dri/renderD128      # e.g. 107
```
Update the `group_add` value in `docker-compose.yml` to match.

## 4. Update the marked items in docker-compose.yml

- `/mnt/tank/pictures:/images:ro` -> your illustration dataset (read-only)
- `/mnt/tank/apps/illustro:/data` -> persistent directory (db/vectors/models/thumbnails/optional config)
- Port `8501:8000`, `group_add` GID

Only **2 mounts** needed (/images read-only, /data read-write).

## 5. Start (choose one)

**SSH (most direct):**
```bash
docker compose -f docker/docker-compose.yml up -d
docker logs -f illustro          # Watch initial model download + tagging progress
```

**TrueNAS UI:** Apps -> Discover -> top-right menu -> **Install via YAML** -> paste
the contents of `docker-compose.yml` (image must already be built via `docker build`).

## 6. Usage

Open `http://<NAS_IP>:8501` in your browser.

- On first start, the model is downloaded automatically (~468MB) and the ffdkj Chinese tag translation table (~30MB SQLite, filtered to ~10K WD14 tags) is fetched. Background tagging then begins. ~20k images on N100 takes a few hours (OpenVINO).
- The ffdkj translation table (`tags_ffdkj.json`) is stored in `/data` and persists across restarts. It contains NSFW vocabulary and is **not** committed to the repo. To disable it, set `tags_zh_extra: []` in your config.
- The top-right status bar shows: **Processing Tagging 1234/20000 - Pending N**, with **Pause / Resume / Process now** buttons.
- To stop completely: `docker stop illustro` -- the worker finishes the current batch, flushes to disk, then exits (`stop_grace_period: 60s`). Next start resumes from untagged images.

## About iGPU acceleration

`config.yaml`'s `tagger.openvino_device` defaults to `AUTO`: uses iGPU if available (container can access `/dev/dri`),
otherwise falls back to OpenVINO CPU automatically -- **no crashes**. Force iGPU with `GPU`, force CPU with `CPU`.

If the log shows `[tagger] Execution backend: OpenVINOExecutionProvider (device_type=...)`, OpenVINO is active.
If iGPU isn't being used, verify: compose has `devices: /dev/dri`, `group_add` GID is correct, and `intel-opencl-icd` is installed in the image.
