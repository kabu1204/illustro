# illustro container image (N100 / OpenVINO)
# Build from project root: docker build -t illustro:latest -f docker/Dockerfile .
FROM python:3.11-slim-bookworm

# System dependencies (all deps ship as wheels, no compiler needed; skipping build-essential avoids -march=native cross-CPU issues):
#  - libgomp1        : OpenMP runtime required by onnxruntime
#  - ocl-icd-libopencl1 : OpenCL loader (needed for OpenVINO iGPU)
#  - intel-opencl-icd: Intel iGPU compute driver (NEO), best-effort; falls back to OpenVINO CPU if unavailable
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 ocl-icd-libopencl1 \
    && (apt-get install -y --no-install-recommends intel-opencl-icd \
        || echo "[warn] intel-opencl-icd not available, iGPU acceleration will fall back to OpenVINO CPU") \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY docker/requirements.docker.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application code (config.yaml is mounted, so config changes don't require rebuilding the image)
COPY illustro /app/illustro
COPY server /app/server
COPY illustro/data /app/illustro/data
COPY docker/config.docker.yaml /app/config.yaml
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Single container: prepare model first, then run web + background worker in one process
CMD ["/app/entrypoint.sh"]
