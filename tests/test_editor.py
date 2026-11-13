import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from clipper import editor as ed
from clipper.config import Config, RectCfg
from clipper.engine import Engine
from clipper.ffutil import probe
from clipper.render import _cam_crop, build_graph
from clipper.spec import EditSpec, EditWord, default_spec
from clipper.subtitles import PRESETS, Word, build_ass, with_preset
from clipper.web.app import create_app


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    p = tmp_path_factory.mktemp("src") / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=5",
                    "-f", "lavfi", "-i", "sine=f=300:d=5", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(p)], check=True)
    return p


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.clip.output_dir = str(tmp_path / "out")
    c.buffer.dir = str(tmp_path / "buf")
    c.transcribe.enabled = False
    c.reframe.detector = "none"        # sin MediaPipe en los tests de renderizado
    return c


def make_clip(cfg, source, stem="clip_a"):
    """Crea un clip editable como lo haría el pipeline (fuente + edit.json)."""
    ed.src_dir(cfg).mkdir(parents=True, exist_ok=True)
    src = ed.source_path(cfg, stem)
    src.write_bytes(source.read_bytes())
    words = [EditWord(start=1.0, end=1.4, text="hola"), EditWord(start=1.5, end=2.0, text="mundo")]
    spec = default_spec(cfg, words, 0.5, 4.5)
    ed.finalize(cfg, stem, src, spec, ed.out_dir(cfg) / f"{stem}.mp4", {"reason": "keyword", "detail": "x"})
    return f"{stem}.mp4"


# ------------------------------------------------------------------ modelo y estilos
def test_spec_validation():
    with pytest.raises(ValidationError):
        EditSpec(trim_start=2, trim_end=2.5)                     # < 1 s
    with pytest.raises(ValidationError):
        EditSpec(trim_start=0, trim_end=400)                     # > 180 s
    with pytest.raises(ValidationError):
        EditSpec(facecam={"x": 0.9, "y": 0.1, "w": 0.3, "h": 0.2})   # se sale del fotograma
    with pytest.raises(ValidationError):
        EditSpec(subtitles={"highlight_color": "rojo"})
    assert EditSpec().layout == "auto"


def test_presets_and_overrides():
    c = Config().subtitles
    assert set(PRESETS) >= {"clasico", "hormozi", "beast", "neon", "fuego", "minimal"}
    h = with_preset(c, "hormozi", highlight_color="#112233", font_size=None)
    assert h.highlight_color == "#112233" and h.font_size == PRESETS["hormozi"]["font_size"]
    ass = build_ass([Word(0, .5, "hola")], with_preset(c, "clasico", position="seam"), seam_y=700,
                    hook=("Un gancho muy largo para que se parta en varias líneas", 2), watermark=("@yo", "tl"), duration=6)
    assert "\\an5\\pos(540,700)" in ass and "Hook," in ass and "\\N" in ass.split("Hook,,0,0,0,,")[1]
    assert "{\\alpha&H66&}@yo" in ass


def test_cam_crop_aspect_and_bounds():
    class I:  # noqa: D401
        width, height = 1920, 1080
    r = RectCfg(x=0.75, y=0.62, w=0.25, h=0.38)
    px, py, pw, ph = _cam_crop(I, r, (0.5, 0.5), 1080, 730)
    assert abs(pw / ph - 1080 / 730) < 0.02
    assert 0.75 * 1920 - 2 <= px and px + pw <= 1920 + 2 and py + ph <= 1080 + 2


# ------------------------------------------------------------------ render real (ffmpeg)
@pytest.mark.parametrize("layout,gp", [("auto", "contain"), ("fit", "contain"), ("split", "contain"), ("split", "cover")])
def test_render_layouts_produce_9x16(cfg, source, layout, gp, tmp_path):
    from clipper.render import render_spec
    spec = EditSpec(layout=layout, gameplay=gp, facecam=RectCfg(x=0.7, y=0.6, w=0.3, h=0.4), trim_start=0.5, trim_end=3.5,
                    words=[EditWord(start=1, end=1.5, text="hola")], hook={"text": "gancho"}, watermark={"text": "@yo"})
    out = tmp_path / f"{layout}_{gp}.mp4"
    meta = render_spec(cfg, source, spec, out)
    i = probe(str(out))
    assert (i.width, i.height) == (1080, 1920) and abs(i.duration - 3.0) < 0.3
    assert meta["layout"] == layout and not list(tmp_path.glob("*.part"))


def test_split_without_cam_falls_back_to_auto(cfg, source, tmp_path):
    from clipper.render import render_spec
    meta = render_spec(cfg, source, EditSpec(layout="split"), tmp_path / "o.mp4")   # sin rect y detector none
    assert meta["layout"] == "auto" and "cámara" in meta["warning"]


