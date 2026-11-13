"""Filtro de malas palabras para títulos y descripciones publicados (los subtítulos del vídeo no se tocan)."""
from __future__ import annotations

import re
import unicodedata

# raíces frecuentes en español e inglés; se comparan sin tildes y sin distinguir mayúsculas
_ROOTS = ("mierd", "puta", "puto", "putear", "joder", "jodid", "cabron", "cabrona", "coño", "cono", "pendej", "culo", "verga", "chingad",
          "chinga", "marica", "maricon", "gilipoll", "hijueputa", "hdp", "hijo de puta", "carajo", "pinche", "mamaguev", "mamahuev",
          "coger", "follar", "polla", "pija", "concha", "boludo", "pelotud", "zorra", "perra", "idiota", "imbecil", "estupid",
          "fuck", "shit", "bitch", "asshole", "dick", "pussy", "cunt", "bastard", "motherf", "nigg", "faggot", "whore", "slut")
_N = lambda s: "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")  # noqa: E731


def has_profanity(text: str) -> bool:
    return any(any(w.startswith(r) or r in w for r in _ROOTS) for w in re.findall(r"\w+", _N(text)))


def clean(text: str) -> str:
    """Quita las malas palabras del texto (no las tapa con asteriscos: simplemente no salen)."""
    if not text:
        return text
    out = []
    for tok in re.split(r"(\s+)", text):
        w = _N(re.sub(r"\W", "", tok))
        if w and any(w.startswith(r) or r in w for r in _ROOTS):
            continue
        out.append(tok)
    return re.sub(r"\s{2,}", " ", "".join(out)).strip(" ,;:-")
