"""Utilidades ffmpeg/ffprobe y detección de hardware (NVENC)."""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
from dataclasses import dataclass


def require(binary: str) -> str:
    p = shutil.which(binary)
    if not p:
        raise RuntimeError(f"'{binary}' no está instalado (sudo apt install ffmpeg)")
    return p


@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float


def probe(path: str, pre: tuple[str, ...] | list[str] = ()) -> VideoInfo:
    """`pre` = opciones de entrada previas (p. ej. ["-f","concat","-safe","0"])."""
    out = subprocess.run(
        [require("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,avg_frame_rate:format=duration", "-of", "json", *pre, "-i", path],
        capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    s = j["streams"][0]
    n, _, d = s.get("avg_frame_rate", "30/1").partition("/")
    fps = float(n) / float(d or 1) if float(d or 1) else 30.0
    return VideoInfo(int(s["width"]), int(s["height"]), fps or 30.0,
                     float(j.get("format", {}).get("duration") or 0))


def duration(path: str) -> float:
    out = subprocess.run(
        [require("ffprobe"), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


@functools.lru_cache(maxsize=None)
def _encoder_works(name: str) -> bool:
    """Prueba real: que el encoder figure en ffmpeg NO basta (aquí NVENC está compilado sin GPU)."""
    r = subprocess.run(
        [require("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=c=black:s=256x256:d=0.2", "-frames:v", "2", "-c:v", name, "-f", "null", "-"],
        capture_output=True)
    return r.returncode == 0


def pick_encoder(preference: str = "auto") -> str:
    if preference == "libx264":
        return "libx264"
    order = [preference] if preference != "auto" else ["h264_nvenc", "hevc_nvenc"]
    for enc in order:
        if _encoder_works(enc):
            return enc
    return "libx264"


def hw_report() -> dict:
    nvidia = bool(shutil.which("nvidia-smi")) and subprocess.run(
        ["nvidia-smi", "-L"], capture_output=True, text=True).stdout.count("GPU") > 0
    return {
        "ffmpeg": shutil.which("ffmpeg"), "nvidia_gpu": nvidia,
        "h264_nvenc": _encoder_works("h264_nvenc"), "hevc_nvenc": _encoder_works("hevc_nvenc"),
        "encoder": pick_encoder("auto"),
        "streamlink": shutil.which("streamlink"), "yt-dlp": shutil.which("yt-dlp"),
    }
