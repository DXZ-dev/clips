"""Título/descripción a partir de plantillas y la transcripción del clip."""
from __future__ import annotations

import re

from ..config import PublishCfg


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def headline(transcript: str, reason: str, detail: str, limit: int = 70) -> str:
    t = re.sub(r"\s+", " ", transcript or "").strip()
    if t:
        first = re.split(r"(?<=[.!?¡¿])\s", t)[0].strip(" ¡¿")
        first = first or t
        if len(first) > limit:
            first = first[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
        return first
    return {"keyword": f"«{detail}»", "laughter": "Se lía una buena 😂", "energy": "Esto se pone intenso",
            "cadence": "No para de hablar"}.get(reason, "Momento del directo")


def render(cfg: PublishCfg, meta: dict, title_limit: int = 100, desc_limit: int = 2200) -> tuple[str, str]:
    tr = meta.get("transcript") or ""
    vars_ = _Safe(headline=headline(tr, meta.get("reason", ""), meta.get("detail", "")),
                  transcript=tr[:300], reason=meta.get("reason", ""), detail=meta.get("detail", ""),
                  hashtags=" ".join(cfg.hashtags), date=(meta.get("created") or "")[:10])
    title = cfg.title_template.format_map(vars_).strip()
    desc = cfg.description_template.format_map(vars_).strip()
    return title[:title_limit], desc[:desc_limit]
