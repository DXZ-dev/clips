"""Avisos al móvil cuando hay un clip listo: Discord (webhook) y/o Telegram (bot)."""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)


def _post(url: str, data: dict) -> None:
    req = urllib.request.Request(url, json.dumps(data).encode(), {"Content-Type": "application/json", "User-Agent": "clipper"})
    urllib.request.urlopen(req, timeout=15).read()


def clip_ready(cfg, name: str, meta: dict) -> None:
    n = cfg.notify
    if not n.enabled:
        return
    text = f"🎬 Clip listo: {meta.get('title') or name}\nCanal: {meta.get('platform', '')} {meta.get('channel', '')}\nMotivo: {meta.get('reason', '')}"
    if cfg.server.public_url:
        text += f"\n{cfg.server.public_url.rstrip('/')}/clips/{name}"
    try:
        if n.discord_webhook:
            _post(n.discord_webhook, {"content": text})
        if n.telegram_token and n.telegram_chat:
            _post(f"https://api.telegram.org/bot{n.telegram_token}/sendMessage", {"chat_id": n.telegram_chat, "text": text})
    except Exception as e:  # noqa: BLE001
        log.warning("aviso no enviado: %s", e)
