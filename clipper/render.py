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
from .tracking import analyze, crop_geometry, detect_facecam, face_center_in, pl_expr, track_face_in

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


def _cam_crop(info: VideoInfo, rect: RectCfg, face: tuple[float, float] | None, w: int, h: int, zoom: float = 1.0) -> tuple[int, int, int, int]:
    """Ventana (px, py, pw, ph) dentro de la cámara con proporción w:h, centrada en el rostro si se conoce. `zoom` > 1 la reduce."""
    rx, ry = int(rect.x * info.width), int(rect.y * info.height)
    rw, rh = max(2, _even(rect.w * info.width)), max(2, _even(rect.h * info.height))
    target = w / h
    if rw / rh > target:                         # la cámara es más ancha que el hueco: recorta ancho
        pw, ph = _even(rh * target), rh
    else:                                        # más alta: recorta alto
        pw, ph = rw, _even(rw / target)
    pw, ph = max(2, _even(pw / zoom)), max(2, _even(ph / zoom))
    fx, fy = face if face else (0.5, 0.5)
    px = rx + min(max(fx * rw - pw / 2, 0), rw - pw)
    py = ry + min(max(fy * rh - ph * 0.45, 0), rh - ph)   # el rostro algo por encima del centro
    return _even(px), _even(py), pw, ph


def _zone_crop(info: VideoInfo, zone: RectCfg, out_w: int, out_h: int) -> tuple[int, int, int, int]:
    """Recorte (cw, ch, x, y) de proporción out_w:out_h centrado DENTRO de la zona marcada a mano."""
    zx, zy = zone.x * info.width, zone.y * info.height
    zw, zh = max(2.0, zone.w * info.width), max(2.0, zone.h * info.height)
    aspect = out_w / out_h
    if zw / zh > aspect:
        ch, cw = zh, zh * aspect
    else:
        cw, ch = zw, zw / aspect
    return _even(cw), _even(ch), _even(zx + (zw - cw) / 2), _even(zy + (zh - ch) / 2)


def _center_crop(info: VideoInfo, rect: RectCfg | None, out_w: int, out_h: int, focus_x: float | None = None,
                 zone: RectCfg | None = None) -> tuple[int, int, int, int]:
    """Recorte (cw, ch, x, y) de proporción out_w:out_h centrado en el fotograma. Si se conoce la cámara superpuesta, el recorte
    se desplaza lo justo para no incluirla (en el resultado la cámara ya va arriba: no debe aparecer dos veces)."""
    if zone is not None:
        return _zone_crop(info, zone, out_w, out_h)
    aspect = out_w / out_h
    if info.height * aspect <= info.width:
        cw, ch = _even(info.height * aspect), _even(info.height)
    else:
        cw = _even(info.width)
        ch = min(info.height, _even(cw / aspect))
    cx = focus_x * info.width if focus_x is not None else info.width / 2
    if focus_x is None and rect is not None:
        cam0, cam1 = rect.x * info.width, (rect.x + rect.w) * info.width
        if cx - cw / 2 < cam1 and cx + cw / 2 > cam0:                 # el recorte pisaría la cámara
            cx = cam1 + cw / 2 if (cam0 + cam1) / 2 < info.width / 2 else cam0 - cw / 2
    x = min(max(cx - cw / 2, 0), info.width - cw)
    return cw, ch, _even(x), _even((info.height - ch) / 2)


def _round_expr(r: int) -> str:
    """Expresión de geq: 255 dentro de un rectángulo con esquinas de radio r, 0 fuera (máscara de la tarjeta)."""
    return (f"if(gt(abs(X-W/2),W/2-{r})*gt(abs(Y-H/2),H/2-{r}),if(lte(hypot(abs(X-W/2)-(W/2-{r}),abs(Y-H/2)-(H/2-{r})),{r}),255,0),255)")


