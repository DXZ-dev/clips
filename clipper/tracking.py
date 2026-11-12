"""Auto-reframe 9:16: detección de rostros / hablante activo / movimiento + suavizado de cámara."""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

import cv2
import numpy as np

from .audio import envelope_db, load_audio
from .config import Config
from .ffutil import VideoInfo, require

log = logging.getLogger("clipper.tracking")


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2


# ------------------------------------------------------------------ detectores
class FaceDetector(Protocol):
    def detect(self, bgr: np.ndarray) -> list[Box]: ...


class NullDetector:
    def detect(self, bgr):
        return []


class HaarDetector:
    def __init__(self, extra_dir: Path | None = None):
        # Los wheels de OpenCV 5 ya no incluyen los cascades; también se busca en models/.
        name = "haarcascade_frontalface_default.xml"
        cands = [Path(cv2.data.haarcascades) / name] + ([extra_dir / name] if extra_dir else [])
        found = next((c for c in cands if c.exists()), None)
        if not found:
            raise FileNotFoundError(f"{name} no encontrado")
        self.c = cv2.CascadeClassifier(str(found))

    def detect(self, bgr):
        g = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
        return [Box(*map(float, r)) for r in self.c.detectMultiScale(g, 1.1, 5, minSize=(24, 24))]


class MediaPipeDetector:
    def __init__(self, model_path: str):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
        self._mp = mp
        self.det = vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=model_path), min_detection_confidence=0.5))

    def close(self):
        try:
            self.det.close()
        except Exception:  # noqa: BLE001
            pass

    def detect(self, bgr):
        rgb = np.ascontiguousarray(bgr[:, :, ::-1])
        res = self.det.detect(self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb))
        return [Box(d.bounding_box.origin_x, d.bounding_box.origin_y, d.bounding_box.width,
                    d.bounding_box.height) for d in res.detections]


def make_detector(cfg: Config) -> FaceDetector:
    kind, model = cfg.reframe.detector, cfg.path(cfg.reframe.mediapipe_model)
    if kind == "none":
        return NullDetector()
    if kind in ("auto", "mediapipe"):
        try:
            if not model.exists():
                raise FileNotFoundError(f"{model} (ejecuta scripts/download_models.sh)")
            return MediaPipeDetector(str(model))
        except Exception as e:  # noqa: BLE001
            if kind == "mediapipe":
                raise
            log.info("MediaPipe no disponible (%s); usando Haar", e)
    try:
        return HaarDetector(model.parent)
    except FileNotFoundError as e:
        log.warning("Sin detector de rostros (%s): se seguirá solo el movimiento", e)
        return NullDetector()


# ------------------------------------------------------------------ hablante activo
@dataclass
class _Track:
    box: Box
    score: float = 0.0
    missed: int = 0


class SpeakerSelector:
    """Elige a quién seguir: el rostro con más movimiento de boca mientras hay voz.

    La actividad labial se aproxima con el cambio de píxeles en la región de la boca entre
    fotogramas (barato y sin landmarks). Con histéresis para no saltar entre personas."""

    def __init__(self, cfg: Config, fps: float):
        self.c = cfg.reframe.speaker
        self.max_missed = int(fps * 1.0)
        self.tracks: list[_Track] = []
        self.current: _Track | None = None
        self.challenger: _Track | None = None
        self.count = 0

    @staticmethod
    def _mouth_activity(box: Box, gray, prev) -> float:
        H, W = gray.shape
        x0, x1 = int(max(0, box.x + .2 * box.w)), int(min(W, box.x + .8 * box.w))
        y0, y1 = int(max(0, box.y + .6 * box.h)), int(min(H, box.y + box.h))
        if x1 - x0 < 2 or y1 - y0 < 2:
            return 0.0
        return float(np.mean(cv2.absdiff(gray[y0:y1, x0:x1], prev[y0:y1, x0:x1])))

    def update(self, faces: list[Box], gray, prev, voice: bool) -> Box | None:
        seen = set()
        for f in faces:
            best, bd = None, 1e9
            for t in self.tracks:
                d = abs(t.box.cx - f.cx) + abs(t.box.y - f.y)
                if d < bd and id(t) not in seen:
                    best, bd = t, d
            if best is not None and bd < 0.8 * max(f.w, best.box.w):
                b = best.box   # posición suavizada para reducir el jitter del detector
                best.box = Box(*(0.5 * np.array([b.x, b.y, b.w, b.h]) + 0.5 * np.array([f.x, f.y, f.w, f.h])))
                best.missed = 0
            else:
                best = _Track(f)
                self.tracks.append(best)
            seen.add(id(best))
            act = self._mouth_activity(best.box, gray, prev) if prev is not None else 0.0
            best.score = 0.6 * best.score + 0.4 * act
        for t in list(self.tracks):
            if id(t) not in seen:
                t.missed += 1
                if t.missed > self.max_missed:
                    self.tracks.remove(t)
                    if t is self.current:
                        self.current = None
        visible = [t for t in self.tracks if t.missed == 0]
        if not visible:
            return None
        if self.current is None or self.current.missed > 0:
            self.current = max(visible, key=lambda t: (t.score, t.box.w * t.box.h))
            self.count = 0
        elif voice:
            cand = max(visible, key=lambda t: t.score)
            if cand is not self.current and cand.score > self.c.switch_ratio * self.current.score + 0.3:
                self.count = self.count + 1 if cand is self.challenger else 1
                self.challenger = cand
                if self.count >= self.c.switch_frames:
                    self.current, self.count = cand, 0
            else:
                self.count = 0
        return self.current.box


