"""Disparadores automáticos: picos de energía, cadencia rápida, palabras clave/risas."""
from __future__ import annotations

import re
import unicodedata
from collections import deque
from dataclasses import dataclass

import numpy as np

from ..config import Config
from ..subtitles import Word
from .vad import SR


@dataclass
class Trigger:
    kind: str       # energy | cadence | keyword | laughter | manual
    t: float        # tiempo de stream del momento clave
    detail: str = ""
    strength: float = 1.0   # 0.5..1.5: cuánto destaca (para puntuar momentos en vídeos)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return re.sub(r"[^a-z0-9ñ ]", "", "".join(c for c in s if unicodedata.category(c) != "Mn")).strip()


_LAUGH = re.compile(r"^(?:(?:ja|je|ji|ha|jo){2,}h?|jaja\w*|haha\w*|risas|xd+)$")


class KeywordMatcher:
    def __init__(self, cfg: Config):
        self.c = cfg.triggers.keywords
        self.phrases = [[t for t in norm(p).split() if t] for p in self.c.phrases if norm(p)]
        self.tail: list[tuple[str, Word]] = []
        self.last_end = -1.0

    def feed(self, words: list[Word]) -> list[Trigger]:
        if not self.c.enabled and not self.c.laughter:
            return []
        self.tail = (self.tail + [(norm(w.text).replace(" ", ""), w) for w in words])[-40:]
        toks = [t for t, _ in self.tail]
        out: list[Trigger] = []
        if self.c.enabled:
            for ph in self.phrases:
                n = len(ph)
                for i in range(len(toks) - n + 1):
                    end_w = self.tail[i + n - 1][1]
                    if toks[i:i + n] == ph and end_w.end > self.last_end:
                        out.append(Trigger("keyword", end_w.end, " ".join(ph)))
        if self.c.laughter:
            for t, w in self.tail:
                if w.end > self.last_end and _LAUGH.match(t):
                    out.append(Trigger("laughter", w.end, w.text))
        if out:
            self.last_end = max(tr.t for tr in out)
        return sorted(out, key=lambda x: x.t)


class EnergyDetector:
    """Pico de volumen respecto al nivel habitual (mediana móvil de 60 s)."""
    FRAME = 0.25

    def __init__(self, cfg: Config):
        self.c = cfg.triggers.energy
        self.hist: deque[tuple[float, float]] = deque(maxlen=int(60 / self.FRAME))
        self.refractory_until = -1.0

    def feed(self, audio: np.ndarray, t0: float) -> list[Trigger]:
        if not self.c.enabled:
            return []
        n, out = int(SR * self.FRAME), []
        win = max(1, int(self.c.window_seconds / self.FRAME))
        for i in range(len(audio) // n):
            fr = audio[i * n:(i + 1) * n]
            t = t0 + (i + 1) * self.FRAME
            self.hist.append((t, 20 * np.log10(max(float(np.sqrt(np.mean(fr ** 2))), 1e-6))))
            if len(self.hist) < win + 4 or self.hist[-1][0] - self.hist[0][0] < self.c.warmup_seconds:
                continue
            recent = [d for _, d in list(self.hist)[-win:]]
            base = [d for _, d in list(self.hist)[:-win] if d > -55]
            if not base:
                continue
            cur, med = float(np.mean(recent)), float(np.median(base))
            if cur - med >= self.c.spike_db and cur >= self.c.floor_db and t > self.refractory_until:
                out.append(Trigger("energy", t, f"+{cur - med:.0f} dB sobre lo habitual", min(1.5, 0.5 + (cur - med) / 20)))
                self.refractory_until = t + 5
        return out


class CadenceDetector:
    def __init__(self, cfg: Config):
        self.c = cfg.triggers.cadence
        self.refractory_until = -1.0

    def feed(self, words: list[Word]) -> list[Trigger]:
        if not self.c.enabled or not words:
            return []
        end = words[-1].end
        recent = [w for w in words if w.end > end - self.c.window_seconds]
        rate = len(recent) / self.c.window_seconds
        if rate >= self.c.words_per_second and end > self.refractory_until:
            self.refractory_until = end + 8
            return [Trigger("cadence", end, f"{rate:.1f} palabras/s", min(1.5, rate / self.c.words_per_second))]
        return []
