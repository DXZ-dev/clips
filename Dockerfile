# syntax=docker/dockerfile:1
# clipper — imagen para CPU (por defecto) o GPU NVIDIA:
#   docker build -t clipper .                       # CPU
#   docker build --build-arg GPU=1 -t clipper:gpu .  # + librerías CUDA/cuDNN para Whisper en GPU
# Base Ubuntu 24.04: la misma plataforma (ffmpeg 6.1, Python 3.12) sobre la que se ha probado el proyecto.
FROM ubuntu:24.04

ARG GPU=0
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    CLIPPER_STATE_DIR=/data \
    CLIPPER__SERVER__HOST=0.0.0.0 \
    LD_LIBRARY_PATH=/opt/venv/lib/python3.12/site-packages/nvidia/cublas/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg python3 python3-venv libgl1 libegl1 libglib2.0-0t64 \
        fonts-dejavu-core curl ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv

WORKDIR /app
COPY requirements.txt .
# torch solo se usa para silero-vad: la versión CPU basta (~200 MB en vez de ~5 GB con CUDA)
RUN (pip install torch --index-url https://download.pytorch.org/whl/cpu || pip install torch) \
 && pip install -r requirements.txt \
 && if [ "$GPU" = "1" ]; then pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"; fi

COPY . .
# Detector de rostros (se baja una vez, al construir la imagen)
RUN mkdir -p models && curl -fsSL --retry 3 -o models/blaze_face_short_range.tflite \
    https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite

# La imagen de Ubuntu trae un usuario "ubuntu" con UID 1000: lo sustituimos para que 1000 sea clipper
RUN (userdel -r ubuntu 2>/dev/null || true) && useradd -u 1000 -m clipper \
 && mkdir -p /data && chown -R clipper:clipper /data /app
USER clipper
VOLUME /data
EXPOSE 7848 1935

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS "http://localhost:${CLIPPER_PORT:-7848}/api/status" -o /dev/null || exit 1

ENTRYPOINT ["tini", "--", "/app/docker/entrypoint.sh"]
CMD ["serve"]
