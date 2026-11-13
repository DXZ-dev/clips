"""Modelo de edición de un clip (lo que el editor guarda y el renderizador consume). Tiempos en segundos de la FUENTE."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .config import Config, RectCfg


class EditWord(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(max_length=80)


class SubStyle(BaseModel):
    enabled: bool = True
    preset: str = "clasico"
    highlight_color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    primary_color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    font_size: int | None = Field(None, ge=24, le=200)
    position: Literal["center", "lower_third", "bottom", "seam"] | None = None   # None = automático según el diseño
    uppercase: bool | None = None


class Hook(BaseModel):
    text: str = Field("", max_length=90)
    seconds: float = Field(3.0, ge=0.5, le=15)


class Watermark(BaseModel):
    text: str = Field("", max_length=40)
    corner: Literal["tl", "tr", "bl", "br"] = "br"


class EditSpec(BaseModel):
    trim_start: float = Field(0.0, ge=0)
    trim_end: float | None = None                    # None = hasta el final de la fuente
    layout: Literal["auto", "split", "fit"] = "auto"
    facecam: RectCfg | None = None                   # None = autodetectar si layout=split
    facecam_position: Literal["top", "bottom"] = "top"
    facecam_height_pct: int = Field(34, ge=15, le=60)
    gameplay: Literal["center", "cover", "contain"] = "center"
    gameplay_x: float | None = Field(None, ge=0, le=1)
    gameplay_zone: RectCfg | None = None
    subtitles: SubStyle = SubStyle()
    hook: Hook = Hook()
    watermark: Watermark = Watermark()
    words: list[EditWord] = Field(default_factory=list, max_length=3000)

    @model_validator(mode="after")
    def _trim(self):
        if self.trim_end is not None and self.trim_end - self.trim_start < 1.0:
            raise ValueError("el recorte debe durar al menos 1 s")
        if self.trim_end is not None and self.trim_end - self.trim_start > 180:
            raise ValueError("el recorte no puede superar 180 s")
        return self


def default_spec(cfg: Config, words: list[EditWord], trim_start: float, trim_end: float | None) -> EditSpec:
    lc = cfg.layout
    return EditSpec(trim_start=trim_start, trim_end=trim_end, layout=lc.mode, facecam=lc.facecam,
                    facecam_position=lc.facecam_position, facecam_height_pct=lc.facecam_height_pct,
                    gameplay=lc.gameplay, gameplay_x=lc.gameplay_x, gameplay_zone=lc.gameplay_zone, subtitles=SubStyle(enabled=cfg.subtitles.enabled), words=words)