def test_preview_is_small(cfg, source, tmp_path):
    from clipper.render import render_spec
    render_spec(cfg, source, EditSpec(layout="fit"), tmp_path / "p.mp4", preview=True)
    assert (probe(str(tmp_path / "p.mp4")).width, probe(str(tmp_path / "p.mp4")).height) == (540, 960)


# ------------------------------------------------------------------ editor + API
def test_apply_as_new_and_validation(cfg, source):
    name = make_clip(cfg, source)
    data = ed.load_edit(cfg, name)
    spec = EditSpec.model_validate(data["spec"])
    spec.layout, spec.hook.text = "fit", "nuevo"
    assert ed.apply(cfg, name, spec, as_new=True) == "clip_a_v2.mp4"
    assert ed.apply(cfg, name, spec, as_new=True) == "clip_a_v3.mp4"
    assert ed.load_edit(cfg, "clip_a_v2.mp4")["spec"]["layout"] == "fit"
    assert ed.load_edit(cfg, name)["spec"]["layout"] == "auto"                  # el original no cambia
    assert json.loads((ed.out_dir(cfg) / "clip_a_v2.json").read_text())["edited"] is True
    bad = spec.model_copy(update={"trim_start": 4.9})
    with pytest.raises(ed.EditorError):
        ed.apply(cfg, name, bad)
    with pytest.raises(ed.EditorError):
        ed.stem_of("../x.mp4")
    ed.remove_clip_files(cfg, "clip_a_v2")
    assert not (ed.out_dir(cfg) / "clip_a_v2.mp4").exists() and not ed.source_path(cfg, "clip_a_v2").exists()
    assert ed.source_path(cfg, "clip_a").exists()                                # el resto intacto


def test_editor_http_api(cfg, source, tmp_path):
    name = make_clip(cfg, source)
    c = TestClient(create_app(Engine(cfg, data_dir=tmp_path), manage_engine=False), base_url="http://localhost")
    listed = c.get("/api/clips").json()
    assert listed[0]["editable"] is True and listed[0]["layout"] == "auto"
    d = c.get(f"/api/clips/{name}/edit").json()
    assert d["source"]["width"] == 640 and len(d["spec"]["words"]) == 2
    assert c.get(f"/api/clips/{name}/frame?t=1&w=320").headers["content-type"] == "image/jpeg"
    assert c.get(f"/api/clips/{name}/source").status_code == 200
    spec = d["spec"] | {"layout": "fit"}
    r = c.post(f"/api/clips/{name}/preview", json=spec).json()
    assert c.get(r["url"]).status_code == 200
    assert c.post(f"/api/clips/{name}/preview", json=spec | {"trim_start": 4.95, "trim_end": 5.9}).status_code in (409, 422)
    assert c.post(f"/api/clips/{name}/preview", json=spec | {"hook": {"text": "x" * 500}}).status_code == 422
    assert c.post(f"/api/clips/{name}/render", json={"spec": spec, "as_new": True}).json()["name"] == "clip_a_v2.mp4"
    assert c.get("/api/clips/..%2Fx.mp4/edit").status_code in (400, 404)
    assert c.get("/api/clips/nope.mp4/edit").status_code == 404
    assert set(c.get("/api/presets").json()) >= {"hormozi", "beast"}
    assert c.get("/static/editor.js").status_code == 200
    assert c.delete(f"/api/clips/{name}").status_code == 200 and not ed.source_path(cfg, "clip_a").exists()


# ------------------------------------------------------------------ autodetección de cámara (necesita MediaPipe + imagen de prueba)
def test_detect_facecam_on_synthetic_stream(tmp_path):
    pytest.importorskip("skimage")
    import cv2
    from skimage import data
    from clipper.config import load_config
    from clipper.tracking import detect_facecam
    cfg = load_config()
    if not cfg.path(cfg.reframe.mediapipe_model).exists():
        pytest.skip("falta el modelo de MediaPipe (scripts/download_models.sh)")
    face = tmp_path / "face.png"
    cv2.imwrite(str(face), data.astronaut()[:, :, ::-1])
    vid = tmp_path / "cam.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=10", "-loop", "1",
                    "-framerate", "25", "-i", str(face), "-filter_complex",
                    "[1]crop=330:248:85:20,scale=320:240[c];[0][c]overlay=W-w:H-h", "-t", "10", "-c:v", "libx264",
                    "-preset", "ultrafast", str(vid)], check=True)
    r = detect_facecam(["-i", str(vid)], probe(str(vid)), cfg)
    assert r is not None
    # debe contener la cámara real (x 0.75..1, y 0.667..1) y estar pegada a la esquina inferior derecha
    assert r.x <= 0.76 and r.y <= 0.68 and r.x + r.w >= 0.99 and r.y + r.h >= 0.99
    assert detect_facecam(["-i", str(vid)], probe(str(vid)), cfg.model_copy(update={"reframe": cfg.reframe.model_copy(update={"detector": "none"})})) is None


