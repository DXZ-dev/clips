"""Subtítulos ASS palabra a palabra: la palabra activa se resalta de color, con borde negro grueso."""
from __future__ import annotations

import textwrap
from dataclasses import dataclass

from .config import SubtitlesCfg

# Estilos listos para usar (inspirados en los de Submagic/Opus). Cada uno sobrescribe campos de SubtitlesCfg.
PRESETS: dict[str, dict] = {
    "clasico": {},
    "hormozi": {"primary_color": "#FFFFFF", "highlight_color": "#00E676", "font_size": 96, "outline": 11, "uppercase": True, "max_words_per_line": 2},
    "beast": {"primary_color": "#FFE600", "highlight_color": "#FFFFFF", "font_size": 104, "outline": 12, "uppercase": True, "max_words_per_line": 2},
    "neon": {"primary_color": "#FFFFFF", "highlight_color": "#00E5FF", "outline_color": "#1A0033", "font_size": 88, "outline": 10},
    "fuego": {"primary_color": "#FFFFFF", "highlight_color": "#FF5A1F", "font_size": 92, "outline": 10, "max_words_per_line": 3},
    "minimal": {"primary_color": "#FFFFFF", "highlight_color": "#FFFFFF", "font_size": 64, "outline": 5, "uppercase": False, "max_words_per_line": 4},
}


def with_preset(cfg: SubtitlesCfg, name: str | None, **overrides) -> SubtitlesCfg:
    """Copia de cfg con un preset y ajustes puntuales aplicados (los None se ignoran)."""
    data = cfg.model_dump()
    data.update(PRESETS.get(name or "clasico", {}))
    data.update({k: v for k, v in overrides.items() if v is not None})
    return SubtitlesCfg.model_validate(data)


@dataclass
class Word:
    start: float   # segundos relativos al inicio del clip
    end: float
    text: str


def _color(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}&".upper()


def _ts(t: float) -> str:
    t = max(t, 0.0)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _clean(text: str, upper: bool) -> str:
    t = text.strip().replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")
    return t.upper() if upper else t


def _pages(words: list[Word], cfg: SubtitlesCfg) -> list[list[Word]]:
    """Agrupa palabras en 'páginas' (hasta max_words_per_line*max_lines) cortando en pausas/puntuación."""
    cap = max(1, cfg.max_words_per_line * cfg.max_lines)
    pages, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        gap = words[i + 1].start - w.end if i + 1 < len(words) else 0
        if len(cur) >= cap or gap > 0.7 or w.text.rstrip().endswith((".", "?", "!", "…")):
            pages.append(cur)
            cur = []
    if cur:
        pages.append(cur)
    return pages


def build_ass(words: list[Word], cfg: SubtitlesCfg, width: int = 1080, height: int = 1920,
              seam_y: int | None = None, hook: tuple[str, float] | None = None,
              watermark: tuple[str, str] | None = None, duration: float = 0.0) -> str:
    """`seam_y`: y (px) de la unión cámara/gameplay para position='seam'. `hook`=(texto, segundos) rótulo
    de gancho arriba; `watermark`=(texto, esquina tl|tr|bl|br)."""
    pos = "center" if cfg.position == "seam" and seam_y is None else cfg.position
    align, margin_v = {"center": (5, 0), "lower_third": (2, int(height * 0.27)),
                       "bottom": (2, int(height * 0.12)), "seam": (5, 0)}[pos]
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{cfg.font},{cfg.font_size},{_color(cfg.primary_color)},{_color(cfg.primary_color)},{_color(cfg.outline_color)},&H64000000&,{-1 if cfg.bold else 0},0,0,0,100,100,0,0,1,{cfg.outline},{cfg.shadow},{align},60,60,{max(0, margin_v + cfg.offset_y)},1

Style: Hook,{cfg.font},{int(cfg.font_size * 0.72)},&H00000000&,&H00000000&,&H00FFFFFF&,&H00FFFFFF&,-1,0,0,0,100,100,0,0,3,18,0,8,70,70,{int(height * 0.07)},1
Style: Mark,{cfg.font},{int(cfg.font_size * 0.42)},&H00FFFFFF&,&H00FFFFFF&,&H00000000&,&H00000000&,-1,0,0,0,100,100,0,0,1,3,0,{{MARKALIGN}},40,40,{int(height * 0.035)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    hi, base = _color(cfg.highlight_color), _color(cfg.primary_color)
    events = []
    words = [w for w in words if w.text.strip()]
    for page in _pages(words, cfg):
        for i, active in enumerate(page):
            start = active.start
            end = page[i + 1].start if i + 1 < len(page) else active.end + 0.25
            if end <= start:
                end = start + 0.1
            # reparte en líneas de max_words_per_line palabras
            parts = []
            for j, w in enumerate(page):
                tok = _clean(w.text, cfg.uppercase)
                parts.append(f"{{\\c{hi}}}{tok}{{\\c{base}}}" if j == i else tok)
                if (j + 1) % cfg.max_words_per_line == 0 and j + 1 < len(page):
                    parts.append("\\N")
                elif j + 1 < len(page):
                    parts.append(" ")
            text = "".join(parts).replace(" \\N ", "\\N").replace(" \\N", "\\N")
            anchor = seam_y if (cfg.position == "seam" and seam_y is not None) else (height // 2 if cfg.position == "center" else None)
            if anchor is not None:                       # centro / unión cámara-gameplay: posición absoluta (+ desplazamiento vertical)
                text = f"{{\\an5\\pos({width // 2},{anchor - cfg.offset_y})}}" + text
            events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{text}")
    mark_align = {"tl": 7, "tr": 9, "bl": 1, "br": 3}.get((watermark or ("", "br"))[1], 9)
    hdr = hdr.replace("{MARKALIGN}", str(mark_align))
    end = _ts(duration or 36000)
    if hook and hook[0].strip():
        lines = "\\N".join(textwrap.wrap(_clean(hook[0], False), 20))   # WrapStyle 2: hay que partir a mano
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(max(0.5, hook[1]))},Hook,,0,0,0,,{lines}")
    if watermark and watermark[0].strip():
        events.append(f"Dialogue: 2,{_ts(0)},{end},Mark,,0,0,0,,{{\\alpha&H66&}}{_clean(watermark[0], False)}")
    return hdr + "\n".join(events) + "\n"
