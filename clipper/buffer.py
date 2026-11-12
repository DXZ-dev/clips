"""Buffer circular en disco: ffmpeg segmenta el directo en bloques cortos y se descartan los viejos.

Un proceso (RollingBuffer) escribe; cualquier otro proceso puede leer con BufferReader porque
el estado vive en el directorio (list.csv que mantiene ffmpeg).
"""
from __future__ import annotations

import csv
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .ffutil import require
from .sources import build_plan

log = logging.getLogger("clipper.buffer")
LIST_NAME = "list.csv"


@dataclass
class Segment:
    path: Path
    start: float
    end: float

    @property
    def dur(self) -> float:
        return self.end - self.start


@dataclass
class ClipInput:
    """Entrada lista para ffmpeg: concat de segmentos fijados con hardlinks + offset de inicio."""
    args: list[str]        # p. ej. ["-ss","12.5","-f","concat","-safe","0","-i","/x/list.txt"]
    duration: float        # duración efectiva del tramo
    workdir: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


class BufferReader:
    def __init__(self, directory: str | Path, segment_seconds: int = 5, window_seconds: int = 90):
        self.dir = Path(directory)
        self.seg_s = segment_seconds
        self.window = window_seconds

    def segments(self) -> list[Segment]:
        lst = self.dir / LIST_NAME
        out: list[Segment] = []
        if not lst.exists():
            return out
        for row in csv.reader(lst.read_text().splitlines()):
            if len(row) < 3:
                continue
            p = self.dir / row[0]
            if p.exists():
                try:
                    out.append(Segment(p, float(row[1]), float(row[2])))
                except ValueError:
                    pass
        return out

    def status(self) -> dict:
        segs = self.segments()
        lst = self.dir / LIST_NAME
        age = time.time() - lst.stat().st_mtime if lst.exists() else None
        return {
            "segments": len(segs),
            "buffered_seconds": round(sum(s.dur for s in segs), 1),
            "window_seconds": self.window,
            "live": age is not None and age < self.seg_s * 3 + 5,
            "last_segment_age": None if age is None else round(age, 1),
        }

    def bounds(self) -> tuple[float, float] | None:
        """(inicio del segmento más antiguo, fin del más reciente) en tiempo de stream."""
        segs = self.segments()
        return (segs[0].start, segs[-1].end) if segs else None

    def _pin(self, chosen: list[Segment], ss: float, dur: float) -> ClipInput:
        work = Path(tempfile.mkdtemp(prefix=f"clip_{uuid.uuid4().hex[:6]}_", dir=self.dir))
        lines = []
        for s in chosen:
            dst = work / s.path.name
            try:
                os.link(s.path, dst)
            except OSError:
                shutil.copy2(s.path, dst)
            lines.append(f"file '{dst}'")
        (work / "list.txt").write_text("\n".join(lines) + "\n")
        args = (["-ss", f"{ss:.3f}"] if ss > 0 else []) + [
            "-f", "concat", "-safe", "0", "-i", str(work / "list.txt")]
        return ClipInput(args, dur, work)

    def prepare(self, seconds: float) -> ClipInput:
        """Los últimos `seconds` del buffer (falta como mucho el segmento en curso: latencia ≤ segment_seconds)."""
        b = self.bounds()
        if not b:
            raise RuntimeError("El buffer está vacío (¿está corriendo la ingesta?)")
        return self.prepare_range(max(b[0], b[1] - seconds), b[1])

    def prepare_range(self, t0: float, t1: float) -> ClipInput:
        """Tramo [t0, t1] en tiempo de stream; se recorta a lo que haya en el buffer."""
        segs = [s for s in self.segments() if s.end > t0 and s.start < t1]
        if not segs:
            raise RuntimeError("El tramo pedido ya no está en el buffer")
        start = max(t0, segs[0].start)
        end = min(t1, segs[-1].end)
        return self._pin(segs, start - segs[0].start, end - start)


