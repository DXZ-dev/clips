from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from .buffer import RollingBuffer
from .config import load_config
from .ffutil import hw_report
from .pipeline import clip_from_buffer, clip_from_file, load_words


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="clipper", description="Clips verticales 9:16 desde directos")
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"],
                    help="GPU NVIDIA (cuda), solo CPU (cpu) o automático; sobrescribe config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="Muestra hardware/encoder detectado")
    sub.add_parser("ingest", help="Ejecuta la ingesta y mantiene el buffer circular")
    c = sub.add_parser("clip", help="Genera un clip con los últimos N segundos del buffer")
    c.add_argument("-s", "--seconds", type=int)
    c.add_argument("--words", help="JSON con [{start,end,word}] relativos al clip")
    sub.add_parser("run", help="Motor completo en terminal: ingesta + disparadores + atajos de teclado")
    sv = sub.add_parser("serve", help="Motor completo + panel web")
    sv.add_argument("--host"); sv.add_argument("--port", type=int)
    dm = sub.add_parser("download-model", help="Descarga UNA VEZ un modelo Whisper a ./models (única operación con red)")
    dm.add_argument("name", nargs="?", default="small")
    tr = sub.add_parser("transcribe", help="Transcribe un archivo a JSON de palabras (para --words)")
    tr.add_argument("input"); tr.add_argument("-o", "--out")
    f = sub.add_parser("reframe", help="Convierte un archivo a 9:16 con tracking y subtítulos")
    f.add_argument("input")
    f.add_argument("-o", "--out")
    f.add_argument("--words")
    f.add_argument("-s", "--last-seconds", type=float)
    a = ap.parse_args(argv)

    if a.cmd != "download-model":       # ejecución normal: cero tráfico hacia Hugging Face
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(a.config)
    if a.device:
        cfg.compute.device = a.device

    if a.cmd == "check":
        print(json.dumps(hw_report(), indent=2))
    elif a.cmd == "ingest":
        rb = RollingBuffer(cfg)
        rb.start()
        try:
            while True:
                time.sleep(5)
                print(json.dumps(rb.status()))
        except KeyboardInterrupt:
            rb.stop()
    elif a.cmd == "clip":
        words = load_words(a.words) if a.words else None
        print(clip_from_buffer(cfg, a.seconds or cfg.clip.default_seconds, words))
    elif a.cmd == "reframe":
        words = load_words(a.words) if a.words else None
        print(clip_from_file(cfg, a.input, Path(a.out) if a.out else None, words, a.last_seconds))
    elif a.cmd == "download-model":
        from faster_whisper.utils import download_model
        dest = cfg.path(f"models/whisper-{a.name}")
        print("Descargando (necesita internet solo ahora)…")
        download_model(a.name, output_dir=str(dest))
        print(f"Listo: {dest}. A partir de ahora funciona sin red.")
    elif a.cmd == "run":
        _run_terminal(cfg)
    elif a.cmd == "serve":
        import uvicorn
        from .engine import Engine
        from .web.app import create_app
        uvicorn.run(create_app(Engine(cfg)), host=a.host or cfg.server.host, port=a.port or cfg.server.port,
                    log_level="warning")
    elif a.cmd == "transcribe":
        from .audio import load_audio
        from .detect.transcriber import LiveTranscriber
        lt = LiveTranscriber(cfg)
        words = lt.transcribe(load_audio(["-i", a.input]), vad_filter=True)
        data = [{"start": round(w.start, 2), "end": round(w.end, 2), "word": w.text} for w in words]
        text = json.dumps(data, ensure_ascii=False, indent=1)
        Path(a.out).write_text(text) if a.out else print(text)
    return 0


def _run_terminal(cfg) -> None:
    """Atajos: Enter o 'c' = clip por defecto; 15/30/60 = esa duración; 's' = estado; 'q' = salir."""
    from .engine import Engine
    eng = Engine(cfg)
    eng.start()
    print("clipper en marcha. Enter/c = clip, 15|30|60 = clip de N s, s = estado, q = salir")
    try:
        for line in sys.stdin:
            cmd = line.strip().lower()
            if cmd == "q":
                break
            if cmd == "s":
                print(json.dumps(eng.status(), ensure_ascii=False, indent=1))
            elif cmd in ("", "c") or cmd.isdigit():
                try:
                    job = eng.manual_clip(int(cmd) if cmd.isdigit() else None)
                    print(f"clip {job.id} en cola")
                except RuntimeError as e:
                    print("no se pudo:", e)
        else:                       # stdin cerrado (ejecución en segundo plano): sigue hasta Ctrl+C
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        eng.stop()
