"""Operaciones del editor de clips: fuente guardada + EditSpec -> previsualización / render final / fotogramas."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import Config, RectCfg
from .ffutil import probe, require
from .render import render_spec
from .spec import EditSpec
from .tracking import detect_facecam


class EditorError(ValueError):
    pass


def out_dir(cfg: Config) -> Path:
    return cfg.path(cfg.clip.output_dir)


def src_dir(cfg: Config) -> Path:
    return out_dir(cfg) / "_src"


def stem_of(name: str) -> str:
    if Path(name).name != name or not name.endswith(".mp4") or name.endswith(".part"):
        raise EditorError("nombre de clip inválido")
    return name[:-4]


def source_path(cfg: Config, stem: str) -> Path:
    """Fuente de un clip: la suya (`<stem>.src.mp4`) o una compartida (vídeos largos: `vod_*.src.mp4`) indicada en su edit.json."""
    ep = src_dir(cfg) / f"{stem}.edit.json"
    if ep.exists():
        try:
            name = json.loads(ep.read_text())["source"]["file"]
            if Path(name).name == name:
                return src_dir(cfg) / name
        except (ValueError, KeyError):
            pass
    return src_dir(cfg) / f"{stem}.src.mp4"


def edit_path(cfg: Config, stem: str) -> Path:
    return src_dir(cfg) / f"{stem}.edit.json"


def transcript_of(spec: EditSpec) -> str:
    end = spec.trim_end if spec.trim_end is not None else 1e9
    return " ".join(w.text for w in spec.words if w.end > spec.trim_start and w.start < end)


def save_edit(cfg: Config, stem: str, spec: EditSpec, src: Path) -> None:
    info = probe(str(src))
    data = {"spec": spec.model_dump(), "source": {"file": src.name, "duration": info.duration,
                                                   "width": info.width, "height": info.height}}
    edit_path(cfg, stem).write_text(json.dumps(data, ensure_ascii=False))


def load_edit(cfg: Config, name: str) -> dict | None:
    stem = stem_of(name)
    p = edit_path(cfg, stem)
    if not p.exists() or not source_path(cfg, stem).exists():
        return None
    return json.loads(p.read_text())


def _check(cfg: Config, name: str, spec: EditSpec) -> tuple[str, Path, dict]:
    stem = stem_of(name)
    data = load_edit(cfg, name)
    if data is None:
        raise EditorError("este clip no tiene fuente guardada (se creó con editor.keep_sources=false o es antiguo)")
    dur = data["source"]["duration"]
    if spec.trim_start >= dur - 1:
        raise EditorError(f"el inicio del recorte está fuera de la fuente ({dur:.1f} s)")
    if spec.trim_end is not None and spec.trim_end > dur + 0.5:
        raise EditorError(f"el final del recorte supera la fuente ({dur:.1f} s)")
    return stem, source_path(cfg, stem), data


def finalize(cfg: Config, stem: str, src: Path, spec: EditSpec, out: Path, extra: dict | None = None) -> dict:
    """Render final + metadatos + guardado de la edición."""
    info = render_spec(cfg, src, spec, out)
    meta = {**(extra or {}), **info, "transcript": transcript_of(spec), "words": len(spec.words),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "editable": cfg.editor.keep_sources}
    out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    if cfg.editor.keep_sources:
        save_edit(cfg, stem, spec, src)
    return meta


def preview(cfg: Config, name: str, spec: EditSpec) -> Path:
    stem, src, _ = _check(cfg, name, spec)
    out = src_dir(cfg) / f"{stem}.preview.mp4"
    render_spec(cfg, src, spec, out, preview=True)
    return out


def apply(cfg: Config, name: str, spec: EditSpec, as_new: bool = False) -> str:
    """Renderiza la edición. as_new=True conserva el clip original y crea «<nombre>_vN.mp4»."""
    stem, src, _ = _check(cfg, name, spec)
    new_stem = stem
    if as_new:
        base = stem.rsplit("_v", 1)[0] if stem.rsplit("_v", 1)[-1].isdigit() else stem
        n = 2
        while (out_dir(cfg) / f"{base}_v{n}.mp4").exists():
            n += 1
        new_stem = f"{base}_v{n}"
        if src.name == f"{stem}.src.mp4":          # fuente propia: la nueva versión recibe su enlace (sin copiar datos)
            new_src = source_path(cfg, new_stem)
            try:
                os.link(src, new_src)
            except OSError:
                shutil.copy2(src, new_src)
            src = new_src
        # fuente compartida: se reutiliza tal cual
    old = out_dir(cfg) / f"{stem}.json"
    extra = {}
    if old.exists():
        try:
            m = json.loads(old.read_text())
            extra = {k: m.get(k) for k in ("reason", "detail") if m.get(k) is not None}
        except ValueError:
            pass
    finalize(cfg, new_stem, src, spec, out_dir(cfg) / f"{new_stem}.mp4", {**extra, "edited": True})
    return f"{new_stem}.mp4"


def suggest_facecam(cfg: Config, name: str, spec: EditSpec) -> RectCfg | None:
    stem, src, data = _check(cfg, name, spec)
    info = probe(str(src))
    return detect_facecam(["-ss", f"{spec.trim_start:.3f}", "-i", str(src)], info, cfg)


def frame_jpeg(cfg: Config, name: str, t: float, width: int = 960) -> bytes:
    stem = stem_of(name)
    src = source_path(cfg, stem)
    if not src.exists():
        raise EditorError("sin fuente guardada")
    r = subprocess.run([require("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", str(src),
                        "-frames:v", "1", "-vf", f"scale={int(width)}:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise EditorError("no se pudo extraer el fotograma")
    return r.stdout


def remove_clip_files(cfg: Config, stem: str) -> None:
    """Borra el clip y sus archivos. Una fuente compartida solo se borra cuando ya ningún clip la usa."""
    o = out_dir(cfg)
    src = source_path(cfg, stem)
    (o / f"{stem}.mp4").unlink(missing_ok=True)
    (o / f"{stem}.json").unlink(missing_ok=True)
    for f in src_dir(cfg).glob(f"{stem}.*"):
        if f.suffix in (".mp4", ".json"):
            f.unlink(missing_ok=True)
    if src.name != f"{stem}.src.mp4" and src.exists():
        still = any(src.name in e.read_text() for e in src_dir(cfg).glob("*.edit.json"))
        if not still:
            src.unlink(missing_ok=True)
