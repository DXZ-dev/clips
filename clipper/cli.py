from __future__ import annotations

import argparse
import json
import logging
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
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="Muestra hardware/encoder detectado")
    sub.add_parser("ingest", help="Ejecuta la ingesta y mantiene el buffer circular")
    c = sub.add_parser("clip", help="Genera un clip con los últimos N segundos del buffer")
    c.add_argument("-s", "--seconds", type=int)
    c.add_argument("--words", help="JSON con [{start,end,word}] relativos al clip")
    f = sub.add_parser("reframe", help="Convierte un archivo a 9:16 con tracking y subtítulos")
    f.add_argument("input")
    f.add_argument("-o", "--out")
    f.add_argument("--words")
    f.add_argument("-s", "--last-seconds", type=float)
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(a.config)

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
    return 0
