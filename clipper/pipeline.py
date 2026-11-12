"""Orquestación: entrada (buffer o archivo) -> seguimiento -> subtítulos -> render."""
from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path

from .buffer import BufferReader, ClipInput
from .config import Config
from .ffutil import duration as ffduration, probe
from .render import render_clip
from .subtitles import Word, build_ass
from .tracking import analyze

log = logging.getLogger("clipper.pipeline")


def load_words(path: str | Path) -> list[Word]:
    return [Word(float(w["start"]), float(w["end"]), w.get("word") or w["text"])
            for w in json.loads(Path(path).read_text())]


def _run(cfg: Config, input_args: list[str], pre: list[str], probe_target: str, out: Path,
         words: list[Word] | None, meta: dict) -> Path:
    t0 = time.time()
    info = probe(probe_target, pre)
    out.parent.mkdir(parents=True, exist_ok=True)
    path = analyze(input_args, info, cfg)
    t1 = time.time()
    ass = build_ass(words, cfg.subtitles, cfg.reframe.width, cfg.reframe.height) \
        if (words and cfg.subtitles.enabled) else None
    with tempfile.TemporaryDirectory(prefix="render_") as wd:
        enc = render_clip(input_args, out, path, cfg, ass, Path(wd))
    t2 = time.time()
    meta.update(encoder=enc, source_resolution=f"{info.width}x{info.height}",
                analysis_seconds=round(t1 - t0, 2), render_seconds=round(t2 - t1, 2),
                created=time.strftime("%Y-%m-%dT%H:%M:%S"), words=len(words or []))
    out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    log.info("Clip %s (%s) análisis %.1fs, render %.1fs", out.name, enc, t1 - t0, t2 - t1)
    return out


def clip_from_buffer(cfg: Config, seconds: int, words: list[Word] | None = None,
                     reason: str = "manual", name: str | None = None) -> Path:
    reader = BufferReader(cfg.buffer.dir, cfg.buffer.segment_seconds, cfg.buffer.window_seconds)
    ci: ClipInput = reader.prepare(seconds)
    try:
        list_txt = ci.workdir / "list.txt"
        out = cfg.path(cfg.clip.output_dir) / f"{name or 'clip_' + time.strftime('%Y%m%d_%H%M%S')}.mp4"
        return _run(cfg, ci.args, ["-f", "concat", "-safe", "0"], str(list_txt), out, words,
                    {"reason": reason, "requested_seconds": seconds, "duration": round(ci.duration, 2)})
    finally:
        ci.cleanup()


def clip_from_file(cfg: Config, src: str | Path, out: Path | None = None, words: list[Word] | None = None,
                   last_seconds: float | None = None, reason: str = "file") -> Path:
    """Procesa un archivo cualquiera (o sus últimos N segundos). Útil para pruebas y uso offline."""
    src = str(Path(src).resolve())   # el render corre con otro cwd
    total = ffduration(src)
    args: list[str] = []
    if last_seconds and total > last_seconds:
        args += ["-ss", f"{total - last_seconds:.3f}"]
    args += ["-i", src]
    out = out or cfg.path(cfg.clip.output_dir) / f"{Path(src).stem}_9x16.mp4"
    return _run(cfg, args, [], src, out, words, {"reason": reason, "source": src,
                                                  "duration": round(min(total, last_seconds or total), 2)})
