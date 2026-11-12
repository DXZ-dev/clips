"""Webhook genérico: envía el clip a una URL tuya (n8n, Make, Zapier, bot propio…) que lo reparte donde quieras."""
from __future__ import annotations

import json
from pathlib import Path

from .base import PublishResult, Publisher, check


class Webhook(Publisher):
    name, label = "webhook", "Webhook"

    def configured(self) -> bool:
        return bool(self.dest.url)

    def title_limit(self):
        return 500

    def description_limit(self):
        return 4000

    def publish(self, path: Path, title: str, description: str, meta: dict) -> PublishResult:
        headers = {"Authorization": self.dest.auth_header} if self.dest.auth_header else {}
        with open(path, "rb") as f:
            r = check(self.http.post(self.dest.url, headers=headers, data={
                "title": title, "description": description, "clip": path.name,
                "meta": json.dumps(meta, ensure_ascii=False)}, files={"file": (path.name, f, "video/mp4")}),
                "Webhook")
        try:
            j = r.json()
        except ValueError:
            j = {}
        return PublishResult(str(j.get("id", path.name)), str(j.get("url", "")))
