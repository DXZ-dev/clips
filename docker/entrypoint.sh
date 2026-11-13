#!/bin/sh
# Entrada del contenedor: prepara carpetas, descarga el modelo Whisper la primera vez (si hace falta) y arranca clipper.
set -e
mkdir -p "${CLIPPER_STATE_DIR:-/data}/models" "${CLIPPER_STATE_DIR:-/data}/output_clips" "${CLIPPER_STATE_DIR:-/data}/data"
python -m clipper ensure-models || echo "[clipper] Continuando sin el modelo (puedes usar transcripción por API en Ajustes)."
exec python -m clipper "$@"