class RollingBuffer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = Path(cfg.buffer.dir)
        self.reader = BufferReader(self.dir, cfg.buffer.segment_seconds, cfg.buffer.window_seconds)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._procs: list[subprocess.Popen] = []
        self.last_error: str | None = None

    # ---- ciclo de vida
    def start(self) -> None:
        self._stop.clear()
        t1 = threading.Thread(target=self._ingest_loop, name="ingest", daemon=True)
        t2 = threading.Thread(target=self._prune_loop, name="prune", daemon=True)
        self._threads = [t1, t2]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
        for t in self._threads:
            t.join(timeout=5)

    def status(self) -> dict:
        return {**self.reader.status(), "error": self.last_error}

    # ---- ffmpeg
    def _ffmpeg_cmd(self, plan) -> list[str]:
        b = self.cfg.buffer
        cmd = [require("ffmpeg"), "-hide_banner", "-loglevel", "warning", "-nostdin", "-nostats",
               *plan.ffmpeg_input, "-map", "0:v:0", "-map", "0:a:0?"]
        if b.reencode:
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                    "-force_key_frames", f"expr:gte(t,n_forced*{b.segment_seconds})",
                    "-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-f", "segment", "-segment_time", str(b.segment_seconds), "-segment_format", "mpegts",
                "-segment_list", str(self.dir / LIST_NAME), "-segment_list_type", "csv",
                "-segment_list_flags", "live", "-reset_timestamps", "0",
                str(self.dir / "seg_%08d.ts")]
        return cmd

    def _wipe(self) -> None:
        """Borra solo lo que creamos nosotros (segmentos, lista, clips temporales), nunca el directorio entero."""
        self.dir.mkdir(parents=True, exist_ok=True)
        for f in self.dir.glob("seg_*.ts"):
            f.unlink(missing_ok=True)
        (self.dir / LIST_NAME).unlink(missing_ok=True)
        for d in self.dir.glob("clip_*"):
            shutil.rmtree(d, ignore_errors=True)

    def _ingest_loop(self) -> None:
        plan = build_plan(self.cfg)
        log.info("Ingesta: %s", plan.description)
        while not self._stop.is_set():
            self._wipe()  # tras una reconexión los timestamps se reinician: empezamos limpio
            cmd = self._ffmpeg_cmd(plan)
            log.debug("ffmpeg: %s", " ".join(cmd))
            prod = None
            try:
                if plan.producer:
                    prod = subprocess.Popen(plan.producer, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    self._procs.append(prod)
                ff = subprocess.Popen(cmd, stdin=prod.stdout if prod else None,
                                      stderr=subprocess.PIPE, text=True)
                if prod:
                    prod.stdout.close()
                self._procs.append(ff)
                _, err = ff.communicate()
                if ff.returncode not in (0, None) and not self._stop.is_set():
                    lines = (err or "").strip().splitlines()
                    self.last_error = lines[-1] if lines else f"ffmpeg salió con {ff.returncode}"
                    log.warning("ffmpeg terminó (%s): %s", ff.returncode, self.last_error)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.exception("fallo en ingesta")
            finally:
                for p in (prod,):
                    if p and p.poll() is None:
                        p.terminate()
            if not plan.restart or self._stop.is_set():
                break
            self._stop.wait(2.0)

    # ---- descarte de lo antiguo
    def _prune_loop(self) -> None:
        while not self._stop.wait(self.cfg.buffer.segment_seconds / 2):
            self.prune()

    def prune(self) -> int:
        segs = self.reader.segments()
        if not segs:
            return 0
        cutoff = segs[-1].end - self.cfg.buffer.window_seconds
        n = 0
        for s in segs:
            if s.end < cutoff:
                try:
                    s.path.unlink()
                    n += 1
                except FileNotFoundError:
                    pass
        # clips huérfanos (>10 min) por si un proceso murió a medias
        for d in self.dir.glob("clip_*"):
            if time.time() - d.stat().st_mtime > 600:
                shutil.rmtree(d, ignore_errors=True)
        return n
