#!/usr/bin/env bash
# Verifica dependencias del sistema para clipper (Debian/Ubuntu).
ok(){ printf "  [ok] %s\n" "$1"; }; ko(){ printf "  [--] %s\n" "$1"; }
echo "== Sistema"; . /etc/os-release; echo "  $PRETTY_NAME"
echo "== Binarios"
for b in ffmpeg ffprobe python3 streamlink yt-dlp; do command -v $b >/dev/null && ok "$b: $(command -v $b)" || ko "$b no instalado"; done
echo "== Python"; python3 --version
echo "== GPU"
if command -v nvidia-smi >/dev/null && nvidia-smi -L 2>/dev/null | grep -q GPU; then ok "$(nvidia-smi -L | head -1)"; else ko "sin GPU NVIDIA utilizable (se usará libx264)"; fi
command -v nvcc >/dev/null && ok "CUDA: $(nvcc --version | tail -1)" || ko "nvcc no encontrado (no es necesario para NVENC; sí para faster-whisper en GPU)"
echo "== Instalación sugerida (Debian/Ubuntu)"
echo "  sudo apt install -y ffmpeg python3-venv fonts-dejavu-core"
echo "  python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
