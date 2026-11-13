"""Descarga explícita de modelos (única operación con red; el resto funciona sin conexión)."""
from __future__ import annotations

import os
from pathlib import Path

from .config import Config


def whisper_dir(cfg: Config) -> Path:
    return cfg.path(f"models/whisper-{cfg.transcribe.model}")


def ensure_whisper(cfg: Config) -> str:
    """Devuelve 'skip' (nada que hacer), 'present' o 'downloaded'. Lanza excepción si la descarga falla."""
    tc = cfg.transcribe
    if not tc.enabled or tc.provider != "local":
        return "skip"
    if os.environ.get("CLIPPER_AUTO_DOWNLOAD", "1") == "0" or Path(tc.model).expanduser().is_dir():
        return "skip" if os.environ.get("CLIPPER_AUTO_DOWNLOAD", "1") == "0" else "present"
    dest = whisper_dir(cfg)
    if dest.is_dir() and any(dest.iterdir()):
        return "present"
    from faster_whisper.utils import download_model
    dest.parent.mkdir(parents=True, exist_ok=True)
    download_model(tc.model, output_dir=str(dest))
    return "downloaded"
