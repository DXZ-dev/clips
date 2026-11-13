"""Vídeos ya grabados (subidos o por enlace): se buscan los momentos destacados y se sacan clips.

Usa los mismos detectores que el directo (palabras clave, risas, picos de volumen, cadencia) y los puntúa para
quedarse con los mejores momentos, separados entre sí. Todos los clips de un vídeo comparten una única fuente."""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from .audio import SR
from .config import Config
from .detect.transcriber import LiveTranscriber
from .detect.triggers import CadenceDetector, EnergyDetector, KeywordMatcher, Trigger
from .editor import finalize, out_dir, src_dir
from .ffutil import duration as ffduration, find_tool, require
from .spec import EditWord, default_spec
from .subtitles import Word

log = logging.getLogger("clipper.vod")
WEIGHT = {"keyword": 3.0, "laughter": 2.5, "energy": 2.0, "cadence": 1.5}
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts", ".flv", ".wmv"}


class Cancelled(Exception):
    pass


@dataclass
class Moment:
    t: float
    score: float
    kinds: list[str]
    detail: str = ""


# ------------------------------------------------------------------ análisis
def iter_audio(src: str, chunk_s: float = 30.0) -> Iterator[tuple[float, np.ndarray]]:
    """Audio 16 kHz mono por trozos (no carga el vídeo entero en memoria)."""
    p = subprocess.Popen([require("ffmpeg"), "-v", "error", "-nostdin", "-i", src, "-vn", "-ac", "1", "-ar", str(SR),
                          "-f", "s16le", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n, t = int(SR * chunk_s) * 2, 0.0
    try:
        while True:
            buf = p.stdout.read(n)
            if not buf:
                break
            buf = buf[: len(buf) // 2 * 2]
            a = np.frombuffer(buf, np.int16).astype(np.float32) / 32768.0
            yield t, a
            t += len(a) / SR
    finally:
        p.stdout.close()
        p.terminate()
        p.wait()


def _weight(tr: Trigger) -> float:
    return WEIGHT.get(tr.kind, 1.0) * tr.strength


def select_moments(triggers: list[Trigger], max_clips: int, min_gap: float, merge_window: float) -> list[Moment]:
    """Agrupa disparos cercanos en momentos, los puntúa (el mejor + 0,6·resto) y elige los mejores separados `min_gap`."""
    clusters: list[list[Trigger]] = []
    for tr in sorted(triggers, key=lambda x: x.t):
        if clusters and tr.t - clusters[-1][-1].t <= merge_window:
            clusters[-1].append(tr)
        else:
            clusters.append([tr])
    moments = []
    for c in clusters:
        ws = sorted((_weight(x) for x in c), reverse=True)
        best = max(c, key=_weight)
        details = list(dict.fromkeys(x.detail for x in c if x.detail))
        moments.append(Moment(best.t, round(ws[0] + 0.6 * sum(ws[1:]), 2), sorted({x.kind for x in c}, key=lambda k: -WEIGHT.get(k, 1.0)), "; ".join(details[:3])))   # el motivo principal primero
    chosen: list[Moment] = []
    for m in sorted(moments, key=lambda m: -m.score):
        if all(abs(m.t - x.t) >= min_gap for x in chosen):
            chosen.append(m)
        if len(chosen) >= max_clips:
            break
    return sorted(chosen, key=lambda m: m.t)


def clip_window(m: Moment, duration: float, seconds: float, post_roll: float, words: list[Word] | None = None) -> tuple[float, float]:
    """Ventana [t0, t1] del clip: acaba `post_roll` s después del momento y se ajusta al inicio/fin de frase más cercanos."""
    seconds = min(seconds, duration)
    t1 = min(duration, m.t + post_roll)
    t0 = max(0.0, t1 - seconds)
    t1 = min(duration, t0 + seconds)
    if words:
        starts = [w.start for w in words if t0 - 2.5 <= w.start <= t0 + 0.5]
        ends = [w.end for w in words if t1 - 0.5 <= w.end <= t1 + 2.5]
        if starts:
            t0 = min(starts, key=lambda x: abs(x - t0))
        if ends:
            t1 = min(duration, min(ends, key=lambda x: abs(x - t1)))
    return round(t0, 2), round(max(t1, t0 + 3), 2)


def find_highlights(cfg: Config, src: str, use_transcription: bool = True, progress: Callable[[float, str], None] = lambda p, m: None,
                    cancelled: Callable[[], bool] = lambda: False, transcriber_factory=None) -> tuple[list[Trigger], list[Word], float]:
    """Recorre el vídeo y devuelve (todos los disparos, todas las palabras, duración)."""
    dur = ffduration(src) or 1.0
    energy, cadence, keywords = EnergyDetector(cfg), CadenceDetector(cfg), KeywordMatcher(cfg)
    trigs: list[Trigger] = []
    words: list[Word] = []

    def on_words(ws: list[Word]) -> None:
        words.extend(ws)
        trigs.extend(keywords.feed(ws) + cadence.feed(words[-80:]))

    tr = None
    if use_transcription and cfg.transcribe.enabled:
        tr = (transcriber_factory or LiveTranscriber)(cfg, on_words=on_words)
        progress(0.0, "Cargando el modelo de transcripción…")
        tr.load()
    for t0, a in iter_audio(src):
        if cancelled():
            raise Cancelled()
        trigs.extend(energy.feed(a, t0))
        if tr:
            tr.feed(a, t0)
        done = min(1.0, (t0 + len(a) / SR) / dur)
        progress(done, f"Analizando el audio… {int(done * 100)} %")
    if tr:
        tr.flush()
    return trigs, words, dur


# ------------------------------------------------------------------ utilidades de ficheros
def _codecs(path: Path) -> tuple[str, str]:
    out = subprocess.run([require("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    v = a = ""
    for s in (json.loads(out or "{}").get("streams") or []):
        if s.get("codec_type") == "video" and not v:
            v = s.get("codec_name", "")
        if s.get("codec_type") == "audio" and not a:
            a = s.get("codec_name", "")
    return v, a


def normalize_to_mp4(src: Path, dst: Path) -> None:
    """MP4 reproducible en el navegador: copia sin recodificar si ya es H.264/AAC (o MP3); si no, recodifica."""
    v, a = _codecs(src)
    if not v:
        raise RuntimeError("El archivo no parece un vídeo válido.")
    if v == "h264" and a in ("aac", "mp3", ""):
        if src.suffix.lower() == ".mp4":
            os.replace(src, dst)
            return
        cmd = ["-c", "copy"]
    else:
        cmd = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k"]
    r = subprocess.run([require("ffmpeg"), "-y", "-v", "error", "-nostdin", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", *cmd,
                        "-movflags", "+faststart", "-f", "mp4", str(dst)], capture_output=True, text=True)
    if r.returncode != 0:
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"No se pudo convertir el vídeo: {r.stderr.strip()[-300:]}")
    src.unlink(missing_ok=True)


# ------------------------------------------------------------------ trabajos
@dataclass
class VideoJob:
    name: str
    mode: str = "highlights"        # highlights | asis
    opts: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: str = "uploading"       # uploading | queued | downloading | converting | analyzing | rendering | done | error | cancelled
    progress: float = 0.0
    message: str = ""
    clips: list[str] = field(default_factory=list)
    error: str | None = None
    created: float = field(default_factory=time.time)
    path: str | None = None         # archivo recibido (subida) pendiente de procesar
    url: str | None = None


class VideoManager:
    def __init__(self, engine):
        self.e = engine
        self.cfg: Config = engine.cfg
        self.jobs: dict[str, VideoJob] = {}
        self._q: queue.Queue[str] = queue.Queue()
        self._cancel: set[str] = set()
        self._stop = threading.Event()
        self.transcriber_factory = None

    def start(self) -> None:
        threading.Thread(target=self._loop, name="videos", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # -- alta
    def tmp_dir(self) -> Path:
        d = src_dir(self.cfg)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def begin_upload(self, filename: str, mode: str, opts: dict) -> tuple[VideoJob, Path]:
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            raise ValueError(f"Formato no admitido ({ext or 'sin extensión'}). Admitidos: {', '.join(sorted(ALLOWED_EXT))}")
        job = VideoJob(name=Path(filename).name[:120], mode=mode, opts=opts)
        path = self.tmp_dir() / f"upload_{job.id}{ext}.part"
        job.path = str(path)
        self.jobs[job.id] = job
        return job, path

    def finish_upload(self, job: VideoJob) -> None:
        final = Path(job.path).with_suffix("")          # quita .part
        os.replace(job.path, final)
        job.path, job.status, job.message = str(final), "queued", "En cola"
        self._q.put(job.id)

    def submit_url(self, url: str, mode: str, opts: dict) -> VideoJob:
        if not re.match(r"^https?://[^\s]+$", url) or url.startswith("-"):
            raise ValueError("Pega un enlace http(s) válido.")
        if not find_tool("yt-dlp"):
            raise ValueError("Para analizar enlaces hace falta yt-dlp (viene en requirements.txt; en Docker ya está incluido).")
        job = VideoJob(name=url[:120], mode=mode, opts=opts, url=url, status="queued", message="En cola")
        self.jobs[job.id] = job
        self._q.put(job.id)
        return job

    def cancel(self, jid: str) -> None:
        self._cancel.add(jid)
        j = self.jobs.get(jid)
        if j and j.status in ("uploading", "queued"):
            j.status, j.message = "cancelled", "Cancelado"
            if j.path:
                Path(j.path).unlink(missing_ok=True)

    def remove(self, jid: str) -> None:
        j = self.jobs.get(jid)
        if j and j.status in ("analyzing", "rendering", "downloading", "converting"):
            raise ValueError("Está en curso: cancélalo primero.")
        self.jobs.pop(jid, None)

    def list(self) -> list[dict]:
        return [asdict(j) | {"path": None} for j in sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)[:30]]

    # -- ejecución
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                jid = self._q.get(timeout=1)
            except queue.Empty:
                continue
            job = self.jobs.get(jid)
            if not job or job.status == "cancelled":
                continue
            try:
                self._run(job)
            except Cancelled:
                job.status, job.message = "cancelled", "Cancelado"
            except Exception as e:  # noqa: BLE001
                job.status, job.error, job.message = "error", str(e)[:400], "Error"
                self.e.event("error", f"vídeo {job.name}: {e}")
                log.exception("fallo procesando vídeo")
            finally:
                self._cancel.discard(jid)

    def _check(self, job: VideoJob) -> None:
        if job.id in self._cancel:
            raise Cancelled()

    def _progress(self, job: VideoJob, lo: float, hi: float):
        def cb(p: float, msg: str = "") -> None:
            self._check(job)
            job.progress = round(lo + (hi - lo) * p, 3)
            if msg:
                job.message = msg
        return cb

    def _download(self, job: VideoJob) -> Path:
        d = self.tmp_dir()
        tmpl = d / f"dl_{job.id}.%(ext)s"
        from .links import cookies_args, explain_ytdlp, platform_of
        cmd = [find_tool("yt-dlp"), "--no-playlist", "--newline", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
               "--merge-output-format", "mp4", *cookies_args(self.cfg), "-o", str(tmpl), "--", job.url]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cb = self._progress(job, 0.0, 0.1)
        last = ""
        try:
            for line in p.stdout:
                last = line.strip() or last
                m = re.search(r"\[download\]\s+([\d.]+)%", line)
                if m:
                    cb(float(m.group(1)) / 100, f"Descargando… {float(m.group(1)):.0f} %")
        except Cancelled:
            p.terminate()
            raise
        finally:
            p.wait()
        files = sorted(d.glob(f"dl_{job.id}.*"))
        if p.returncode != 0 or not files:
            raise RuntimeError(explain_ytdlp(last, platform_of(job.url or "")))
        return files[0]

    def _run(self, job: VideoJob) -> None:
        cfg, opts = self.cfg, job.opts
        job.status, job.message = ("downloading", "Descargando…") if job.url else ("converting", "Preparando el vídeo…")
        raw = self._download(job) if job.url else Path(job.path)
        self._check(job)
        job.status, job.message, job.progress = "converting", "Preparando el vídeo…", 0.1
        shared = self.tmp_dir() / f"vod_{job.id}.src.mp4"
        normalize_to_mp4(raw, shared)
        job.path = None

        if job.mode == "asis":
            self._publish_as_is(job, shared)
            return

        use_tr = bool(opts.get("transcribe", cfg.vod.use_transcription))
        job.status = "analyzing"
        trigs, words, dur = find_highlights(cfg, str(shared), use_tr, self._progress(job, 0.1, 0.7),
                                            lambda: job.id in self._cancel, self.transcriber_factory)
        max_clips = int(opts.get("max_clips") or cfg.vod.max_clips)
        seconds = int(opts.get("clip_seconds") or cfg.vod.clip_seconds or cfg.triggers.auto_clip_seconds)
        moments = select_moments(trigs, max_clips, cfg.vod.min_gap_seconds, cfg.vod.merge_window_seconds)
        if not moments:
            job.status, job.progress = "done", 1.0
            job.message = "No se detectaron momentos destacados (prueba a activar la transcripción o a bajar el umbral de energía en Ajustes)."
            shared.unlink(missing_ok=True)
            return
        job.status = "rendering"
        for i, m in enumerate(moments, 1):
            self._check(job)
            t0, t1 = clip_window(m, dur, seconds, cfg.triggers.post_roll_seconds, words)
            near = [EditWord(start=w.start, end=w.end, text=w.text) for w in words if w.end > t0 - 60 and w.start < t1 + 60][:3000]
            spec = default_spec(cfg, near, t0, t1)
            stem = f"clip_{job.id}_{i:02d}_{m.kinds[0]}"
            job.message = f"Creando clip {i} de {len(moments)}…"
            out = out_dir(cfg) / f"{stem}.mp4"
            with self.e.render_lock:
                finalize(cfg, stem, shared, spec, out, {"reason": m.kinds[0], "detail": m.detail, "score": m.score,
                                                         "video": job.name, "at": round(m.t, 1)})
            job.clips.append(out.name)
            job.progress = round(0.7 + 0.3 * i / len(moments), 3)
            try:
                meta = json.loads(out.with_suffix(".json").read_text())
                self.e.publisher.on_clip(out, meta, manual=False)
            except Exception:  # noqa: BLE001
                log.exception("on_clip falló")
        if not cfg.editor.keep_sources:
            shared.unlink(missing_ok=True)
        job.status, job.progress, job.message = "done", 1.0, f"{len(job.clips)} clips creados"
        self.e.event("clip", f"vídeo «{job.name}»: {len(job.clips)} clips")

    def _publish_as_is(self, job: VideoJob, shared: Path) -> None:
        """Modo «subir y publicar»: el vídeo se publica tal cual en las redes elegidas (cola con la misma revisión/límites)."""
        cfg, opts = self.cfg, job.opts
        out = out_dir(cfg) / f"upload_{job.id}.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        os.replace(shared, out)
        title = str(opts.get("title") or Path(job.name).stem)[:100]
        desc = str(opts.get("description") or "")
        meta = {"reason": "upload", "detail": job.name, "duration": round(ffduration(str(out)), 2), "transcript": desc or title,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "editable": False}
        out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        job.clips.append(out.name)
        plats = [p for p in (opts.get("platforms") or []) if p in self.e.publisher.destinations()]
        if opts.get("platforms") and not cfg.publish.enabled:
            raise RuntimeError("La publicación está desactivada (Ajustes → Publicación).")
        for p in plats:
            pid = self.e.publisher.enqueue(out.name, p, meta, approved=True)
            self.e.publisher.edit(pid, title, desc or None)
        job.status, job.progress = "done", 1.0
        job.message = f"Guardado y en cola para: {', '.join(plats)}" if plats else "Guardado en tus clips"
