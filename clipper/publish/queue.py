"""Cola persistente (SQLite) y worker de publicación con límites, reintentos y modo revisión."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

import httpx

from ..config import STATE, Config
from . import metadata
from .base import AuthError, PublishError, PublishResult, Publisher, RetryableError, TokenStore
from .instagram import Instagram
from .tiktok import TikTok
from .webhook import Webhook
from .youtube import YouTube

log = logging.getLogger("clipper.publish")
STATUSES = ("pending_review", "queued", "uploading", "published", "failed", "rejected")
SCHEMA = """CREATE TABLE IF NOT EXISTS publications(
 id INTEGER PRIMARY KEY AUTOINCREMENT, clip TEXT NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL,
 title TEXT, description TEXT, attempts INTEGER DEFAULT 0, url TEXT, remote_id TEXT, error TEXT,
 created REAL, updated REAL, not_before REAL)"""


class PublishQueue:
    def __init__(self, cfg: Config, db_path: Path | None = None, tokens: TokenStore | None = None,
                 http: httpx.Client | None = None):
        self.cfg = cfg
        self.tokens = tokens or TokenStore()
        db_path = db_path or STATE / "data" / "publish.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute(SCHEMA)
            self.db.execute("UPDATE publications SET status='queued' WHERE status='uploading'")  # caída a medias
            self.db.commit()
        self.providers: dict[str, Publisher] = {
            cls.name: cls(cfg, self.tokens, http) for cls in (YouTube, TikTok, Instagram, Webhook)}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_event = lambda kind, detail: None

    # ------------------------------------------------------------------ hilo
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="publisher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ alta
    def destinations(self) -> list[str]:
        return [n for n, p in self.providers.items() if getattr(self.cfg.publish, n).enabled]

    def on_clip(self, clip_path: Path, meta: dict, manual: bool = False) -> list[int]:
        """Llamado por el motor al terminar un clip. Crea una entrada por destino activado."""
        pc = self.cfg.publish
        if not pc.enabled or (manual and not pc.include_manual):
            return []
        return [self.enqueue(clip_path.name, n, meta) for n in self.destinations()]

    def enqueue(self, clip: str, platform: str, meta: dict | None = None, approved: bool | None = None) -> int:
        """approved=None => según el modo del destino; True => directo a la cola (acción explícita del usuario)."""
        prov = self.providers[platform]
        if meta is None:
            j = self.clip_path(clip).with_suffix(".json")
            meta = json.loads(j.read_text()) if j.exists() else {}
        title, desc = metadata.render(self.cfg.publish, meta, prov.title_limit(), prov.description_limit())
        if self.cfg.ai.auto_metadata and self.cfg.ai.llm.enabled and meta.get("transcript"):
            try:
                title, desc = self._ai_text(prov, meta)
            except Exception as e:  # noqa: BLE001  — si la IA falla se queda la plantilla
                log.warning("IA no disponible para el título, se usa la plantilla: %s", e)
        auto = approved if approved is not None else prov.dest.mode == "auto"
        now = time.time()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO publications(clip,platform,status,title,description,created,updated) VALUES(?,?,?,?,?,?,?)",
                (clip, platform, "queued" if auto else "pending_review", title, desc, now, now))
            self.db.commit()
        self.on_event("publish", f"{prov.label}: {clip} → {'en cola' if auto else 'pendiente de revisión'}")
        return cur.lastrowid

    def _ai_text(self, prov: Publisher, meta: dict) -> tuple[str, str]:
        from .. import ai
        sug = ai.suggest(self.cfg.ai.llm, meta.get("transcript", ""), meta.get("reason", ""), meta.get("detail", ""))
        desc = (sug["description"] + "\n\n" + " ".join(sug["hashtags"])).strip()
        return sug["title"][:prov.title_limit()], desc[:prov.description_limit()]

    def regenerate(self, pid: int) -> dict:
        """Vuelve a generar título y descripción con la IA para una entrada aún editable."""
        it = self.get(pid)
        if not it or it["status"] not in ("pending_review", "failed", "queued"):
            raise ValueError("solo se puede regenerar antes de publicar")
        j = self.clip_path(it["clip"]).with_suffix(".json")
        meta = json.loads(j.read_text()) if j.exists() else {}
        title, desc = self._ai_text(self.providers[it["platform"]], meta)
        self._set(pid, title=title, description=desc)
        return self.get(pid)

    def clip_path(self, clip: str) -> Path:
        if Path(clip).name != clip:
            raise ValueError("nombre de clip inválido")
        return self.cfg.path(self.cfg.clip.output_dir) / clip

    # ------------------------------------------------------------------ gestión
    def _set(self, pid: int, **f) -> None:
        f["updated"] = time.time()
        with self.lock:
            self.db.execute(f"UPDATE publications SET {','.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), pid))
            self.db.commit()

    def get(self, pid: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM publications WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

    def list(self, limit: int = 60) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM publications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def approve(self, pid: int) -> None:
        self._transition(pid, ("pending_review", "failed", "rejected"), "queued", attempts=0, error=None, not_before=None)

    def reject(self, pid: int) -> None:
        self._transition(pid, ("pending_review", "queued", "failed"), "rejected")

    def edit(self, pid: int, title: str | None, description: str | None) -> None:
        it = self.get(pid)
        if not it or it["status"] not in ("pending_review", "failed", "queued"):
            raise ValueError("solo se puede editar antes de publicar")
        prov = self.providers[it["platform"]]
        f = {}
        if title is not None:
            f["title"] = title.strip()[:prov.title_limit()]
        if description is not None:
            f["description"] = description.strip()[:prov.description_limit()]
        if f:
            self._set(pid, **f)

    def delete(self, pid: int) -> None:
        it = self.get(pid)
        if it and it["status"] == "uploading":
            raise ValueError("se está subiendo ahora mismo")
        with self.lock:
            self.db.execute("DELETE FROM publications WHERE id=?", (pid,))
            self.db.commit()

    def _transition(self, pid: int, allowed: tuple, to: str, **extra) -> None:
        it = self.get(pid)
        if not it:
            raise KeyError(pid)
        if it["status"] not in allowed:
            raise ValueError(f"no se puede pasar de '{it['status']}' a '{to}'")
        self._set(pid, status=to, **extra)

    # ------------------------------------------------------------------ worker
    def _loop(self) -> None:
        while not self._stop.wait(3):
            try:
                self.process_one()
            except Exception:  # noqa: BLE001
                log.exception("fallo en el worker de publicación")

    def _allowed_now(self, platform: str) -> bool:
        pc = self.cfg.publish
        day0 = time.time() - 86400
        with self.lock:
            n, last = self.db.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated),0) FROM publications WHERE platform=? AND status='published' AND updated>?",
                (platform, day0)).fetchone()
        return n < pc.max_per_day and time.time() - last >= pc.min_gap_minutes * 60

    def process_one(self) -> int | None:
        """Procesa como mucho una publicación lista. Devuelve su id (o None si no había nada que hacer)."""
        if not self.cfg.publish.enabled:
            return None
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM publications WHERE status='queued' AND (not_before IS NULL OR not_before<=?) ORDER BY id",
                (time.time(),)).fetchall()
        for r in map(dict, rows):
            if self._allowed_now(r["platform"]):
                self._publish(r)
                return r["id"]
        return None

    def _publish(self, it: dict) -> None:
        pid, prov = it["id"], self.providers[it["platform"]]
        self._set(pid, status="uploading")
        path = self.clip_path(it["clip"])
        try:
            if not path.exists():
                raise PublishError("el archivo del clip ya no existe")
            if self.cfg.publish.dry_run:
                res = PublishResult("dry-run", f"dry-run://{prov.name}/{it['clip']}")
            else:
                if not prov.dest.enabled:
                    raise PublishError(f"{prov.label} está desactivado")
                j = path.with_suffix(".json")
                meta = json.loads(j.read_text()) if j.exists() else {}
                res = prov.publish(path, it["title"] or "", it["description"] or "", meta)
            self._set(pid, status="published", url=res.url, remote_id=res.id, error=None)
            self.on_event("publish", f"{prov.label}: publicado {res.url or it['clip']}")
        except (AuthError, PublishError) as e:
            self._set(pid, status="failed", error=str(e)[:500], attempts=it["attempts"] + 1)
            self.on_event("error", f"{prov.label}: {e}")
        except (RetryableError, httpx.TransportError) as e:
            n = it["attempts"] + 1
            if n >= self.cfg.publish.max_attempts:
                self._set(pid, status="failed", error=f"{e} (sin más reintentos)"[:500], attempts=n)
                self.on_event("error", f"{prov.label}: {e}")
            else:
                self._set(pid, status="queued", error=str(e)[:500], attempts=n, not_before=time.time() + 60 * 2 ** n)
                self.on_event("publish", f"{prov.label}: fallo transitorio, reintento en {2 ** n} min")
        except Exception as e:  # noqa: BLE001
            log.exception("publicación falló")
            self._set(pid, status="failed", error=f"{type(e).__name__}: {e}"[:500], attempts=it["attempts"] + 1)
            self.on_event("error", f"{prov.label}: {e}")

    # ------------------------------------------------------------------ estado
    def status(self) -> dict:
        pc = self.cfg.publish
        dests = {}
        for n, p in self.providers.items():
            d = getattr(pc, n)
            dests[n] = {"label": p.label, "enabled": d.enabled, "mode": d.mode, "configured": p.configured(),
                        "connected": p.connected(), "oauth": p.needs_oauth,
                        "redirect_uri": getattr(p, "redirect_uri", lambda: "")() if p.needs_oauth else ""}
        return {"enabled": pc.enabled, "dry_run": pc.dry_run, "destinations": dests, "items": self.list()}
