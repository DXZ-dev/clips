"""Historial de enlaces de directos usados (para volver a elegirlos con un clic)."""
from __future__ import annotations

import json
import threading
import time

from . import config as C

_lock = threading.Lock()
FILE = lambda: C.STATE / "streams.local.json"  # noqa: E731


def load() -> list[dict]:
    try:
        return json.loads(FILE().read_text())
    except (OSError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    p = FILE()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    tmp.replace(p)


def remember(url: str, url2: str = "") -> None:
    from .links import channel_of
    url = (url or "").strip()
    if not url.startswith("http"):
        return
    with _lock:
        items = [i for i in load() if not (i["url"] == url and i.get("url2", "") == url2)]
        try:
            info = channel_of(url)
        except Exception:  # noqa: BLE001
            info = {}
        items.insert(0, {"url": url, "url2": url2, "platform": info.get("platform", ""), "channel": info.get("channel", ""),
                         "last_used": time.strftime("%Y-%m-%d %H:%M")})
        _save(items[:50])


def forget(url: str) -> None:
    with _lock:
        _save([i for i in load() if i["url"] != url])
