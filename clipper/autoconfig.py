"""Configuración automática: deja todo listo para sacar clips verticales 9:16 con aspecto profesional con un solo botón."""
from __future__ import annotations

import os
from typing import Callable

from . import ai
from .config import SubtitlesCfg
from .ffutil import resolve_device
from .models import whisper_dir
from .subtitles import with_preset

STYLE_KEYS = ("font_size", "uppercase", "primary_color", "highlight_color", "outline_color", "outline", "max_words_per_line", "bold", "shadow")


def _step(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def _detect_cam_local(engine):
    """Detector local (MediaPipe) sobre los últimos segundos del buffer."""
    from .ffutil import probe
    from .tracking import detect_facecam
    b = engine.reader.bounds()
    ci = engine.reader.prepare_range(max(b[0], b[1] - 10), b[1])
    try:
        info = probe(str(ci.workdir / "list.txt"), ["-f", "concat", "-safe", "0"])
        return detect_facecam(ci.args, info, engine.cfg)
    finally:
        ci.cleanup()


def _available_whisper(engine) -> list[str]:
    root = whisper_dir(engine.cfg).parent
    names = []
    for n in ("tiny", "base", "small", "medium", "large-v3"):
        if (root / f"whisper-{n}").is_dir():
            names.append(n)
    return names


def run(engine, jpeg_fn: Callable[[], bytes]) -> dict:
    cfg, steps, patch = engine.cfg, [], {}
    b = engine.reader.bounds()
    live = bool(b and b[1] - b[0] >= 4)
    steps.append(_step("Señal del directo", "ok" if live else "warn",
                       "recibiendo señal" if live else "todavía no llega señal: se aplican los valores recomendados; "
                       "cuando haya directo vuelve a pulsar para ubicar la cámara"))

    # 1) cámara y diseño
    cam, how = None, ""
    if live:
        if cfg.ai.llm.enabled:
            try:
                cam, how = ai.locate_faces(cfg.ai.llm, jpeg_fn())["facecam"], "la IA de tu API"
            except Exception as e:  # noqa: BLE001
                steps.append(_step("IA de visión", "warn", f"{e} — se usa el detector local"))
        if cam is None:
            try:
                r = _detect_cam_local(engine)
                cam, how = (r.model_dump() if r else None), "el detector local"
            except Exception as e:  # noqa: BLE001
                steps.append(_step("Detector local de cámara", "warn", str(e)[:160]))
    if cam:
        patch["layout"] = {"mode": "split", "facecam": cam, "cam_follow": True, "cam_zoom": 1.15, "gameplay": "center", "facecam_height_pct": 34}
        steps.append(_step("Cámara y diseño", "ok", f"cámara ubicada con {how}: cámara arriba y centro del gameplay abajo, siguiendo tu rostro"))
    elif live:
        patch["layout"] = {"mode": "auto", "facecam": None}
        steps.append(_step("Cámara y diseño", "ok", "no hay cámara superpuesta: recorte vertical que sigue a personas/personajes y a la acción (con preferencia por el centro)"))
    else:
        steps.append(_step("Cámara y diseño", "skip", "se mantiene el diseño actual hasta que haya directo"))

    # 2) subtítulos estilo TikTok
    st = with_preset(SubtitlesCfg(), "hormozi")
    style = {k: getattr(st, k) for k in STYLE_KEYS}
    style.update(enabled=True, position="seam" if cam else "lower_third", offset_y=0, reviewed=True)
    patch["subtitles"] = style
    steps.append(_step("Subtítulos", "ok", "estilo «hormozi» (letras grandes con la palabra activa en verde y borde grueso), "
                       + ("en la unión cámara/gameplay" if cam else "en el tercio inferior")))

    # 3) detectores y sensibilidad
    patch["triggers"] = {"sensitivity": "medium", "min_score": 2.5, "cooldown_seconds": 40, "auto_clip_seconds": 30, "max_per_hour": 12,
                         "energy": {"enabled": True, "spike_db": 10}, "cadence": {"enabled": True, "words_per_second": 4.5},
                         "keywords": {"enabled": True, "laughter": True}, "visual": {"enabled": True}}
    steps.append(_step("Detección de momentos", "ok", "voz, risas, volumen, habla rápida, movimiento y reacción del streamer; clips de 30 s, máx. 12 por hora"))

    # 4) modelo de voz según el hardware
    dev = resolve_device(cfg.compute.device)
    want = "medium" if dev == "cuda" else ("small" if (os.cpu_count() or 2) >= 12 else "base")
    have = _available_whisper(engine)
    order = ["tiny", "base", "small", "medium", "large-v3"]
    ok = [m for m in order if m in have and order.index(m) <= order.index(want)]
    if cfg.transcribe.provider == "api":
        steps.append(_step("Transcripción", "ok", "se usa tu API de transcripción"))
    elif ok or have:
        pick = ok[-1] if ok else have[0]                 # el mejor ≤ ideal; si no hay, el más pequeño que exista (algo es mejor que nada)
        patch["transcribe"] = {"enabled": True, "model": pick}
        steps.append(_step("Transcripción", "ok" if pick == want else "warn",
                           f"modelo «{pick}» en {dev.upper()}" + ("" if pick == want else f" (para tu hardware iría mejor «{want}»: clipper download-model {want})")))
    else:
        steps.append(_step("Transcripción", "warn", f"no hay modelos descargados: clipper download-model {want}"))

    # 5) modo automático a revisión (nunca publica solo)
    dests = [n for n in ("youtube", "tiktok", "instagram", "webhook") if getattr(cfg.publish, n).enabled]
    patch["triggers"]["auto_enabled"] = True
    if dests:
        patch["publish"] = {"enabled": True, **{n: {"mode": "review"} for n in dests}}
    engine.resume() if getattr(engine, "paused", False) else None
    steps.append(_step("Modo automático", "ok", "activado en «a revisión»: crea los clips y tú apruebas antes de publicar"
                       + (f" ({', '.join(dests)})" if dests else ". Activa tus redes en la tarjeta del modo automático")))

    res = engine.apply_settings(patch)
    return {"steps": steps, "changed": res["changed"], "live": live, "camera": cam}