# ------------------------------------------------------------------ movimiento
def motion_center(gray, prev) -> float | None:
    """Centro de masa horizontal (0..1) del movimiento entre dos fotogramas; None si casi no hay."""
    if prev is None:
        return None
    d = cv2.GaussianBlur(cv2.absdiff(gray, prev), (5, 5), 0)
    mask = (d > 18).astype(np.float32)
    if mask.mean() < 0.002:
        return None
    cols = mask.sum(axis=0)
    return float((cols * np.arange(gray.shape[1])).sum() / cols.sum() / gray.shape[1])


# ------------------------------------------------------------------ lectura de frames
def read_frames(input_args: list[str], fps: int, w: int, h: int) -> Iterator[np.ndarray]:
    cmd = [require("ffmpeg"), "-v", "error", "-nostdin", *input_args, "-an",
           "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    n = w * h * 3
    try:
        while True:
            buf = p.stdout.read(n)
            if len(buf) < n:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        p.stdout.close()
        p.terminate()
        p.wait()


# ------------------------------------------------------------------ suavizado y trayectoria
@dataclass
class CropGeometry:
    crop_w: int
    crop_h: int
    y: int
    src_w: int
    src_h: int


def crop_geometry(info: VideoInfo, out_w: int, out_h: int) -> CropGeometry:
    aspect = out_w / out_h
    if info.height * aspect <= info.width:          # fuente más ancha que 9:16: altura completa
        cw, ch = int(info.height * aspect) // 2 * 2, info.height // 2 * 2
    else:                                            # fuente ya vertical/estrecha: ancho completo
        cw = info.width // 2 * 2
        ch = min(info.height, int(round(cw / aspect))) // 2 * 2
    return CropGeometry(cw, ch, (info.height - ch) // 2 // 2 * 2, info.width, info.height)


def smooth_targets(targets: list[float | None], fps: float, crop_frac: float, cfg: Config) -> list[float]:
    """EMA + zona muerta + límite de velocidad sobre la coordenada X normalizada (centro del recorte)."""
    s = cfg.reframe.smoothing
    lo, hi = crop_frac / 2, 1 - crop_frac / 2
    first = next((t for t in targets if t is not None), 0.5)
    cur = min(max(first, lo), hi)
    hold_max = int(s.lost_hold_seconds * fps)
    last, lost, out = cur, 0, []
    max_step = s.max_speed * crop_frac / fps
    for t in targets:
        if t is None:
            lost += 1
            goal = last if lost <= hold_max else 0.5   # tras un rato sin objetivo, vuelve al centro
        else:
            lost, last, goal = 0, t, t
        goal = min(max(goal, lo), hi)
        if abs(goal - cur) > s.deadzone * crop_frac:
            step = s.ema_alpha * (goal - cur)
            cur += max(-max_step, min(max_step, step))
        out.append(cur)
    return out


@dataclass
class CropPath:
    fps: float
    cx: list[float]            # centro X normalizado por muestra
    geom: CropGeometry

    def x_expr(self) -> str:
        """Expresión ffmpeg (función de t) lineal a tramos para el borde izquierdo del recorte, en px."""
        g = self.geom
        xs = [min(max(c * g.src_w - g.crop_w / 2, 0), g.src_w - g.crop_w) for c in self.cx]
        dt = 1 / self.fps
        terms = [f"{xs[0]:.2f}"]
        for i in range(len(xs) - 1):
            slope = (xs[i + 1] - xs[i]) / dt
            if abs(slope) < 1e-3:
                continue
            terms.append(f"({slope:.3f})*clip(t-{i * dt:.4f},0,{dt:.4f})")
        return "+".join(terms)


def analyze(input_args: list[str], info: VideoInfo, cfg: Config) -> CropPath:
    r = cfg.reframe
    geom = crop_geometry(info, r.width, r.height)
    aw = r.analysis_width
    ah = int(round(aw * info.height / info.width)) // 2 * 2
    det = make_detector(cfg)
    sel = SpeakerSelector(cfg, r.analysis_fps)

    audio = load_audio(input_args)
    n_est = int(len(audio) / 16000 * r.analysis_fps) + r.analysis_fps * 2 if len(audio) else 100000
    audio_db = envelope_db(audio, r.analysis_fps, n_est)
    gate = r.speaker.audio_gate_db

    targets: list[float | None] = []
    prev = None
    try:
        for i, bgr in enumerate(read_frames(input_args, r.analysis_fps, aw, ah)):
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            voice = bool(audio_db[min(i, n_est - 1)] > gate)
            box = sel.update(det.detect(bgr), gray, prev, voice)
            targets.append(box.cx / aw if box is not None else motion_center(gray, prev))
            prev = gray
    finally:
        if hasattr(det, "close"):
            det.close()
    if not targets:
        raise RuntimeError("No se pudieron leer fotogramas para el seguimiento")
    n = len(targets)

    cx = smooth_targets(targets, r.analysis_fps, geom.crop_w / info.width, cfg)
    log.info("Seguimiento: %d muestras, detector=%s", n, type(det).__name__)
    return CropPath(r.analysis_fps, cx, geom)
