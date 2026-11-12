"""Transcripción continua: VAD -> trozos de voz -> faster-whisper con timestamps por palabra."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

import numpy as np

from ..config import Config
from ..ffutil import resolve_device
from ..subtitles import Word
from .api_transcriber import transcribe_api
from .vad import SR, SpeechDetector

log = logging.getLogger("clipper.transcriber")


class WordStore:
    """Palabras con tiempo ABSOLUTO de stream (mismo eje que los segmentos del buffer)."""

    def __init__(self, keep_seconds: float = 240):
        self.keep = keep_seconds
        self._w: list[Word] = []
        self._lock = threading.Lock()

    def add(self, words: list[Word]) -> None:
        with self._lock:
            self._w.extend(words)
            if self._w:
                cut = self._w[-1].end - self.keep
                self._w = [w for w in self._w if w.end >= cut]

    def between(self, t0: float, t1: float) -> list[Word]:
        with self._lock:
            return [w for w in self._w if w.end > t0 and w.start < t1]

    def tail(self, n: int = 40) -> list[Word]:
        with self._lock:
            return list(self._w[-n:])


class LiveTranscriber:
    def __init__(self, cfg: Config, on_words: Callable[[list[Word]], None] | None = None):
        self.cfg, self.tc = cfg, cfg.transcribe
        self.on_words = on_words
        self.pend = np.zeros(0, np.float32)
        self.pend_t0 = 0.0
        self.processed_until = 0.0
        self.device = resolve_device(cfg.compute.device)
        self.vad = SpeechDetector(self.tc.vad.threshold, self.tc.vad.min_silence_ms)
        self._model = None
        self._lock = threading.Lock()

    def _model_ref(self) -> str:
        """Carpeta local si existe (./models/whisper-<nombre> o una ruta), si no el nombre (caché local)."""
        name = self.tc.model
        for cand in (Path(name).expanduser(), self.cfg.path(f"models/whisper-{name}")):
            if cand.is_dir():
                return str(cand)
        return name

    def load(self) -> None:
        if self.tc.provider == "api":
            self._model = "api"                     # no hay modelo que cargar
            return
        if self._model is not None:
            return
        from faster_whisper import WhisperModel
        ct = self.tc.compute_type
        if ct == "auto":
            ct = "float16" if self.device == "cuda" else "int8"
        log.info("Cargando faster-whisper '%s' en %s (%s), VAD=%s", self.tc.model, self.device, ct,
                 self.vad.backend)
        try:
            self._model = WhisperModel(self._model_ref(), device=self.device, compute_type=ct,
                                       local_files_only=not self.tc.allow_downloads)
        except Exception as e:  # noqa: BLE001
            if not self.tc.allow_downloads:
                raise RuntimeError(
                    f"Modelo '{self.tc.model}' no encontrado en local y las descargas están desactivadas. "
                    f"Descárgalo una vez con: python -m clipper download-model {self.tc.model}  "
                    f"(o usa transcribe.provider: api). Detalle: {e}") from e
            raise

    def reload(self) -> None:
        """Fuerza recargar el modelo/proveedor (tras cambiar ajustes)."""
        with self._lock:
            self.device = resolve_device(self.cfg.compute.device)
            self._model = None

    def set_device(self, device: str) -> None:
        """Cambia GPU/CPU en caliente: el modelo se vuelve a cargar en el siguiente uso."""
        self.cfg.compute.device = device
        self.reload()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    # ------------------------------------------------------------------ API
    def feed(self, audio: np.ndarray, t0: float) -> None:
        """Añade audio contiguo (16 kHz mono) que empieza en el tiempo de stream t0."""
        with self._lock:
            if self.pend.size and abs(self.pend_t0 + self.pend.size / SR - t0) > 0.5:
                self._emit(self.pend, self.pend_t0)       # hueco/reinicio: cierra lo pendiente
                self.pend = np.zeros(0, np.float32)
            if not self.pend.size:
                self.pend_t0 = t0
            self.pend = np.concatenate([self.pend, audio])
            self._process()

    def transcribe(self, audio: np.ndarray, t0: float = 0.0, vad_filter: bool = False) -> list[Word]:
        self.load()
        if self.tc.provider == "api":
            return transcribe_api(self.tc.api, audio, t0, self.tc.language)
        segs, _ = self._model.transcribe(
            audio, language=self.tc.language, word_timestamps=True, vad_filter=vad_filter,
            beam_size=self.tc.beam_size, condition_on_previous_text=False)
        out = []
        for sg in segs:
            for w in sg.words or []:
                if w.word.strip():
                    out.append(Word(t0 + w.start, t0 + w.end, w.word.strip()))
        return out

    # ------------------------------------------------------------------ interno
    def _emit(self, audio: np.ndarray, t0: float) -> None:
        if audio.size < SR // 4:
            return
        words = self.transcribe(audio, t0)
        if words and self.on_words:
            self.on_words(words)

    def _drop(self, seconds: float) -> None:
        n = int(seconds * SR)
        self.pend = self.pend[n:]
        self.pend_t0 += n / SR

    def _process(self) -> None:
        dur = self.pend.size / SR
        spans = self.vad.speech(self.pend)
        if not spans:
            self._drop(max(0.0, dur - 1.0))               # solo conserva 1 s de contexto
            self.processed_until = self.pend_t0 + self.pend.size / SR
            return
        first, last_end = spans[0][0], spans[-1][1]
        ongoing = dur - last_end < self.tc.vad.min_silence_ms / 1000
        if ongoing and last_end - first < self.tc.vad.max_chunk_seconds:
            self.processed_until = self.pend_t0 + first    # esperando a que termine la frase
            return
        a, b = max(0.0, first - 0.2), (dur if ongoing else min(dur, last_end + 0.2))
        self._emit(self.pend[int(a * SR):int(b * SR)], self.pend_t0 + a)
        self._drop(b)
        self.processed_until = self.pend_t0
