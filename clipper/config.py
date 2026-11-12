"""Carga y validación de config.yaml (pydantic)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent


class SourceCfg(BaseModel):
    type: Literal["file", "rtmp", "hls", "streamlink", "ytdlp"] = "file"
    url: str = ""
    rtmp_listen: bool = True
    streamlink_quality: str = "best"
    file: str = "./samples/test.mp4"
    file_loop: bool = True
    file_follow: bool = False


class BufferCfg(BaseModel):
    dir: str = "/tmp/clip_buffer"
    segment_seconds: int = Field(5, ge=1)
    window_seconds: int = Field(90, ge=10)
    reencode: bool = False


class ClipCfg(BaseModel):
    default_seconds: int = 30
    duration_options: list[int] = [15, 30, 60]
    output_dir: str = "./output_clips"


class SmoothingCfg(BaseModel):
    ema_alpha: float = Field(0.18, gt=0, le=1)
    deadzone: float = Field(0.04, ge=0, lt=0.5)
    max_speed: float = Field(0.9, gt=0)
    lost_hold_seconds: float = 1.5


class SpeakerCfg(BaseModel):
    switch_ratio: float = 1.6
    switch_frames: int = 3
    audio_gate_db: float = -45.0


class ReframeCfg(BaseModel):
    width: int = 1080
    height: int = 1920
    detector: Literal["auto", "mediapipe", "haar", "none"] = "auto"
    mediapipe_model: str = "./models/blaze_face_short_range.tflite"
    analysis_fps: int = 6
    analysis_width: int = 480
    smoothing: SmoothingCfg = SmoothingCfg()
    speaker: SpeakerCfg = SpeakerCfg()


class SubtitlesCfg(BaseModel):
    enabled: bool = True
    font: str = "DejaVu Sans"
    font_size: int = 84
    uppercase: bool = True
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFE600"
    outline_color: str = "#000000"
    outline: int = 9
    position: Literal["center", "lower_third", "bottom"] = "lower_third"
    max_words_per_line: int = 3
    max_lines: int = 2


class RenderCfg(BaseModel):
    encoder: Literal["auto", "h264_nvenc", "hevc_nvenc", "libx264"] = "auto"
    preset_cpu: str = "veryfast"
    crf: int = 21
    nvenc_cq: int = 23
    audio_bitrate: str = "160k"


class ComputeCfg(BaseModel):
    """Dónde corre el trabajo pesado. auto = GPU NVIDIA si funciona, si no CPU."""
    device: Literal["auto", "cuda", "cpu"] = "auto"


class VadCfg(BaseModel):
    threshold: float = 0.5
    min_silence_ms: int = 500
    max_chunk_seconds: float = 20.0


class TranscribeCfg(BaseModel):
    enabled: bool = True
    model: str = "small"                 # tiny | base | small | medium | large-v3 ...
    language: str | None = "es"          # None = autodetectar
    compute_type: str = "auto"           # auto => float16 en GPU, int8 en CPU
    beam_size: int = 1
    vad: VadCfg = VadCfg()


class EnergyCfg(BaseModel):
    enabled: bool = True
    spike_db: float = 10.0               # subida sobre la mediana de los últimos 60 s
    floor_db: float = -38.0              # nivel mínimo absoluto para considerar un pico
    window_seconds: float = 1.0
    warmup_seconds: float = 20.0


class CadenceCfg(BaseModel):
    enabled: bool = True
    words_per_second: float = 4.5
    window_seconds: float = 4.0


class KeywordsCfg(BaseModel):
    enabled: bool = True
    laughter: bool = True                # "jajaja" en la transcripción cuenta como risa
    phrases: list[str] = ["mira esto", "increíble", "ojo con esto", "no puede ser", "qué locura"]


class TriggersCfg(BaseModel):
    cooldown_seconds: float = 40.0
    auto_clip_seconds: int = 30
    post_roll_seconds: float = 8.0       # cuánto después del disparo se sigue grabando
    energy: EnergyCfg = EnergyCfg()
    cadence: CadenceCfg = CadenceCfg()
    keywords: KeywordsCfg = KeywordsCfg()


class ServerCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class Config(BaseModel):
    compute: ComputeCfg = ComputeCfg()
    transcribe: TranscribeCfg = TranscribeCfg()
    triggers: TriggersCfg = TriggersCfg()
    server: ServerCfg = ServerCfg()
    source: SourceCfg = SourceCfg()
    buffer: BufferCfg = BufferCfg()
    clip: ClipCfg = ClipCfg()
    reframe: ReframeCfg = ReframeCfg()
    subtitles: SubtitlesCfg = SubtitlesCfg()
    render: RenderCfg = RenderCfg()

    def path(self, p: str) -> Path:
        """Resuelve rutas relativas contra la raíz del proyecto."""
        q = Path(p).expanduser()
        return q if q.is_absolute() else (ROOT / q).resolve()


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else ROOT / "config.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return Config.model_validate(data or {})