def build_graph(cfg: Config, spec: EditSpec, info: VideoInfo, input_args: list[str], static: bool = False,
                cache: dict | None = None, panels: str | None = None) -> tuple[str, int | None, dict]:
    """Devuelve (filtergraph terminado en [v0], y de la unión cámara/gameplay o None, info de lo decidido)."""
    W, H = cfg.reframe.width, cfg.reframe.height
    note: dict = {"layout": spec.layout}
    rect = None
    cache = cache if cache is not None else {}
    if spec.layout == "split":
        if "rect" not in cache:
            cache["rect"] = spec.facecam or detect_facecam(input_args, info, cfg)
        rect = cache["rect"]
        if rect is None:
            note.update(layout="auto", warning="no se encontró la cámara: se usó el diseño automático")
        else:
            note["facecam"] = rect.model_dump()

    if note["layout"] == "fit":
        return _contain("[0:v]", "[v0]", W, H, cfg.layout.blur_strength), None, note

    if note["layout"] == "split":
        pmode = panels or spec.panels or cfg.layout.panels
        overlay, cards = pmode == "overlay", pmode == "cards"
        m, g_, rad = (cfg.layout.panel_margin, cfg.layout.panel_gap, cfg.layout.panel_radius) if cards else (0, 0, 0)
        PW = W - 2 * m                                   # ancho de cada pantalla
        content = H - 2 * m - g_
        cam_h = _even(content * spec.facecam_height_pct / 100)
        game_h = content - cam_h
        exact = cfg.layout.exact_zones and spec.gameplay == "center" and spec.gameplay_zone is not None
        if exact:                                        # la zona marcada se ve entera: su proporción manda sobre la altura
            z = spec.gameplay_zone
            want = _even(PW * (z.h * info.height) / max(2.0, z.w * info.width))
            game_h = min(max(want, _even(content * 0.30)), _even(content * 0.72))
            cam_h = content - game_h
        ob = None
        if overlay:                                      # pantalla 1 = fondo a lienzo completo; pantalla 2 = cámara encima, en la caja que tú arrastras
            cb = spec.cam_box or cfg.layout.cam_box or RectCfg(x=0.05, y=0.04, w=0.90, h=0.30)
            bw_, bh_ = max(64, _even(cb.w * W)), max(64, _even(cb.h * H))
            ob = (min(_even(cb.x * W), W - bw_), min(_even(cb.y * H), H - bh_))
            PW, cam_h = bw_, bh_
        zoom = spec.cam_zoom if spec.cam_follow else 1.0
        if spec.cam_follow and "facepath" not in cache:
            cache["facepath"] = track_face_in(input_args, info, cfg, rect)         # seguimiento real, fotograma a fotograma
        fp = cache.get("facepath")
        if fp is not None:
            _, _, pw, ph = _cam_crop(info, rect, (0.5, 0.5), PW, cam_h, zoom)
            rx, ry = int(rect.x * info.width), int(rect.y * info.height)
            rw, rh = max(2, _even(rect.w * info.width)), max(2, _even(rect.h * info.height))
            q = lambda v: round(v / 2) * 2                                           # a pasos de 2 px: sin microtemblores  # noqa: E731
            xs = [q(rx + min(max(f * rw - pw / 2, 0), rw - pw)) for f in fp.fx]
            ys = [q(ry + min(max(f * rh - ph * 0.45, 0), rh - ph)) for f in fp.fy]
            if static:                                                              # respaldo sin expresiones: posición mediana
                xe, ye = f"{sorted(xs)[len(xs) // 2]:.2f}", f"{sorted(ys)[len(ys) // 2]:.2f}"
            else:
                xe, ye = pl_expr(xs, fp.fps), pl_expr(ys, fp.fps)
            cam = f"[s1]crop={pw}:{ph}:x='{xe}':y='{ye}',scale={PW}:{cam_h}:flags=lanczos[cam]"
            face = (0.5, 0.5)
        else:
            if "face" not in cache:
                cache["face"] = face_center_in(input_args, info, cfg, rect)
            face = cache["face"]
            px, py, pw, ph = _cam_crop(info, rect, face, PW, cam_h, zoom)
            cam = f"[s1]crop={pw}:{ph}:{px}:{py},scale={PW}:{cam_h}:flags=lanczos[cam]"
        if overlay:
            if spec.gameplay_zone is not None:
                cw, ch, gx, gy = _zone_crop(info, spec.gameplay_zone, W, H)
            else:
                cw, ch, gx, gy = _center_crop(info, rect, W, H, spec.gameplay_x, None)
            game = f"[s2]crop={cw}:{ch}:{gx}:{gy},scale={W}:{H}:flags=lanczos[game]"
        elif spec.gameplay == "center":
            cw, ch, gx, gy = _center_crop(info, rect, PW, game_h, spec.gameplay_x, spec.gameplay_zone)
            game = f"[s2]crop={cw}:{ch}:{gx}:{gy},scale={PW}:{game_h}:flags=lanczos[game]"
        elif spec.gameplay == "cover":
            if "game_path" not in cache:
                cache["game_path"] = analyze(input_args, info, cfg, out_w=PW, out_h=game_h, ignore=rect, faces=False)
            path = cache["game_path"]
            g = path.geom
            game = (f"[s2]crop=w={g.crop_w}:h={g.crop_h}:x='{path.static_x() if static else path.x_expr()}':y={g.y},"
                    f"scale={PW}:{game_h}:flags=lanczos[game]")
        elif spec.gameplay_zone is not None:                       # «completo»: se muestra entera la zona marcada, con fondo desenfocado
            zc = _zone_crop(info, spec.gameplay_zone, spec.gameplay_zone.w * info.width, spec.gameplay_zone.h * info.height)
            game = f"[s2]crop={zc[0]}:{zc[1]}:{zc[2]}:{zc[3]}[zc];" + _contain("[zc]", "[game]", PW, game_h, cfg.layout.blur_strength)
        else:
            game = _contain("[s2]", "[game]", PW, game_h, cfg.layout.blur_strength)
        note.update(face_in_cam=bool(face), panels="overlay" if overlay else "cards" if cards else "flush")
        if overlay:
            mk = f"color=c=white:s={PW}x{cam_h}:r=25:d=0.04,format=gray,geq=lum='{_round_expr(cfg.layout.panel_radius)}',loop=loop=-1:size=1[mk]"
            graph = (f"[0:v]split=2[s1][s2];{cam};{game};{mk};[cam]format=yuva420p[camf];[camf][mk]alphamerge[camr];"
                     f"[game][camr]overlay={ob[0]}:{ob[1]}[v0]")
            return graph, None, note
        if cards:                                          # dos pantallas independientes sobre un fondo desenfocado del propio directo
            top_h, bot_h = (cam_h, game_h) if spec.facecam_position == "top" else (game_h, cam_h)
            first, second = ("cam", "game") if spec.facecam_position == "top" else ("game", "cam")
            bw, bh = _even(W / 4), _even(H / 4)
            bg = (f"[s3]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=6:2,scale={W}:{H},"
                  f"eq=brightness=-0.28:saturation=0.9[bg]")
            mk = lambda lab, h_: (f"color=c=white:s={PW}x{h_}:r=25:d=0.04,format=gray,geq=lum='{_round_expr(rad)}',loop=loop=-1:size=1[{lab}]")  # noqa: E731
            rnd = lambda f, k: f"[{f}]format=yuva420p[{f}a];[{f}a][{k}]alphamerge[{f}r]"  # noqa: E731
            graph = (f"[0:v]split=3[s1][s2][s3];{cam};{game};{bg};{mk('mk1', top_h)};{mk('mk2', bot_h)};{rnd(first, 'mk1')};{rnd(second, 'mk2')};"
                     f"[bg][{first}r]overlay={m}:{m}[t1];[t1][{second}r]overlay={m}:{m + top_h + g_}[v0]")
            return graph, m + top_h + g_ // 2, note
        order = "[cam][game]" if spec.facecam_position == "top" else "[game][cam]"
        graph = f"[0:v]split=2[s1][s2];{cam};{game};{order}vstack=inputs=2[v0]"
        return graph, (cam_h if spec.facecam_position == "top" else game_h), note

    if "path" not in cache:
        cache["path"] = analyze(input_args, info, cfg, zone=spec.gameplay_zone)   # auto: sigue al hablante / acción (dentro de la zona)
    path = cache["path"]
    g = path.geom
    graph = (f"[0:v]crop=w={g.crop_w}:h={g.crop_h}:x='{path.static_x() if static else path.x_expr()}':y={g.y},"
             f"scale={W}:{H}:flags=lanczos,setsar=1[v0]")
    return graph, None, note


