"""Render final con ffmpeg: recorte con cámara virtual -> 1080x1920 -> subtítulos ASS quemados."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .config import Config
from .ffutil import pick_encoder, require
from .tracking import CropPath

log = logging.getLogger("clipper.render")


def _encoder_args(enc: str, cfg: Config) -> list[str]:
    r = cfg.render
    if enc in ("h264_nvenc", "hevc_nvenc"):
        a = ["-c:v", enc, "-preset", "p4", "-rc", "vbr", "-cq", str(r.nvenc_cq), "-b:v", "0"]
        return a + (["-tag:v", "hvc1"] if enc == "hevc_nvenc" else [])
    return ["-c:v", "libx264", "-preset", r.preset_cpu, "-crf", str(r.crf)]


def render_clip(input_args: list[str], out_path: Path, path: CropPath, cfg: Config,
                ass_text: str | None, workdir: Path) -> str:
    """Devuelve el encoder usado. Si NVENC falla, reintenta con libx264 automáticamente."""
    g, rf = path.geom, cfg.reframe
    workdir.mkdir(parents=True, exist_ok=True)
    chain = (f"[0:v]crop=w={g.crop_w}:h={g.crop_h}:x='{path.x_expr()}':y={g.y},"
             f"scale={rf.width}:{rf.height}:flags=lanczos,setsar=1")
    if ass_text:
        (workdir / "subs.ass").write_text(ass_text, encoding="utf-8")
        chain += ",ass=subs.ass"          # ruta relativa + cwd=workdir evita escapes
    chain += "[v]"

    enc = pick_encoder(cfg.render.encoder, cfg.compute.device)
    for attempt in (enc, "libx264"):
        cmd = [require("ffmpeg"), "-y", "-v", "error", "-nostdin", *input_args,
               "-filter_complex", chain, "-map", "[v]", "-map", "0:a:0?",
               *_encoder_args(attempt, cfg), "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", cfg.render.audio_bitrate, "-movflags", "+faststart",
               str(out_path.resolve())]
        r = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
        if r.returncode == 0:
            return attempt
        log.warning("ffmpeg falló con %s: %s", attempt, r.stderr.strip()[-400:])
        if attempt == "libx264":
            raise RuntimeError(f"Render falló: {r.stderr.strip()[-800:]}")
        log.warning("Reintentando con libx264")
    raise AssertionError
