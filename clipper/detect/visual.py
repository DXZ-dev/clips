"""Señales visuales para puntuar momentos: acción en pantalla (con más peso si es céntrica), reacción de la cámara del
streamer y personajes presentes. Trabaja con fotogramas pequeños (2 por segundo): es barato y no descarga modelos."""
from __future__ import annotations

from collections import deque
from typing import Iterable

import cv2
import numpy as np

from ..config import Config, RectCfg
from ..tracking import PersonDetector, motion_center, pick_person
from .triggers import Trigger

FPS = 2.0


class VisualDetector:
    def __init__(self, cfg: Config, cam: RectCfg | None = None, person_detector=None, zone: RectCfg | None = None):
        self.cfg, self.cam, self.zone = cfg, cam, zone
        self.prev = None
        self.game_hist: deque[float] = deque(maxlen=120)       # ~60 s de energía de movimiento del juego
        self.cam_hist: deque[float] = deque(maxlen=120)        # ~60 s de movimiento en la cámara del streamer
        self.persons: deque[tuple[float, int, float]] = deque(maxlen=600)   # (t, nº de figuras, x del principal)
        self._pdet = person_detector
        self._n = 0
        self._ref: dict[str, float] = {}

    def _person_det(self):
        if self._pdet is None and self.cfg.reframe.track_people:
            self._pdet = PersonDetector()
        return self._pdet

    def bonus(self, t: float, window: float = 5.0) -> bool:
        """¿Había personajes/personas en pantalla cerca de ese instante?"""
        return any(abs(pt - t) <= window and n > 0 for pt, n, _ in self.persons)

    def feed(self, frames: Iterable[np.ndarray], t0: float) -> list[Trigger]:
        out: list[Trigger] = []
        for i, bgr in enumerate(frames):
            t = t0 + i / FPS
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            cam_px = None
            if self.cam:
                cam_px = (int(self.cam.x * w), int(self.cam.y * h), int((self.cam.x + self.cam.w) * w) + 1, int((self.cam.y + self.cam.h) * h) + 1)
            if self.prev is not None and self.prev.shape == gray.shape:
                d = cv2.GaussianBlur(cv2.absdiff(gray, self.prev), (5, 5), 0)
                mask = d > 22
                cam_e = None
                if cam_px:
                    x0, y0, x1, y1 = cam_px
                    cam_e = float(mask[y0:y1, x0:x1].mean()) if (y1 > y0 and x1 > x0) else None
                    mask = mask.copy()
                    mask[y0:y1, x0:x1] = False
                zone_px = None
                if self.zone:                                         # solo cuenta el movimiento dentro de la zona marcada
                    zx0, zy0, zx1, zy1 = (int(self.zone.x * w), int(self.zone.y * h), int((self.zone.x + self.zone.w) * w) + 1,
                                          int((self.zone.y + self.zone.h) * h) + 1)
                    keep = np.zeros_like(mask)
                    keep[zy0:zy1, zx0:zx1] = True
                    mask = mask & keep
                    zone_px = (zx0, zy0, zx1, zy1)
                game_e = float(mask.sum()) / max(1.0, float(keep.sum()) if self.zone else float(mask.size))
                cx = motion_center(gray, self.prev, cam_px, self.cfg.reframe.center_bias, zone_px)
                centrality = 1 - abs((cx if cx is not None else 0.5) - 0.5) * 2
                out += self._check("action", t, game_e, self.game_hist, floor=0.03, ratio=2.5, refractory=6.0,
                                   detail="movimiento en pantalla", extra=0.3 * centrality)
                if cam_e is not None:
                    out += self._check("reaction", t, cam_e, self.cam_hist, floor=0.02, ratio=2.5, refractory=8.0,
                                       detail="reacción del streamer")
                    self.cam_hist.append(cam_e)
                self.game_hist.append(game_e)
            self.prev = gray
            if self._n % 4 == 0 and self._person_det() is not None:            # personajes: cada ~2 s
                rects = self._person_det().detect(bgr)
                self.persons.append((t, len(rects), pick_person(rects, w, cam_px) or 0.5))
            self._n += 1
        return out

    def _check(self, kind: str, t: float, value: float, hist: deque, floor: float, ratio: float, refractory: float,
               detail: str, extra: float = 0.0) -> list[Trigger]:
        if len(hist) < 20:                                                      # calentamiento: ~10 s de historia
            return []
        base = max(float(np.median(hist)), 0.002)
        if value >= max(floor, base * ratio) and t - self._ref.get(kind, -1e9) >= refractory:
            self._ref[kind] = t
            return [Trigger(kind, t, detail, min(1.5, 0.6 + value / (base * 6) + extra))]
        return []