def _audio_filter(cfg: Config, dur: float, preview: bool) -> str | None:
    if preview:
        return None
    cc, f = cfg.clip, []
    if cc.normalize_audio:
        f.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    if cc.fade_seconds > 0 and dur > 4 * cc.fade_seconds:
        f += [f"afade=t=in:st=0:d={cc.fade_seconds}", f"afade=t=out:st={dur - cc.fade_seconds:.3f}:d={cc.fade_seconds}"]
    return ",".join(f) or None


def _run_ffmpeg(cfg: Config, input_args: list[str], graph: str, part: Path, cwd: str, preview: bool, dur_out: float = 36000.0,
                afilter: str | None = None) -> tuple[str | None, str]:
    """Ejecuta ffmpeg (NVENC con respaldo a libx264). Devuelve (encoder usado | None, stderr del último intento)."""
    enc = "libx264" if preview else pick_encoder(cfg.render.encoder, cfg.compute.device)
    err = ""
    for attempt in dict.fromkeys((enc, "libx264")):
        cmd = [require("ffmpeg"), "-y", "-v", "error", "-nostdin", *input_args,
               "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
               *_encoder_args(attempt, cfg, preview), "-pix_fmt", "yuv420p",
               *(["-af", afilter] if afilter else []), "-c:a", "aac", "-b:a", "96k" if preview else cfg.render.audio_bitrate,
               "-movflags", "+faststart", "-t", f"{dur_out:.3f}", "-f", "mp4", str(part)]
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        if r.returncode == 0:
            return attempt, ""
        part.unlink(missing_ok=True)
        err = r.stderr.strip()
        log.warning("ffmpeg falló con %s: %s", attempt, err[-300:])
    return None, err


