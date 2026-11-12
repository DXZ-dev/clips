"""Carga y validación de config.yaml (pydantic)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent


class SourceCfg(BaseModel):
    type: Literal["file", "rtmp", "hls", "streamlink", "ytdlp"] = "file"
    url: str = ""
    rtmp_listen: bool = True
    streamlink_quality: str = "best"
    file: str = "./samples/test.mp4"
    file_loop: bool = True
    file_follow: bool = False


    @field_validator("url")
    @classmethod
    def _no_flag_injection(cls, v: str) -> str:
        v = v.strip()
        if v.startswith("-"):
            raise ValueError("la URL no puede empezar por '-'")
        return v

    @model_validator(mode="after")
    def _scheme(self):
        ok = {"rtmp": ("rtmp://", "rtmps://"), "hls": ("http://", "https://"),
              "streamlink": ("http://", "https://"), "ytdlp": ("http://", "https://")}
        if self.url and self.type in ok and not self.url.startswith(ok[self.type]):
            raise ValueError(f"para type={self.type} la URL debe empezar por {' o '.join(ok[self.type])}")
        return self


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


class ApiCfg(BaseModel):
    """Servicio de transcripción compatible con OpenAI (/audio/transcriptions). Lo eliges tú."""
    base_url: str = ""                   # p. ej. http://localhost:8080/v1  (un servidor tuyo) u otro de tu elección
    model: str = "whisper-1"
    api_key: str = Field("", exclude=True, repr=False)   # nunca se guarda en config.yaml ni se devuelve por la API

    @field_validator("base_url")
    @classmethod
    def _http(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("base_url debe empezar por http:// o https://")
        return v


class TranscribeCfg(BaseModel):
    enabled: bool = True
    provider: Literal["local", "api"] = "local"   # local = faster-whisper en tu máquina (sin red)
    allow_downloads: bool = False        # False = jamás contacta Hugging Face; usa solo modelos ya descargados
    api: ApiCfg = ApiCfg()
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
    allowed_hosts: list[str] = []        # nombres extra aceptados en la cabecera Host (anti DNS-rebinding)


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


SETTINGS_FILE = ROOT / "settings.local.yaml"   # cambios hechos desde el panel (se aplican sobre config.yaml)
SECRETS_FILE = ROOT / "secrets.local.yaml"     # claves de API (chmod 600, fuera de git)


def deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | Path | None = None) -> Config:
    """config.yaml (+ settings.local.yaml + secrets.local.yaml cuando se usa el config por defecto)."""
    default = path is None
    path = Path(path) if path else ROOT / "config.yaml"
    data = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    secrets = {}
    if default:
        if SETTINGS_FILE.exists():
            data = deep_merge(data, yaml.safe_load(SETTINGS_FILE.read_text()) or {})
        if SECRETS_FILE.exists():
            secrets = yaml.safe_load(SECRETS_FILE.read_text()) or {}
    cfg = Config.model_validate(data)
    import os
    cfg.transcribe.api.api_key = os.environ.get("CLIPPER_API_KEY") or secrets.get("transcribe_api_key", "")
    return cfg
