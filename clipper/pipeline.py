"""Orquestación: entrada (buffer o archivo) -> fuente guardada -> EditSpec -> render."""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path

from .buffer import BufferReader
from .config import Config, RectCfg
from .editor import finalize, out_dir, source_path, src_dir
from .ffutil import duration as ffduration, require
from .render import render_spec
from .spec import EditWord, default_spec
from .subtitles import Word

log = logging.getLogger("clipper.pipeline")


def load_words(path: str | Path) -> list[Word]:
    return [Word(float(w["start"]), float(w["end"]), w.get("word") or w["text"])
            for w in json.loads(Path(path).read_text())]


def clip_from_range(cfg: Config, t0: float, t1: float, words_abs: list[Word] | None, reason: str,
                    detail: str = "", name: str | None = None) -> Path:
    """Clip del tramo [t0, t1] (tiempo de stream). `words_abs` con tiempos absolutos de stream.
    Guarda la fuente (con margen a ambos lados) para poder editar el clip después."""
    reader = BufferReader(cfg.buffer.dir, cfg.buffer.segment_seconds, cfg.buffer.window_seconds)
    m = cfg.editor.margin_seconds
    ci = reader.prepare_range(t0 - m, t1 + m)
    try:
        stem = name or f"clip_{time.strftime('%Y%m%d_%H%M%S')}_{reason}"
        tmp = None
        if cfg.editor.keep_sources:
            src_dir(cfg).mkdir(parents=True, exist_ok=True)
            src = source_path(cfg, stem)
        else:
            tmp = tempfile.TemporaryDirectory(prefix="src_")
            src = Path(tmp.name) / f"{stem}.src.mp4"
        subprocess.run([require("ffmpeg"), "-y", "-v", "error", "-nostdin", "-f", "concat", "-safe", "0",
                        "-i", str(ci.workdir / "list.txt"), "-c", "copy", "-movflags", "+faststart", str(src)],
                       check=True, capture_output=True)
        sdur = ffduration(str(src))
        seg0 = ci.seg_start
        words = [EditWord(start=max(0.0, w.start - seg0), end=max(0.0, w.end - seg0), text=w.text)
                 for w in (words_abs or []) if w.text.strip()]
        spec = default_spec(cfg, words, max(0.0, t0 - seg0), min(t1 - seg0, sdur))
        out = out_dir(cfg) / f"{stem}.mp4"
        finalize(cfg, stem, src, spec, out, {"reason": reason, "detail": detail})
        if tmp:
            tmp.cleanup()
        return out
    finally:
        ci.cleanup()


def clip_from_buffer(cfg: Config, seconds: int, words: list[Word] | None = None, reason: str = "manual") -> Path:
    """Los últimos `seconds` del buffer. `words` (si hay) vienen relativos al inicio del clip."""
    b = BufferReader(cfg.buffer.dir, cfg.buffer.segment_seconds, cfg.buffer.window_seconds).bounds()
    if not b:
        raise RuntimeError("El buffer está vacío (¿está corriendo la ingesta?)")
    t0 = max(b[0], b[1] - seconds)
    abs_words = [Word(t0 + w.start, t0 + w.end, w.text) for w in (words or [])]
    return clip_from_range(cfg, t0, b[1], abs_words, reason)


def clip_from_file(cfg: Config, src: str | Path, out: Path | None = None, words: list[Word] | None = None,
                   last_seconds: float | None = None, reason: str = "file", layout: str | None = None,
                   facecam: RectCfg | None = None) -> Path:
    """Procesa un archivo cualquiera (o sus últimos N segundos). `words` relativos al inicio del tramo."""
    src = Path(src).resolve()
    total = ffduration(str(src))
    start = max(0.0, total - last_seconds) if last_seconds else 0.0
    spec = default_spec(cfg, [EditWord(start=w.start + start, end=w.end + start, text=w.text) for w in (words or [])],
                        start, total)
    if layout:
        spec.layout = layout
    if facecam:
        spec.facecam = facecam
    out = out or out_dir(cfg) / f"{src.stem}_9x16.mp4"
    info = render_spec(cfg, src, spec, out)
    meta = {**info, "reason": reason, "source": str(src), "words": len(words or []),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return out
