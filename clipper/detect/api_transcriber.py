"""Transcripción vía un servicio compatible con OpenAI (POST {base_url}/audio/transcriptions).

Solo se usa si TÚ lo activas en Ajustes (transcribe.provider = api). El audio del directo se envía al
endpoint configurado; apúntalo a un servidor tuyo (localhost) si no quieres que salga de tu red."""
from __future__ import annotations

import io
import wave

import httpx
import numpy as np

from ..config import ApiCfg
from ..subtitles import Word
from .vad import SR


def to_wav(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def parse_words(j: dict, t0: float) -> list[Word]:
    words = [Word(t0 + float(w["start"]), t0 + float(w["end"]), str(w["word"]).strip())
             for w in j.get("words") or [] if str(w.get("word", "")).strip()]
    if words:
        return words
    # El servicio no da timestamps por palabra: reparte cada segmento proporcionalmente (aproximado).
    out: list[Word] = []
    for sg in j.get("segments") or []:
        toks = str(sg.get("text", "")).split()
        if not toks:
            continue
        a, b = float(sg["start"]), float(sg["end"])
        step = (b - a) / len(toks)
        out += [Word(t0 + a + i * step, t0 + a + (i + 1) * step, t) for i, t in enumerate(toks)]
    return out


def transcribe_api(cfg: ApiCfg, audio: np.ndarray, t0: float, language: str | None, timeout: float = 90) -> list[Word]:
    if not cfg.base_url:
        raise RuntimeError("Falta la URL del servicio de transcripción (Ajustes → Transcripción → API)")
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    data = {"model": cfg.model, "response_format": "verbose_json", "timestamp_granularities[]": ["word", "segment"]}
    if language:
        data["language"] = language
    r = httpx.post(f"{cfg.base_url}/audio/transcriptions", headers=headers, data=data, timeout=timeout,
                   files={"file": ("audio.wav", to_wav(audio), "audio/wav")})
    if r.status_code >= 400:
        raise RuntimeError(f"El servicio respondió {r.status_code}: {r.text[:200]}")
    return parse_words(r.json(), t0)
