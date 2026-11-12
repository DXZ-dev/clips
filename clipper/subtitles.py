"""Subtítulos ASS palabra a palabra: la palabra activa se resalta de color, con borde negro grueso."""
from __future__ import annotations

from dataclasses import dataclass

from .config import SubtitlesCfg


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


def build_ass(words: list[Word], cfg: SubtitlesCfg, width: int = 1080, height: int = 1920) -> str:
    align, margin_v = {"center": (5, 0), "lower_third": (2, int(height * 0.27)),
                       "bottom": (2, int(height * 0.12))}[cfg.position]
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{cfg.font},{cfg.font_size},{_color(cfg.primary_color)},{_color(cfg.primary_color)},{_color(cfg.outline_color)},&H64000000&,-1,0,0,0,100,100,0,0,1,{cfg.outline},0,{align},60,60,{margin_v},1

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
            events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Default,,0,0,0,,{text}")
    return hdr + "\n".join(events) + "\n"
