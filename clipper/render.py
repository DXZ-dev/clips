"""Render final con ffmpeg a partir de un EditSpec: diseños auto / split (cámara + gameplay) / fit, ASS quemado."""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from .config import Config, RectCfg
from .ffutil import VideoInfo, pick_encoder, probe, require
from .spec import EditSpec
from .subtitles import Word, build_ass, with_preset
from .tracking import analyze, crop_geometry, detect_facecam, face_center_in

log = logging.getLogger("clipper.render")


def _even(v: float) -> int:
    return int(v) // 2 * 2


def _encoder_args(enc: str, cfg: Config, preview: bool) -> list[str]:
    r = cfg.render
    if preview:
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30"]
    if enc in ("h264_nvenc", "hevc_nvenc"):
        a = ["-c:v", enc, "-preset", "p4", "-rc", "vbr", "-cq", str(r.nvenc_cq), "-b:v", "0"]
        return a + (["-tag:v", "hvc1"] if enc == "hevc_nvenc" else [])
    return ["-c:v", "libx264", "-preset", r.preset_cpu, "-crf", str(r.crf)]


def _contain(src: str, dst: str, w: int, h: int, blur: int) -> str:
    """Fotograma completo centrado en un área w×h sobre su propio fondo desenfocado."""
    bw, bh = max(2, _even(w / 4)), max(2, _even(h / 4))
    return (f"{src}split=2[bgi][fgi];"
            f"[bgi]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur={max(1, blur // 4)}:2,scale={w}:{h},eq=brightness=-0.08[bgo];"
            f"[fgi]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fgo];"
            f"[bgo][fgo]overlay=(W-w)/2:(H-h)/2{dst}")


def _cam_crop(info: VideoInfo, rect: RectCfg, face: tuple[float, float] | None, w: int, h: int) -> tuple[int, int, int, int]:
    """Ventana (px, py, pw, ph) dentro de la cámara con proporción w:h, centrada en el rostro si se conoce."""
    rx, ry = int(rect.x * info.width), int(rect.y * info.height)
    rw, rh = max(2, _even(rect.w * info.width)), max(2, _even(rect.h * info.height))
    target = w / h
    if rw / rh > target:                         # la cámara es más ancha que el hueco: recorta ancho
        pw, ph = _even(rh * target), rh
    else:                                        # más alta: recorta alto
        pw, ph = rw, _even(rw / target)
    fx, fy = face if face else (0.5, 0.5)
    px = rx + min(max(fx * rw - pw / 2, 0), rw - pw)
    py = ry + min(max(fy * rh - ph * 0.45, 0), rh - ph)   # el rostro algo por encima del centro
    return _even(px), _even(py), pw, ph


def build_graph(cfg: Config, spec: EditSpec, info: VideoInfo, input_args: list[str]) -> tuple[str, int | None, dict]:
    """Devuelve (filtergraph terminado en [v0], y de la unión cámara/gameplay o None, info de lo decidido)."""
    W, H = cfg.reframe.width, cfg.reframe.height
    note: dict = {"layout": spec.layout}
    rect = None
    if spec.layout == "split":
        rect = spec.facecam or detect_facecam(input_args, info, cfg)
        if rect is None:
            note.update(layout="auto", warning="no se encontró la cámara: se usó el diseño automático")
        else:
            note["facecam"] = rect.model_dump()

    if note["layout"] == "fit":
        return _contain("[0:v]", "[v0]", W, H, cfg.layout.blur_strength), None, note

    if note["layout"] == "split":
        cam_h = _even(H * spec.facecam_height_pct / 100)
        game_h = H - cam_h
        face = face_center_in(input_args, info, cfg, rect)
        px, py, pw, ph = _cam_crop(info, rect, face, W, cam_h)
        cam = f"[s1]crop={pw}:{ph}:{px}:{py},scale={W}:{cam_h}:flags=lanczos[cam]"
        if spec.gameplay == "cover":
            path = analyze(input_args, info, cfg, out_w=W, out_h=game_h, ignore=rect, faces=False)
            g = path.geom
            game = (f"[s2]crop=w={g.crop_w}:h={g.crop_h}:x='{path.x_expr()}':y={g.y},"
                    f"scale={W}:{game_h}:flags=lanczos[game]")
        else:
            game = _contain("[s2]", "[game]", W, game_h, cfg.layout.blur_strength)
        order = "[cam][game]" if spec.facecam_position == "top" else "[game][cam]"
        graph = f"[0:v]split=2[s1][s2];{cam};{game};{order}vstack=inputs=2[v0]"
        note.update(face_in_cam=bool(face))
        return graph, (cam_h if spec.facecam_position == "top" else game_h), note

    path = analyze(input_args, info, cfg)           # auto: sigue al hablante / acción
    g = path.geom
    graph = (f"[0:v]crop=w={g.crop_w}:h={g.crop_h}:x='{path.x_expr()}':y={g.y},"
             f"scale={W}:{H}:flags=lanczos,setsar=1[v0]")
    return graph, None, note


