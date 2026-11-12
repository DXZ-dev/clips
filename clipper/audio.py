"""Extracción de audio y envolvente de energía (reutilizable por la Fase 2)."""
from __future__ import annotations

import subprocess

import numpy as np

from .ffutil import require

SR = 16000


def load_audio(input_args: list[str], sr: int = SR) -> np.ndarray:
    """Devuelve el audio como float32 mono [-1, 1]. Vacío si no hay pista de audio."""
    cmd = [require("ffmpeg"), "-v", "error", "-nostdin", *input_args, "-vn", "-ac", "1", "-ar", str(sr),
           "-f", "s16le", "pipe:1"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def envelope_db(audio: np.ndarray, fps: float, n: int, sr: int = SR) -> np.ndarray:
    """Nivel RMS en dBFS por paso de análisis (n pasos a `fps`). Sin audio => -120 dB."""
    out = np.full(n, -120.0, dtype=np.float32)
    if audio.size == 0:
        return out
    hop = sr / fps
    for i in range(n):
        seg = audio[int(i * hop): int((i + 1) * hop)]
        if seg.size:
            out[i] = 20 * np.log10(max(float(np.sqrt(np.mean(seg ** 2))), 1e-6))
    return out
