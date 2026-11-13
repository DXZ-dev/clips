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
from .detect.visual import FPS as VIS_FPS, VisualDetector
from .ffutil import cuda_available, pick_encoder, resolve_device
from .pipeline import clip_from_range
from .publish.base import TokenStore
from . import updater
from .vod import VideoManager, make_moment
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
        self._auto_times: deque[float] = deque(maxlen=200)     # instantes (reloj) de los clips automáticos aceptados
        self.energy, self.cadence, self.keywords = EnergyDetector(cfg), CadenceDetector(cfg), KeywordMatcher(cfg)
        self.visual = VisualDetector(cfg, cam=cfg.layout.facecam, zone=cfg.layout.gameplay_zone)
        self._pending: list[Trigger] = []                     # disparos recientes que aún se están puntuando como un solo momento
        self._pending_since = 0.0
        self._plock = threading.Lock()
        self._vh: int | None = None
        self.paused = False                                   # pausa de la generación AUTOMÁTICA (los clips manuales siguen funcionando)
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
        self._start_chat()
        for fn in (self._feeder, self._transcribe_loop, self._worker, self._update_loop, self._decider):
            t = threading.Thread(target=fn, name=fn.__name__, daemon=True)
            t.start()
            self._threads.append(t)
        self.event("system", f"motor iniciado (cómputo: {self.compute_info()['whisper_device']}, "
                             f"encoder: {self.compute_info()['encoder']})")

    def _start_chat(self) -> None:
        from . import chat
        ch = chat.twitch_channel(self.cfg.source.url) if self.cfg.source.type == "live" and self.cfg.triggers.chat else None
        if not ch:
            return
        def now():
            b = self.reader.bounds()
            return b[1] if b else None
        chat.TwitchChat(ch, chat.ChatSpikes(self._auto, now), self._stop).start()
        self.event("system", f"escuchando el chat de Twitch de {ch} (picos = momento destacado)")

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
        if "layout" in changed:
            self.visual.cam, self.visual.zone = self.cfg.layout.facecam, self.cfg.layout.gameplay_zone
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

    class Busy(RuntimeError):
        pass

    def live_preview(self, seconds: float = 8.0) -> Path:
        """Previa 9:16 (540x960) de los últimos segundos del directo, con el diseño y los subtítulos actuales.
        No bloquea el render de clips: si ya hay un render en curso lanza Busy."""
        import subprocess
        from .editor import src_dir
        from .ffutil import duration as ffdur, require
        from .render import render_spec
        from .spec import EditWord, default_spec
        b = self.reader.bounds()
        if not b:
            raise RuntimeError("Todavía no llega señal del directo (el buffer está vacío).")
        t1 = b[1]
        t0 = max(b[0], t1 - seconds)
        if t1 - t0 < 2:
            raise RuntimeError("Aún hay muy poco directo en el buffer: espera unos segundos.")
        if not self.render_lock.acquire(blocking=False):
            raise Engine.Busy("Hay un clip renderizándose ahora mismo; inténtalo en unos segundos.")
        try:
            ci = self.reader.prepare_range(t0, t1)
            try:
                d = src_dir(self.cfg)
                d.mkdir(parents=True, exist_ok=True)
                src, out = d / "live_preview_src.mp4", d / "live_preview.mp4"
                r = subprocess.run([require("ffmpeg"), "-y", "-v", "error", "-nostdin", "-f", "concat", "-safe", "0", "-i",
                                    str(ci.workdir / "list.txt"), "-c", "copy", "-movflags", "+faststart", str(src)],
                                   capture_output=True, text=True)
                sdur = ffdur(str(src)) if r.returncode == 0 else 0.0
                if sdur < 1:
                    raise RuntimeError("No se pudo preparar la previa (el buffer aún no tiene vídeo válido).")
                seg0 = ci.seg_start
                words = [EditWord(start=max(0.0, w.start - seg0), end=max(0.0, w.end - seg0), text=w.text)
                         for w in self.store.between(t0, t1)]
                ts = max(0.0, min(t0 - seg0, sdur - 1.0))
                if not words and self.cfg.subtitles.enabled:       # sin transcripción aún: texto de ejemplo para poder ajustar el estilo
                    demo = "Así se verán tus subtítulos en los clips del directo".split()
                    words = [EditWord(start=ts + 0.4 + i * 0.45, end=ts + 0.4 + (i + 1) * 0.45, text=w) for i, w in enumerate(demo)]
                spec = default_spec(self.cfg, words, ts, min(max(t1 - seg0, ts + 1.0), sdur))
                render_spec(self.cfg, src, spec, out, preview=True)
                return out
            finally:
                ci.cleanup()
        finally:
            self.render_lock.release()

    def pause(self) -> None:
        self.paused = True
        with self._plock:
            self._pending = []
        self.event("system", "generación automática de clips en PAUSA")

    def resume(self) -> None:
        self.paused = False
        self.event("system", "generación automática de clips reanudada")

    def _has_cam(self) -> bool:
        return self.cfg.layout.facecam is not None or self.cfg.layout.mode == "split"

    def _auto(self, tr: Trigger) -> None:
        """Recoge un disparo. No crea el clip al momento: espera unos segundos a que lleguen más señales del mismo momento
        (voz + volumen + movimiento + reacción…) y decide por puntuación (ver _flush_pending)."""
        if tr.kind not in ("action", "reaction"):                # esas dos son muy frecuentes: solo cuentan, no se anuncian
            self.event(tr.kind, tr.detail, at=round(tr.t, 1))
        if not self.cfg.triggers.auto_enabled or self.paused:
            return
        with self._plock:
            if not self._pending:
                self._pending_since = time.time()
            self._pending.append(tr)

    def _decider(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self._flush_pending()
            except Exception:  # noqa: BLE001
                log.exception("decisor de clips automáticos")

    def _flush_pending(self, force: bool = False) -> None:
        tc = self.cfg.triggers
        if self.paused:
            with self._plock:
                self._pending = []
            return
        with self._plock:
            if not self._pending or (not force and time.time() - self._pending_since < 6.0):
                return
            trigs, self._pending = self._pending, []
        m = make_moment(trigs, 1.0 if self._has_cam() else 1.5)          # sin cámara, la acción en pantalla pesa más
        if self.visual.bonus(m.t):
            m.score = round(m.score * 1.15, 2)                            # hay personajes en pantalla
        why = f"{'+'.join(m.kinds)}: {m.detail}".strip(": ")
        if m.score < tc.min_score:
            self.event("system", f"momento descartado ({why}): puntuación {m.score} < {tc.min_score}")
            return
        if m.t - self._last_auto < tc.cooldown_seconds:
            return
        now = time.time()
        if sum(1 for x in self._auto_times if now - x < 3600) >= tc.max_per_hour:
            self.event("system", f"límite de {tc.max_per_hour} clips automáticos por hora alcanzado: se omite este")
            return
        self._auto_times.append(now)
        self._last_auto = m.t
        t1 = m.t + tc.post_roll_seconds
        self.event("clip", f"momento destacado (puntuación {m.score}): {why}")
        self._submit(ClipJob(m.kinds[0], f"{m.detail} · puntuación {m.score}".strip(" ·"), t1 - tc.auto_clip_seconds, t1))

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
                if self.cfg.triggers.visual.enabled:
                    try:
                        if self._vh is None:
                            from .ffutil import probe
                            i = probe(str(sg.path))
                            self._vh = max(2, int(round(640 * i.height / i.width)) // 2 * 2)
                        from .tracking import read_frames
                        for tr in self.visual.feed(list(read_frames(["-i", str(sg.path)], VIS_FPS, 640, self._vh)), sg.start):
                            self._auto(tr)
                    except Exception:  # noqa: BLE001
                        log.exception("análisis visual del segmento")
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

    def _update_loop(self) -> None:
        """Cada 6 h (y poco después de arrancar) pregunta a GitHub si hay versión nueva. Solo avisa: nunca instala solo."""
        if self._stop.wait(45):
            return
        notified = None
        while not self._stop.is_set():
            if self.cfg.update.auto_check:
                try:
                    st = updater.check()
                    lat = st.get("latest") or {}
                    if st.get("update_available") and lat.get("sha") != notified:
                        notified = lat.get("sha")
                        self.event("system", f"Hay una actualización disponible: {lat.get('short', '')} {lat.get('subject', '')} (Ajustes → Actualizaciones)")
                except Exception:  # noqa: BLE001
                    log.exception("comprobación de actualizaciones")
            if self._stop.wait(6 * 3600):
                return

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
            from . import notify
            threading.Thread(target=notify.clip_ready, args=(self.cfg, out.name, meta), daemon=True).start()
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
            "paused": self.paused,
            "auto": {"enabled": self.cfg.triggers.auto_enabled, "max_per_hour": self.cfg.triggers.max_per_hour,
                     "last_hour": sum(1 for x in self._auto_times if time.time() - x < 3600)},
            "auth": bool(self.cfg.server.password_hash),
            "update": updater.summary(),
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
                        ("reason", "detail", "duration", "encoder", "transcript", "layout", "warning", "edited", "title", "platform", "channel", "score")},
                        "editable": (d / "_src" / f"{f.stem}.edit.json").exists()})
        return out
