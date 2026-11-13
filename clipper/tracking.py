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
from .config import Config, RectCfg
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
def motion_center(gray, prev, mask_px: tuple[int, int, int, int] | None = None, center_bias: float = 0.0,
                  keep_px: tuple[int, int, int, int] | None = None) -> float | None:
    """Dónde está la acción (x, 0..1) entre dos fotogramas; None si casi no hay movimiento.

    Se toma el PICO del movimiento (no la media: con dos acciones separadas la media cae en un hueco vacío) y se centra en su
    entorno. `center_bias` favorece la acción céntrica frente a la lateral (el punto de vista suele estar en el centro)."""
    if prev is None:
        return None
    d = cv2.GaussianBlur(cv2.absdiff(gray, prev), (5, 5), 0)
    if mask_px:                       # zona a ignorar (p. ej. la cámara del streamer)
        x0, y0, x1, y1 = mask_px
        d[y0:y1, x0:x1] = 0
    if keep_px:                       # zona de interés (como las zonas de movimiento de un DVR): fuera de ella se ignora todo
        x0, y0, x1, y1 = keep_px
        keep = np.zeros_like(d)
        keep[y0:y1, x0:x1] = 1
        d = d * keep
    mask = (d > 18).astype(np.float32)
    if mask.mean() < 0.0005 if keep_px else mask.mean() < 0.002:
        return None
    cols = mask.sum(axis=0)
    w = gray.shape[1]
    xs = np.arange(w) / w
    prior = (1 - center_bias) + center_bias * np.exp(-((xs - 0.5) / 0.28) ** 2 / 2)       # 1 en el centro, menos en los bordes
    k = max(3, w // 12)
    smooth = np.convolve(cols * prior, np.ones(k) / k, mode="same")
    peak = int(np.argmax(smooth))
    lo, hi = max(0, peak - int(0.18 * w)), min(w, peak + int(0.18 * w))                    # entorno del pico
    seg = cols[lo:hi]
    if seg.sum() <= 0:
        return peak / w
    return float((seg * np.arange(lo, hi)).sum() / seg.sum() / w)


# ------------------------------------------------------------------ personas / personajes (HOG integrado en OpenCV)
class PersonDetector:
    """Detecta figuras humanas de pie (personas o personajes de juego) sin descargar modelos. Rinde mejor con figuras grandes."""

    def __init__(self):
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def detect(self, bgr) -> list[tuple[float, float, float, float, float]]:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        rects, weights = self.hog.detectMultiScale(gray, winStride=(8, 8), padding=(8, 8), scale=1.06, hitThreshold=0.2)
        return [(float(x), float(y), float(w), float(h), float(wt)) for (x, y, w, h), wt in zip(rects, np.ravel(weights))]


def pick_person(rects, width: int, mask_px: tuple[int, int, int, int] | None = None) -> float | None:
    """x (0..1) del personaje más relevante: grande, seguro y céntrico. Ignora lo que caiga en la zona enmascarada (la cámara)."""
    best, score = None, 0.0
    for x, y, w, h, wt in rects:
        cx, cy = x + w / 2, y + h / 2
        if mask_px and mask_px[0] <= cx <= mask_px[2] and mask_px[1] <= cy <= mask_px[3]:
            continue
        s = (w * h) * max(wt, 0.1) * (0.5 + 0.5 * (1 - abs(cx / width - 0.5) * 2))
        if s > score:
            best, score = cx / width, s
    return best


# ------------------------------------------------------------------ lectura de frames
def read_frames(input_args: list[str], fps: float, w: int, h: int, pre: str = "") -> Iterator[np.ndarray]:
    """Fotogramas BGR a `fps` y tamaño w×h. `pre` = filtros previos (p. ej. "crop=…,")."""
    cmd = [require("ffmpeg"), "-v", "error", "-nostdin", *input_args, "-an",
           "-vf", f"{pre}fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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


def pl_expr(xs: list[float], fps: float) -> str:
    """Expresión ffmpeg (función de t) lineal a tramos que pasa por los valores `xs` (una muestra cada 1/fps s)."""
    dt = 1 / fps
    terms = [f"{xs[0]:.2f}"]
    for i in range(len(xs) - 1):
        slope = (xs[i + 1] - xs[i]) / dt
        if abs(slope) < 1e-3:
            continue
        terms.append(f"({slope:.3f})*clip(t-{i * dt:.4f},0,{dt:.4f})")
    return "+".join(terms)


@dataclass
class FacePath:
    """Posición del rostro (0..1, relativa a la cámara) por muestra, ya suavizada."""
    fps: float
    fx: list[float]
    fy: list[float]


def track_face_in(input_args: list[str], info: VideoInfo, cfg: Config, rect: RectCfg, fps: int = 6) -> FacePath | None:
    """Seguimiento REAL del rostro dentro de la cámara: detecta la cara en cada muestra y suaviza el recorrido (EMA, zona muerta y
    espera si la pierde un instante). None si nunca se ve un rostro."""
    px = (int(rect.x * info.width), int(rect.y * info.height), max(2, int(rect.w * info.width) // 2 * 2), max(2, int(rect.h * info.height) // 2 * 2))
    aw = 320
    ah = max(2, int(round(aw * px[3] / px[2])) // 2 * 2)
    det = make_detector(cfg)
    raw: list[tuple[float, float] | None] = []
    try:
        for bgr in read_frames(input_args, fps, aw, ah, pre=f"crop={px[2]}:{px[3]}:{px[0]}:{px[1]},"):
            f = det.detect(bgr)
            if f:
                b = max(f, key=lambda b: b.w)
                raw.append(((b.x + b.w / 2) / aw, (b.y + b.h / 2) / ah))
            else:
                raw.append(None)
    finally:
        if hasattr(det, "close"):
            det.close()
    return smooth_face(raw, fps)


def smooth_face(raw: list, fps: float, dead: tuple[float, float] = (0.08, 0.07), alpha: float = 0.10, hold_s: float = 2.0) -> FacePath | None:
    """Encuadre TRANQUILO: el encuadre se queda quieto mientras el rostro esté dentro de una zona cómoda (`dead`, fracción de la cámara)
    y solo se recoloca —suave y lento— por la parte que se sale. Así no hay un deslizamiento constante de lado a lado."""
    first = next((r for r in raw if r is not None), None)
    if first is None:
        return None
    cx, cy = first
    fx, fy, lost, last = [], [], 0, first
    for r in raw:
        if r is None:
            lost += 1
            goal = last if lost <= hold_s * fps else (0.5, 0.45)         # tras ~2 s sin verse, vuelve al centro
        else:
            lost, last, goal = 0, r, r
        for axis, d in ((0, dead[0]), (1, dead[1])):
            diff = goal[axis] - (cx if axis == 0 else cy)
            if abs(diff) > d:
                step = alpha * (diff - (d if diff > 0 else -d))            # solo la parte que sale de la zona cómoda
                if axis == 0:
                    cx += step
                else:
                    cy += step
        fx.append(cx)
        fy.append(cy)
    return FacePath(fps, fx, fy)


@dataclass
class CropPath:
    fps: float
    cx: list[float]            # centro X normalizado por muestra
    geom: CropGeometry

    def static_x(self) -> str:
        """Posición fija (px) del recorte: la mediana de la trayectoria. Respaldo si el recorte móvil no funciona."""
        g = self.geom
        xs = sorted(min(max(c * g.src_w - g.crop_w / 2, 0), g.src_w - g.crop_w) for c in self.cx)
        return f"{xs[len(xs) // 2]:.2f}"

    def x_expr(self) -> str:
        """Expresión ffmpeg (función de t) lineal a tramos para el borde izquierdo del recorte, en px."""
        g = self.geom
        return pl_expr([min(max(c * g.src_w - g.crop_w / 2, 0), g.src_w - g.crop_w) for c in self.cx], self.fps)


def analyze(input_args: list[str], info: VideoInfo, cfg: Config, out_w: int | None = None, out_h: int | None = None,
            ignore: "RectCfg | None" = None, faces: bool = True, people: bool | None = None,
            zone: "RectCfg | None" = None) -> CropPath:
    """Trayectoria de la cámara virtual. out_w/out_h = proporción del recorte (por defecto 9:16 completo);
    `ignore` = rectángulo normalizado que no cuenta como acción; faces=False sigue solo el movimiento.
    Prioridad del objetivo: rostro/hablante → personaje en pantalla → movimiento (con sesgo al centro) → punto de vista central."""
    r = cfg.reframe
    geom = crop_geometry(info, out_w or r.width, out_h or r.height)
    people = r.track_people if people is None else people
    aw = max(r.analysis_width, 640) if people else r.analysis_width          # los personajes necesitan algo más de resolución
    ah = int(round(aw * info.height / info.width)) // 2 * 2
    det = make_detector(cfg) if faces else NullDetector()
    sel = SpeakerSelector(cfg, r.analysis_fps)

    audio = load_audio(input_args)
    n_est = int(len(audio) / 16000 * r.analysis_fps) + r.analysis_fps * 2 if len(audio) else 100000
    audio_db = envelope_db(audio, r.analysis_fps, n_est)
    gate = r.speaker.audio_gate_db

    mask_px = None
    if ignore:
        mask_px = (int(ignore.x * aw), int(ignore.y * ah), int((ignore.x + ignore.w) * aw) + 1, int((ignore.y + ignore.h) * ah) + 1)
    zone_px = None
    if zone:
        zone_px = (int(zone.x * aw), int(zone.y * ah), int((zone.x + zone.w) * aw) + 1, int((zone.y + zone.h) * ah) + 1)
    pdet = PersonDetector() if people else None
    person_x: float | None = None
    targets: list[float | None] = []
    prev = None
    try:
        for i, bgr in enumerate(read_frames(input_args, r.analysis_fps, aw, ah)):
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            voice = bool(audio_db[min(i, n_est - 1)] > gate)
            box = sel.update(det.detect(bgr), gray, prev, voice)
            if box is not None:
                targets.append(box.cx / aw)
            else:
                if pdet is not None and i % 3 == 0:                      # ~2 detecciones/s: suficiente y barato
                    person_x = pick_person(pdet.detect(bgr), aw, mask_px)
                if person_x is not None:
                    targets.append(person_x)
                else:
                    targets.append(motion_center(gray, prev, mask_px, r.center_bias, zone_px))
            prev = gray
    finally:
        if hasattr(det, "close"):
            det.close()
    if not targets:
        raise RuntimeError("No se pudieron leer fotogramas para el seguimiento")
    n = len(targets)

    cx = smooth_targets(targets, r.analysis_fps, geom.crop_w / info.width, cfg)
    if zone:                                                  # el recorte no sale de la zona marcada
        half = geom.crop_w / info.width / 2
        lo, hi = zone.x + half, zone.x + zone.w - half
        cx = [min(max(c, lo), hi) for c in cx] if lo <= hi else [zone.x + zone.w / 2] * len(cx)
    log.info("Seguimiento: %d muestras, detector=%s", n, type(det).__name__)
    return CropPath(r.analysis_fps, cx, geom)


# ------------------------------------------------------------------ cámara del streamer (facecam)
def _search_regions() -> list[tuple[float, float, float, float]]:
    """Zonas (x, y, w, h normalizadas) donde suele ir una cámara superpuesta: esquinas y centros de borde."""
    out = []
    for sz in (0.5, 0.34):
        for ax in (0.0, 1 - sz):
            for ay in (0.0, 1 - sz):
                out.append((ax, ay, sz, sz))
        out += [(0.5 - sz / 2, 0.0, sz, sz), (0.5 - sz / 2, 1 - sz, sz, sz)]
    return out


def detect_facecam(input_args: list[str], info: VideoInfo, cfg: Config, seconds: int = 8) -> RectCfg | None:
    """Busca la cámara superpuesta del streamer: un rostro pequeño y estable hacia una esquina/borde.
    El detector de rostros solo ve caras grandes, así que se buscan recortes ampliados de las esquinas.
    Devuelve el rectángulo normalizado que la contiene, o None si no parece haber (p. ej. cámara a pantalla completa)."""
    aw = 1280
    ah = int(round(aw * info.height / info.width)) // 2 * 2
    det = make_detector(cfg)
    hits: list[tuple[float, float, float, float]] = []        # (cx, cy, w, h) normalizados al fotograma
    frames = 0
    try:
        for i, bgr in enumerate(read_frames(input_args, 1.0, aw, ah)):
            if i >= seconds:
                break
            frames += 1
            for rx, ry, rw, rh in _search_regions():
                x0, y0, x1, y1 = int(rx * aw), int(ry * ah), int((rx + rw) * aw), int((ry + rh) * ah)
                crop = bgr[y0:y1, x0:x1]
                k = 480 / crop.shape[1]
                for b in det.detect(cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)):
                    if 0.06 * 480 < b.w < 0.8 * 480:           # descarta falsos positivos diminutos / gigantes
                        hits.append(((x0 + (b.x + b.w / 2) / k) / aw, (y0 + (b.y + b.h / 2) / k) / ah,
                                     b.w / k / aw, b.h / k / ah))
    finally:
        if hasattr(det, "close"):
            det.close()
    if frames == 0 or not hits:
        return None
    # agrupa por posición (rejilla del 6 %) y se queda con el grupo más repetido
    groups: dict[tuple[int, int], list] = {}
    for h in hits:
        groups.setdefault((round(h[0] / 0.06), round(h[1] / 0.06)), []).append(h)
    best = max(groups.values(), key=len)
    if len(best) < max(2, frames // 2):                           # debe verse en la mitad de los fotogramas
        return None
    cx, cy, fw, fh = (float(v) for v in np.median(np.array(best), axis=0))
    if fw > 0.3 or not (cx < 0.35 or cx > 0.65 or cy < 0.3 or cy > 0.7):
        return None                                               # rostro grande o central: no es una cámara superpuesta
    w = fw * 3.2                                                  # una cámara con plano de busto mide ~3,2 veces el ancho del rostro
    ed = min(cx, 1 - cx)
    if ed < 0.3:                                                  # pegada a un lado: puede ser algo mayor, pero sin pasarse
        w = min(max(w, 2 * ed), fw * 3.6)
    w = min(0.5, w)
    h = min(0.9, w * (info.width / info.height) * 3 / 4)         # proporción 4:3 en píxeles
    x, y = min(max(cx - w / 2, 0), 1 - w), min(max(cy - h / 2, 0), 1 - h)
    snap = 0.08                                                   # las cámaras suelen ir pegadas al borde
    x = 0.0 if x < snap else (1 - w if 1 - (x + w) < snap else x)
    y = 0.0 if y < snap else (1 - h if 1 - (y + h) < snap else y)
    return RectCfg(x=round(x, 4), y=round(y, 4), w=round(w, 4), h=round(h, 4))


def face_center_in(input_args: list[str], info: VideoInfo, cfg: Config, rect: RectCfg, seconds: int = 10) -> tuple[float, float] | None:
    """Centro (0..1, relativo al rectángulo) del rostro dentro de la cámara; sirve para encuadrarla bien."""
    px = (int(rect.x * info.width), int(rect.y * info.height),
          int(rect.w * info.width) // 2 * 2, int(rect.h * info.height) // 2 * 2)
    aw = 320
    ah = max(2, int(round(aw * px[3] / px[2])) // 2 * 2)
    det = make_detector(cfg)
    cs: list[tuple[float, float]] = []
    try:
        for i, bgr in enumerate(read_frames(input_args, 1.0, aw, ah, pre=f"crop={px[2]}:{px[3]}:{px[0]}:{px[1]},")):
            if i >= seconds:
                break
            f = det.detect(bgr)
            if f:
                b = max(f, key=lambda b: b.w)
                cs.append(((b.x + b.w / 2) / aw, (b.y + b.h / 2) / ah))
    finally:
        if hasattr(det, "close"):
            det.close()
    if not cs:
        return None
    return float(np.median([c[0] for c in cs])), float(np.median([c[1] for c in cs]))