def render_spec(cfg: Config, src: Path, spec: EditSpec, out_path: Path, preview: bool = False) -> dict:
    """Renderiza `src` según `spec` a out_path (escritura atómica). Devuelve metadatos del render.

    Red de seguridad: si el recorte móvil falla se reintenta con recorte fijo y, si también falla, con el diseño «completo»;
    un clip no debería perderse por una rareza de una versión de ffmpeg. El recorte se ajusta si cae fuera del vídeo."""
    import tempfile
    src = Path(src).resolve()                       # el render corre con otro cwd
    info = probe(str(src))
    total = info.duration or 0.0
    want = (spec.trim_end - spec.trim_start) if spec.trim_end is not None else None
    start = spec.trim_start
    adjusted = None
    if total and start >= total - 0.5:              # el recorte empieza fuera del vídeo guardado: se usa el final
        start = max(0.0, total - (want or 30.0))
        adjusted = f"el inicio ({spec.trim_start:.1f} s) caía fuera de la fuente ({total:.1f} s)"
    end = min(spec.trim_end if spec.trim_end is not None else (total or 1e9), total or 1e9)
    if end - start < 1.0:
        end = min(total or 1e9, start + (want or 30.0))
    dur = max(1.0, end - start)
    input_args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]

    words = []
    for w in spec.words:
        s0, e0 = w.start - start, w.end - start
        if e0 > 0 and s0 < dur and w.text.strip():
            words.append(Word(max(0.0, s0), min(dur, e0), w.text))
    st = spec.subtitles
    hook = (spec.hook.text, spec.hook.seconds) if spec.hook.text.strip() else None
    mark = (spec.watermark.text, spec.watermark.corner) if spec.watermark.text.strip() else None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    part = out_path.resolve().with_name(out_path.name + ".part")   # el clip no existe hasta estar completo
    cache: dict = {}
    variants = ["normal"] if spec.layout == "fit" else ["normal", "static", "flush", "fit"]
    last_err = ""
    with tempfile.TemporaryDirectory(prefix="render_") as wd:
        for variant in variants:
            sv = spec.model_copy(update={"layout": "fit"}) if variant == "fit" else spec
            graph, seam_y, note = build_graph(cfg, sv, info, input_args, static=(variant in ("static", "flush")), cache=cache,
                                              panels="flush" if variant == "flush" else None)
            pos = st.position or (cfg.subtitles.position if note.get("panels") == "overlay" else "seam" if note["layout"] == "split" else None)
            sub_cfg = with_preset(cfg.subtitles, st.preset, highlight_color=st.highlight_color,
                                  primary_color=st.primary_color, font_size=st.font_size, position=pos, uppercase=st.uppercase)
            ass = build_ass(words if st.enabled else [], sub_cfg, cfg.reframe.width, cfg.reframe.height,
                            seam_y=seam_y, hook=hook, watermark=mark, duration=dur)
            has_ass = bool((st.enabled and words) or hook or mark)
            graph += (";[v0]ass=subs.ass[v1]" if has_ass else ";[v0]null[v1]")
            cc = cfg.clip
            if not preview and cc.progress_bar:                  # barra que avanza con el clip
                graph += (f";color=c=white@0.85:s={cfg.reframe.width}x12:r=25:d={dur:.2f}[bar];"
                          f"[v1][bar]overlay=x=-w+w*t/{dur:.3f}:y=H-12:eval=frame:shortest=1[v1]")
            if not preview and cc.fade_seconds > 0 and dur > 4 * cc.fade_seconds:
                graph += f";[v1]fade=t=in:st=0:d={cc.fade_seconds},fade=t=out:st={dur - cc.fade_seconds:.3f}:d={cc.fade_seconds}[v1]"
            graph += ";[v1]scale=540:960[v]" if preview else ";[v1]null[v]"
            if has_ass:
                (Path(wd) / "subs.ass").write_text(ass, encoding="utf-8")   # ruta relativa + cwd evita escapes
            enc, err = _run_ffmpeg(cfg, input_args, graph, part, wd, preview, dur, _audio_filter(cfg, dur, preview))
            if enc:
                os.replace(part, out_path.resolve())
                meta = {**note, "encoder": enc, "duration": round(dur, 2), "source_resolution": f"{info.width}x{info.height}"}
                if variant != "normal":
                    meta["fallback"] = variant
                    log.warning("render con respaldo '%s' (el normal falló): %s", variant, last_err[-200:])
                if adjusted:
                    meta["adjusted"] = adjusted
                return meta
            last_err = err
    ctx = f"entrada {info.width}x{info.height}, {total:.1f} s, recorte {start:.1f}–{start + dur:.1f} s, diseño {spec.layout}"
    raise RuntimeError(f"Render falló ({ctx}): {last_err[-700:]}")