def render_spec(cfg: Config, src: Path, spec: EditSpec, out_path: Path, preview: bool = False) -> dict:
    """Renderiza `src` según `spec` a out_path (escritura atómica). Devuelve metadatos del render."""
    src = Path(src).resolve()                       # el render corre con otro cwd
    info = probe(str(src))
    end = min(spec.trim_end if spec.trim_end is not None else info.duration or 1e9, info.duration or 1e9)
    dur = max(1.0, end - spec.trim_start)
    input_args = ["-ss", f"{spec.trim_start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
    graph, seam_y, note = build_graph(cfg, spec, info, input_args)

    # subtítulos + gancho + marca de agua en un único .ass
    words = []
    for w in spec.words:
        s, e = w.start - spec.trim_start, w.end - spec.trim_start
        if e > 0 and s < dur and w.text.strip():
            words.append(Word(max(0.0, s), min(dur, e), w.text))
    st = spec.subtitles
    pos = st.position or ("seam" if note["layout"] == "split" else None)
    sub_cfg = with_preset(cfg.subtitles, st.preset, highlight_color=st.highlight_color,
                          primary_color=st.primary_color, font_size=st.font_size, position=pos, uppercase=st.uppercase)
    hook = (spec.hook.text, spec.hook.seconds) if spec.hook.text.strip() else None
    mark = (spec.watermark.text, spec.watermark.corner) if spec.watermark.text.strip() else None
    ass = build_ass(words if st.enabled else [], sub_cfg, cfg.reframe.width, cfg.reframe.height,
                    seam_y=seam_y, hook=hook, watermark=mark, duration=dur)
    has_ass = (st.enabled and words) or hook or mark
    graph += (";[v0]ass=subs.ass[v1]" if has_ass else ";[v0]null[v1]")
    graph += ";[v1]scale=540:960[v]" if preview else ";[v1]null[v]"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    part = out_path.resolve().with_name(out_path.name + ".part")   # el clip no existe hasta estar completo
    import tempfile
    with tempfile.TemporaryDirectory(prefix="render_") as wd:
        if has_ass:
            (Path(wd) / "subs.ass").write_text(ass, encoding="utf-8")   # ruta relativa + cwd evita escapes
        enc = "libx264" if preview else pick_encoder(cfg.render.encoder, cfg.compute.device)
        for attempt in dict.fromkeys((enc, "libx264")):
            cmd = [require("ffmpeg"), "-y", "-v", "error", "-nostdin", *input_args,
                   "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
                   *_encoder_args(attempt, cfg, preview), "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "96k" if preview else cfg.render.audio_bitrate,
                   "-movflags", "+faststart", "-f", "mp4", str(part)]
            r = subprocess.run(cmd, cwd=wd, capture_output=True, text=True)
            if r.returncode == 0:
                os.replace(part, out_path.resolve())
                return {**note, "encoder": attempt, "duration": round(dur, 2), "source_resolution": f"{info.width}x{info.height}"}
            part.unlink(missing_ok=True)
            log.warning("ffmpeg falló con %s: %s", attempt, r.stderr.strip()[-400:])
            if attempt == "libx264":
                raise RuntimeError(f"Render falló: {r.stderr.strip()[-800:]}")
            log.warning("Reintentando con libx264")
    raise AssertionError
