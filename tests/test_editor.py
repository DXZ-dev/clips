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
                    "[1]crop=512:384:0:40,scale=320:240[c];[0][c]overlay=W-w:H-h", "-t", "10", "-c:v", "libx264",
                    "-preset", "ultrafast", str(vid)], check=True)
    r = detect_facecam(["-i", str(vid)], probe(str(vid)), cfg)
    assert r is not None
    # debe contener la cámara real (x 0.75..1, y 0.667..1) y estar pegada a la esquina inferior derecha
    assert r.x <= 0.76 and r.y <= 0.68 and r.x + r.w >= 0.99 and r.y + r.h >= 0.99
    assert detect_facecam(["-i", str(vid)], probe(str(vid)), cfg.model_copy(update={"reframe": cfg.reframe.model_copy(update={"detector": "none"})})) is None
