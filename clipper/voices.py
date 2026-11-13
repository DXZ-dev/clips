"""Distingue voces por el tono (F0) de cada palabra: hombre / mujer y, dentro de cada género, una segunda voz si el tono es claramente otro.
Sin modelos pesados: autocorrelación con numpy. Si solo se detecta una voz, no se cambia ningún color."""
from __future__ import annotations

import subprocess

import numpy as np

SR = 16000
FEMALE_HZ = 165.0           # por encima = voz aguda (mujer / voz de niño); por debajo = grave (hombre)


def f0_of(x: np.ndarray, sr: int = SR) -> float | None:
    """Tono mediano (Hz) de un trozo de audio, o None si no hay voz clara."""
    if len(x) < int(0.06 * sr):
        return None
    win, hop = int(0.04 * sr), int(0.01 * sr)
    lo, hi = int(sr / 320), int(sr / 70)
    vals = []
    for i in range(0, len(x) - win - hi, hop):
        f = x[i:i + win + hi].astype(np.float64)
        seg = f[:win]
        if np.sqrt(np.mean(seg ** 2)) < 0.01:
            continue
        seg = seg - seg.mean()
        e0 = float(seg @ seg) + 1e-9
        ac = np.array([float(seg @ (f[l:l + win] - f[l:l + win].mean())) for l in range(lo, hi)])
        ac /= np.sqrt(e0 * (np.array([float(np.sum((f[l:l + win] - f[l:l + win].mean()) ** 2)) for l in range(lo, hi)]) + 1e-9))
        k = int(np.argmax(ac))
        for j in range(1, len(ac) - 1):                       # evita el error de octava: el primer pico casi tan alto como el máximo
            if ac[j] >= 0.88 * ac[k] and ac[j] >= ac[j - 1] and ac[j] >= ac[j + 1]:
                k = j
                break
        if ac[k] > 0.45:
            vals.append(sr / (lo + k))
    return float(np.median(vals)) if len(vals) >= 3 else None


def assign(f0s: list[float | None]) -> list[tuple[str, int]] | None:
    """Etiqueta (género, n.º de voz) por palabra, o None si todo parece una sola voz."""
    known = [f for f in f0s if f]
    if len(known) < 6:
        return None
    g = [None if f is None else ("f" if f >= FEMALE_HZ else "m") for f in f0s]
    for gen in ("f", "m"):                                      # el género minoritario debe ser relevante (≥20 % y ≥3 palabras)
        n = sum(1 for x in g if x == gen)
        if 0 < n and (n < 3 or n < 0.2 * len(known)):
            other = "m" if gen == "f" else "f"
            g = [other if x == gen else x for x in g]
    labels: list[tuple[str, int] | None] = [None] * len(g)
    for gen in ("f", "m"):
        idx = [i for i, x in enumerate(g) if x == gen]
        vals = sorted(f0s[i] for i in idx)
        sub = {i: 0 for i in idx}
        cuts: list[float] = []
        if len(vals) >= 6:                                      # hasta 3 voces por género: se corta en los saltos de tono claros
            gaps = sorted(((vals[k + 1] / vals[k], k) for k in range(2, len(vals) - 2)), reverse=True)
            for ratio, k in gaps:
                if ratio < 1.3 or len(cuts) >= 2:
                    break
                c = (vals[k] + vals[k + 1]) / 2
                if all(abs(c - x) > 8 for x in cuts) and sum(v <= c for v in vals) >= 3 and sum(v > c for v in vals) >= 3:
                    cuts.append(c)
            cuts.sort()
            sub = {i: sum(f0s[i] > c for c in cuts) for i in idx}
        for i in idx:
            labels[i] = (gen, sub[i])
    last = next((l for l in labels if l), None)                  # palabras sin tono: heredan la anterior
    out = []
    for l in labels:
        last = l or last
        out.append(last)
    if len(set(out)) < 2:
        return None
    sm = list(out)                                               # suaviza saltos sueltos de una sola palabra
    for i in range(1, len(out) - 1):
        if out[i - 1] == out[i + 1] != out[i]:
            sm[i] = out[i - 1]
    return sm


def audio(src: str, start: float, dur: float) -> np.ndarray:
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src,
                        "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.float32) if r.returncode == 0 else np.zeros(0, np.float32)


def label_words(src: str, start: float, dur: float, words) -> list[tuple[str, int]] | None:
    """`words`: Word con tiempos relativos al clip. Devuelve la etiqueta por palabra (o None)."""
    x = audio(src, start, dur)
    if len(x) < SR:
        return None
    f0s = [f0_of(x[int(max(0, w.start) * SR):int(w.end * SR) + 1]) for w in words]
    return assign(f0s)
