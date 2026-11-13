"""Motor: buffer + transcripción + disparadores + cola de clips. Lo usan la CLI y el panel web."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .audio import load_audio
from .buffer import BufferReader, RollingBuffer
from .config import Config
from .detect.transcriber import LiveTranscriber, WordStore
from .detect.triggers import CadenceDetector, EnergyDetector, KeywordMatcher, Trigger
from .ffutil import cuda_available, pick_encoder, resolve_device
from .pipeline import clip_from_range
from .publish.base import TokenStore
from .vod import VideoManager
from .publish.queue import PublishQueue
from .subtitles import Word

log = logging.getLogger("clipper.engine")


@dataclass
class ClipJob:
    reason: str
    detail: str
    t0: float
    t1: float
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: str = "queued"          # queued | waiting | rendering | done | error
    file: str | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)


class Engine:
    def __init__(self, cfg: Config, data_dir: Path | None = None):
        self.cfg = cfg
        self.publisher = PublishQueue(
            cfg, db_path=(data_dir / "publish.db") if data_dir else None,
            tokens=TokenStore(data_dir / "tokens.json") if data_dir else None)
        self.publisher.on_event = self.event
        self.videos = VideoManager(self)
        self.buffer = RollingBuffer(cfg)
        self.reader: BufferReader = self.buffer.reader
        self.store = WordStore()
        self.render_lock = threading.Lock()   # un render a la vez (CPU/GPU): clips automáticos y editor
        self.events: deque[dict] = deque(maxlen=200)
        self.jobs: dict[str, ClipJob] = {}
        self._q: queue.Queue[ClipJob] = queue.Queue()
        self._audio_q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._seen: set[str] = set()
        self._last_end = -1.0
        self._last_auto = -1e9
        self.energy, self.cadence, self.keywords = EnergyDetector(cfg), CadenceDetector(cfg), KeywordMatcher(cfg)
        self.transcriber: LiveTranscriber | None = None
        self.transcriber_error: str | None = None
        self.transcriber_ready = False
        if cfg.transcribe.enabled:
            self.transcriber = LiveTranscriber(cfg, on_words=self._on_words)

    # ------------------------------------------------------------------ ciclo de vida
    def start(self) -> None:
        self.buffer.start()
        self.publisher.start()
        self.videos.start()
        for fn in (self._feeder, self._transcribe_loop, self._worker):
            t = threading.Thread(target=fn, name=fn.__name__, daemon=True)
            t.start()
            self._threads.append(t)
        self.event("system", f"motor iniciado (cómputo: {self.compute_info()['whisper_device']}, "
                             f"encoder: {self.compute_info()['encoder']})")

    def stop(self) -> None:
        self._stop.set()
        self.publisher.stop()
        self.videos.stop()
        self.buffer.stop()

    def event(self, kind: str, detail: str = "", **extra) -> None:
        self.events.appendleft({"t": time.strftime("%H:%M:%S"), "kind": kind, "detail": detail, **extra})

    def set_device(self, device: str) -> dict:
        if device not in ("auto", "cuda", "cpu"):
            raise ValueError("device debe ser auto, cuda o cpu")
        self.cfg.compute.device = device
        if self.transcriber:
            self.transcriber.set_device(device)
        info = self.compute_info()
        self.event("system", f"dispositivo: {device} -> whisper en {info['whisper_device']}, encoder {info['encoder']}")
        return info

    def apply_settings(self, patch: dict) -> dict:
        """Aplica ajustes del panel en caliente y reacciona a lo que haya cambiado."""
        from .settings import apply_update, public_view
        changed = apply_update(self.cfg, patch)
        if "triggers" in changed:
            self.keywords = KeywordMatcher(self.cfg)
        if "transcribe" in changed or "compute" in changed:
            if self.cfg.transcribe.enabled:
                if self.transcriber is None:
                    self.transcriber = LiveTranscriber(self.cfg, on_words=self._on_words)
                else:
                    self.transcriber.reload()
                self.transcriber_error = None
            else:
                self.transcriber, self.transcriber_ready = None, False
        if "source" in changed or "buffer" in changed:
            self.buffer.stop()
            self.buffer = RollingBuffer(self.cfg)
            self.reader = self.buffer.reader
            self._seen.clear()
            self._last_end = -1.0
            self.buffer.start()
        if changed:
            self.event("system", "ajustes aplicados: " + ", ".join(changed))
        return {"changed": changed, "settings": public_view(self.cfg)}

    def compute_info(self) -> dict:
        return {"requested": self.cfg.compute.device,
                "whisper_device": resolve_device(self.cfg.compute.device),
                "cuda_available": cuda_available(),
                "encoder": pick_encoder(self.cfg.render.encoder, self.cfg.compute.device)}

    # ------------------------------------------------------------------ disparadores
    def manual_clip(self, seconds: int | None = None) -> ClipJob:
        seconds = seconds or self.cfg.clip.default_seconds
        b = self.reader.bounds()
        if not b:
            raise RuntimeError("El buffer está vacío: la ingesta aún no ha producido segmentos")
        job = ClipJob("manual", f"últimos {seconds}s", max(b[0], b[1] - seconds), b[1])
        self._submit(job)
        return job

    def _auto(self, tr: Trigger) -> None:
        tc = self.cfg.triggers
        self.event(tr.kind, tr.detail, at=round(tr.t, 1))
        if not tc.auto_enabled or tr.t - self._last_auto < tc.cooldown_seconds:
            return
        self._last_auto = tr.t
        t1 = tr.t + tc.post_roll_seconds
        self._submit(ClipJob(tr.kind, tr.detail, t1 - tc.auto_clip_seconds, t1))

    def _submit(self, job: ClipJob) -> None:
        self.jobs[job.id] = job
        self._q.put(job)
        self.event("clip", f"en cola ({job.reason}: {job.detail})", job=job.id)

    # ------------------------------------------------------------------ hilos
    def _on_words(self, words: list[Word]) -> None:
        self.store.add(words)
        for tr in self.keywords.feed(words) + self.cadence.feed(self.store.tail(80)):
            self._auto(tr)

    def _feeder(self) -> None:
        """Decodifica el audio de cada segmento nuevo; la energía se evalúa al momento."""
        while not self._stop.wait(1.0):
            segs = self.reader.segments()
            if not segs:
                continue
            if segs[-1].end < self._last_end - 1:      # el buffer se reinició (reconexión)
                self._seen.clear()
                self.store = WordStore()
            for sg in segs:
                if sg.path.name in self._seen:
                    continue
                self._seen.add(sg.path.name)
                self._last_end = max(self._last_end, sg.end)
                try:
                    audio = load_audio(["-i", str(sg.path)])
                except Exception:  # noqa: BLE001
                    log.exception("no se pudo decodificar %s", sg.path)
                    continue
                for tr in self.energy.feed(audio, sg.start):
                    self._auto(tr)
                if self.transcriber:
                    self._audio_q.put((audio, sg.start))
            if len(self._seen) > 400:
                self._seen = {s.path.name for s in segs}

    def _transcribe_loop(self) -> None:
        while not self._stop.is_set():
            if not self.transcriber:                   # desactivada (puede activarse desde Ajustes)
                self._stop.wait(1)
                continue
            if not self.transcriber.loaded:            # primera carga o cambio de dispositivo
                self.transcriber_ready = False
                try:
                    self.transcriber.load()
                    self.transcriber_ready, self.transcriber_error = True, None
                    tc = self.cfg.transcribe
                    self.event("system", "transcripción vía API: " + tc.api.base_url if tc.provider == "api"
                               else f"Whisper '{tc.model}' listo en {self.transcriber.device}")
                except Exception as e:  # noqa: BLE001
                    self.transcriber_error = f"{type(e).__name__}: {e}"
                    self.event("error", f"no se pudo cargar Whisper: {self.transcriber_error}")
                    log.exception("fallo cargando whisper")
                    if self._stop.wait(15):
                        return
                    continue
            try:
                audio, t0 = self._audio_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.transcriber.feed(audio, t0)
            except Exception as e:  # noqa: BLE001
                self.event("error", f"transcripción: {e}")
                log.exception("fallo transcribiendo")

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._run_job(job)
            except Exception as e:  # noqa: BLE001
                job.status, job.error = "error", str(e)
                self.event("error", f"clip {job.id}: {e}", job=job.id)
                log.exception("clip falló")

    def _run_job(self, job: ClipJob) -> None:
        job.status = "waiting"
        deadline = time.time() + 40
        while time.time() < deadline and not self._stop.is_set():       # espera al desenlace
            b = self.reader.bounds()
            if b and b[1] >= job.t1 - 0.5:
                break
            time.sleep(0.5)
        if self.transcriber and self.transcriber_ready:                  # y a que la transcripción alcance
            deadline = time.time() + 25
            while self.transcriber.processed_until < job.t1 and time.time() < deadline:
                time.sleep(0.5)
        b = self.reader.bounds()
        if not b:
            raise RuntimeError("buffer vacío")
        t0, t1 = max(job.t0, b[0]), min(job.t1, b[1])
        if t1 - t0 < 2:
            raise RuntimeError("tramo demasiado corto (ya salió del buffer)")
        m = self.cfg.editor.margin_seconds
        words = self.store.between(t0 - m, t1 + m)              # tiempos absolutos: la fuente guarda margen extra
        job.status = "rendering"
        with self.render_lock:
            out = clip_from_range(self.cfg, t0, t1, words, job.reason, job.detail)
        job.status, job.file = "done", out.name
        self.event("clip", f"listo: {out.name}", job=job.id)
        try:
            j = out.with_suffix(".json")
            meta = json.loads(j.read_text()) if j.exists() else {}
            self.publisher.on_clip(out, meta, manual=job.reason == "manual")
        except Exception as e:  # noqa: BLE001
            self.event("error", f"cola de publicación: {e}")
            log.exception("on_clip falló")

    # ------------------------------------------------------------------ estado
    def status(self) -> dict:
        st = self.buffer.status()
        b = self.reader.bounds()
        lag = None
        if self.transcriber and self.transcriber_ready and b:
            lag = round(max(0.0, b[1] - self.transcriber.processed_until), 1)
        return {
            "buffer": st, "compute": self.compute_info(),
            "transcriber": {"enabled": bool(self.transcriber), "ready": self.transcriber_ready,
                            "model": self.cfg.transcribe.model, "lag_seconds": lag,
                            "error": self.transcriber_error},
            "source": self.cfg.source.type, "queue": self._q.qsize(),
            "transcript": " ".join(w.text for w in self.store.tail(25)),
            "events": list(self.events)[:30],
            "videos_active": sum(1 for j in self.videos.jobs.values() if j.status not in ("done", "error", "cancelled")),
            "auto_enabled": self.cfg.triggers.auto_enabled,
            "publish": {"enabled": self.cfg.publish.enabled, "dry_run": self.cfg.publish.dry_run,
                        "pending_review": sum(1 for i in self.publisher.list() if i["status"] == "pending_review")},
            "durations": self.cfg.clip.duration_options, "default_seconds": self.cfg.clip.default_seconds,
        }

    def list_clips(self) -> list[dict]:
        d = self.cfg.path(self.cfg.clip.output_dir)
        out = []
        for f in sorted(d.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            meta = {}
            j = f.with_suffix(".json")
            if j.exists():
                try:
                    meta = json.loads(j.read_text())
                except ValueError:
                    pass
            out.append({"name": f.name, "size_mb": round(f.stat().st_size / 1e6, 2),
                        "mtime": f.stat().st_mtime, **{k: meta.get(k) for k in
                        ("reason", "detail", "duration", "encoder", "transcript", "layout", "warning", "edited")},
                        "editable": (d / "_src" / f"{f.stem}.edit.json").exists()})
        return out