# ------------------------------------------------------------------ red de seguridad del render
def test_trim_outside_source_is_adjusted_not_fatal(cfg, source, tmp_path):
    from clipper.render import render_spec
    meta = render_spec(cfg, source, EditSpec(layout="fit", trim_start=50, trim_end=55), tmp_path / "o.mp4")   # la fuente dura 5 s
    assert "adjusted" in meta and probe(str(tmp_path / "o.mp4")).height == 1920


def test_render_falls_back_to_static_then_fit(cfg, source, tmp_path, monkeypatch):
    import clipper.render as R
    real = R._run_ffmpeg
    seen = []

    def flaky(cfg_, input_args, graph, part, cwd, preview, dur_out=36000.0, fail_on=("clip(t-",)):
        seen.append(graph[:40])
        if any(f in graph for f in fail_on):                 # el recorte móvil «no funciona» en esta máquina
            return None, "Failed to configure input pad on Parsed_crop_0 (simulado)"
        return real(cfg_, input_args, graph, part, cwd, preview, dur_out)

    monkeypatch.setattr(R, "_run_ffmpeg", flaky)
    # un movimiento real en el vídeo para que la trayectoria tenga pendiente
    moving = tmp_path / "m.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x203040:s=640x360:r=25:d=6", "-f", "lavfi", "-i",
                    "color=c=white:s=80x110:r=25:d=6", "-filter_complex", "[0][1]overlay=x='20+500*t/6':y=120", "-c:v", "libx264",
                    "-preset", "ultrafast", str(moving)], check=True)
    meta = R.render_spec(cfg, moving, EditSpec(layout="auto"), tmp_path / "a.mp4")
    assert meta.get("fallback") == "static" and len(seen) >= 2 and probe(str(tmp_path / "a.mp4")).height == 1920
    # si también falla el estático, cae a «completo»
    monkeypatch.setattr(R, "_run_ffmpeg", lambda *a, **k: (None, "falla") if "x='" in a[2] else real(*a, **k))
    meta = R.render_spec(cfg, moving, EditSpec(layout="auto"), tmp_path / "b.mp4")
    assert meta.get("fallback") == "fit"
    # si todo falla, el error trae contexto para diagnosticar
    monkeypatch.setattr(R, "_run_ffmpeg", lambda *a, **k: (None, "boom"))
    with pytest.raises(RuntimeError, match=r"entrada 640x360.*boom"):
        R.render_spec(cfg, moving, EditSpec(layout="auto"), tmp_path / "c.mp4")


def test_selftest_passes_on_this_machine(cfg):
    from clipper import selftest
    lines = []
    assert selftest.run(cfg, out=lines.append) == 0, "\n".join(lines)
    assert any("clip desde el buffer" in l and "OK" in l for l in lines)


# ------------------------------------------------------------------ zona 1 (gameplay) marcada a mano
def test_zone_crop_geometry_and_render(cfg, source, tmp_path):
    from clipper.render import _zone_crop, render_spec
    from clipper.ffutil import VideoInfo
    info = VideoInfo(1920, 1080, 30, 10)
    zone = RectCfg(x=0.30, y=0.10, w=0.50, h=0.80)                       # 960x864 px
    cw, ch, x, y = _zone_crop(info, zone, 1080, 1267)                    # proporción de salida del hueco de gameplay
    assert abs(cw / ch - 1080 / 1267) < 0.01
    assert 0.30 * 1920 - 2 <= x and x + cw <= 0.80 * 1920 + 2 and 0.10 * 1080 - 2 <= y and y + ch <= 0.90 * 1080 + 2   # dentro de la zona
    for mode in ("center", "contain"):
        spec = EditSpec(layout="split", gameplay=mode, facecam=RectCfg(x=0.0, y=0.0, w=0.28, h=0.37),
                        gameplay_zone=RectCfg(x=0.3, y=0.1, w=0.5, h=0.8), trim_start=0.5, trim_end=3.5)
        meta = render_spec(cfg, source, spec, tmp_path / f"z_{mode}.mp4")
        i = probe(str(tmp_path / f"z_{mode}.mp4"))
        assert (i.width, i.height) == (1080, 1920) and meta["layout"] == "split"
    meta = render_spec(cfg, source, EditSpec(layout="auto", gameplay_zone=RectCfg(x=0.1, y=0.0, w=0.5, h=1.0)), tmp_path / "za.mp4")
    assert probe(str(tmp_path / "za.mp4")).height == 1920


