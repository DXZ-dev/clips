"""VAD (detección de voz). Usa silero-vad; si no está instalado, el VAD silero ONNX que trae faster-whisper."""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("clipper.vad")
SR = 16000


class SpeechDetector:
    def __init__(self, threshold: float = 0.5, min_silence_ms: int = 500):
        self.th, self.min_sil = threshold, min_silence_ms
        try:
            import torch
            from silero_vad import get_speech_timestamps, load_silero_vad
            self._torch, self._ts, self._model = torch, get_speech_timestamps, load_silero_vad()
            self.backend = "silero-vad"
        except Exception as e:  # noqa: BLE001
            log.warning("silero-vad no disponible (%s); usando el VAD de faster-whisper", e)
            self._model = None
            self.backend = "faster-whisper-vad"

    def speech(self, audio: np.ndarray) -> list[tuple[float, float]]:
        """Tramos de voz (inicio, fin) en segundos relativos al audio."""
        if audio.size < SR // 4:
            return []
        if self._model is not None:
            r = self._ts(self._torch.from_numpy(audio), self._model, threshold=self.th,
                         sampling_rate=SR, min_silence_duration_ms=self.min_sil, return_seconds=True)
            return [(x["start"], x["end"]) for x in r]
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        r = get_speech_timestamps(audio, VadOptions(threshold=self.th, min_silence_duration_ms=self.min_sil))
        return [(x["start"] / SR, x["end"] / SR) for x in r]