def test_motion_only_counts_inside_the_zone():
    import numpy as np
    from clipper.config import Config as Cfg
    from clipper.detect.visual import VisualDetector
    cfg = Cfg()
    cfg.reframe.track_people = False

    def run(zone):
        det = VisualDetector(cfg, zone=zone)
        base = np.full((180, 320, 3), 90, np.uint8)
        frames = [base.copy() for _ in range(30)]                         # calentamiento: escena quieta
        moving = base.copy()
        moving[40:140, 10:80] = 255                                       # mucho movimiento a la IZQUIERDA
        return det.feed(frames + [moving], 0.0)

    assert any(t.kind == "action" for t in run(None))                      # sin zona: se detecta
    assert not any(t.kind == "action" for t in run(RectCfg(x=0.5, y=0.0, w=0.5, h=1.0)))   # zona a la derecha: se ignora
    assert any(t.kind == "action" for t in run(RectCfg(x=0.0, y=0.0, w=0.4, h=1.0)))       # zona sobre el movimiento: cuenta


def test_zone_settings_roundtrip(tmp_path):
    from fastapi.testclient import TestClient
    from clipper.engine import Engine
    from clipper.web.app import create_app
    c0 = Config()
    c0.buffer.dir, c0.clip.output_dir, c0.transcribe.enabled = str(tmp_path / "b"), str(tmp_path / "o"), False
    eng = Engine(c0, data_dir=tmp_path)
    c = TestClient(create_app(eng, manage_engine=False), base_url="http://localhost")
    z = {"x": 0.3, "y": 0.1, "w": 0.5, "h": 0.8}
    r = c.put("/api/settings", json={"layout": {"mode": "split", "gameplay_zone": z, "facecam": {"x": 0, "y": 0, "w": 0.28, "h": 0.37}}})
    assert r.status_code == 200 and c0.layout.gameplay_zone.w == 0.5 and eng.visual.zone.x == 0.3        # el detector visual la recibe
    assert c.put("/api/settings", json={"layout": {"gameplay_zone": {"x": 0.8, "y": 0, "w": 0.5, "h": 1}}}).status_code == 422
    assert c.put("/api/settings", json={"layout": {"gameplay_zone": None}}).status_code == 200 and eng.visual.zone is None


# ------------------------------------------------------------------ acciones en lote
def test_bulk_zip_and_delete(cfg, source):
    import io
    import zipfile
    a, b = make_clip(cfg, source, "clip_a"), make_clip(cfg, source, "clip_b")
    c = TestClient(create_app(Engine(cfg, data_dir=ed.out_dir(cfg).parent), manage_engine=False), base_url="http://localhost")
    r = c.get("/api/clips/download.zip", params={"names": f"{a},../x.mp4,nada.mp4"})
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert a in names and "clip_a.txt" in names and len(names) == 2
    assert c.get("/api/clips/download.zip", params={"names": "nada.mp4"}).status_code == 404
    assert len(zipfile.ZipFile(io.BytesIO(c.get("/api/clips/download.zip", params={"all": "true"}).content)).namelist()) == 4
    assert c.post("/api/clips/delete", json={"names": [a, "../x.mp4"]}).json() == {"deleted": 1}
    assert not (ed.out_dir(cfg) / a).exists() and (ed.out_dir(cfg) / b).exists()
    assert c.post("/api/clips/delete_all", json={}).status_code == 422
    assert (ed.out_dir(cfg) / b).exists()
    assert c.post("/api/clips/delete_all", json={"confirm": True}).json() == {"deleted": 1}
    assert not list(ed.out_dir(cfg).glob("*.mp4"))


def test_exact_zone_sets_panel_height(cfg, source):
    from clipper.config import RectCfg
    cfg.layout.facecam = RectCfg(x=0.02, y=0.02, w=0.3, h=0.3)
    cfg.layout.gameplay_zone = RectCfg(x=0.1, y=0.3, w=0.8, h=0.6)
    stem = make_clip(cfg, source, "clip_z")
    spec = default_spec(cfg, [], 0.5, 4.5)
    spec = spec.model_copy(update={"layout": "split"})
    info = probe(source)
    graph, _, note = build_graph(cfg, spec, info, ["-i", str(source)], False, {}, None)
    assert "crop=" in graph and note["panels"] == "cards"


def test_overlay_render(cfg, source):
    from clipper.config import RectCfg
    cfg.layout.facecam = RectCfg(x=0.02, y=0.02, w=0.3, h=0.3)
    cfg.layout.gameplay_zone = RectCfg(x=0.2, y=0.0, w=0.6, h=1.0)
    cfg.layout.panels = "overlay"
    cfg.layout.cam_box = RectCfg(x=0.1, y=0.05, w=0.8, h=0.25)
    cfg.layout.mode = "split"
    cfg.reframe.detector = "none"
    cfg.layout.cam_follow = False
    name = make_clip(cfg, source, "clip_o")
    i = probe(ed.out_dir(cfg) / name)
    assert (i.width, i.height) == (1080, 1920)
