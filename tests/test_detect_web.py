import numpy as np
from fastapi.testclient import TestClient

from clipper.config import Config
from clipper.detect.triggers import CadenceDetector, EnergyDetector, KeywordMatcher, norm
from clipper.detect.vad import SR
from clipper.engine import Engine
from clipper.subtitles import Word
from clipper.web.app import create_app


def W(t, text):
    return Word(t, t + 0.3, text)


def test_norm_strips_accents_and_punct():
    assert norm("¡Increíble!") == "increible"


def test_keywords_span_chunks_and_no_duplicates():
    cfg = Config()
    km = KeywordMatcher(cfg)
    assert km.feed([W(1, "Mira")]) == []
    out = km.feed([W(1.4, "esto,"), W(2, "bien")])          # frase partida entre dos trozos
    assert [(t.kind, t.detail) for t in out] == [("keyword", "mira esto")]
    assert km.feed([W(3, "otra")]) == []                      # no se repite


def test_laughter_detected():
    km = KeywordMatcher(Config())
    assert [t.kind for t in km.feed([W(1, "Jajajaja")])] == ["laughter"]


def test_cadence_trigger():
    words = [W(i * 0.18, "x") for i in range(40)]           # ~5.5 palabras/s
    assert CadenceDetector(Config()).feed(words)
    slow = [W(i * 1.0, "x") for i in range(10)]
    assert not CadenceDetector(Config()).feed(slow)


def test_energy_spike_only_after_baseline():
    rng = np.random.default_rng(0)
    quiet = (rng.standard_normal(SR * 30) * 0.01).astype(np.float32)    # ≈ -40 dB
    loud = (rng.standard_normal(SR * 2) * 0.3).astype(np.float32)       # ≈ -11 dB
    d = EnergyDetector(Config())
    assert d.feed(quiet, 0.0) == []
    trig = d.feed(loud, 30.0)
    assert trig and 30 <= trig[0].t <= 32
    assert len(trig) == 1                                                # periodo refractario


def _client(tmp_path):
    cfg = Config()
    cfg.buffer.dir = str(tmp_path / "buf")
    cfg.clip.output_dir = str(tmp_path / "out")
    cfg.transcribe.enabled = False
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.mp4").write_bytes(b"x")
    return TestClient(create_app(Engine(cfg), manage_engine=False)), tmp_path


def test_api_status_clips_and_errors(tmp_path):
    c, tp = _client(tmp_path)
    assert c.get("/").status_code == 200
    s = c.get("/api/status").json()
    assert s["durations"] == [15, 30, 60] and s["compute"]["requested"] == "auto"
    assert c.post("/clip", json={"seconds": 30}).status_code == 409      # buffer vacío
    assert c.post("/clip", json={"seconds": 1}).status_code == 422
    assert [x["name"] for x in c.get("/api/clips").json()] == ["a.mp4"]
    assert c.get("/clips/a.mp4").status_code == 200
    assert c.get("/clips/..%2Fa.mp4").status_code in (400, 404)
    assert c.get("/clips/a.txt").status_code == 400
    assert c.delete("/api/clips/a.mp4").status_code == 200 and not (tp / "out" / "a.mp4").exists()


def test_device_selector(tmp_path):
    c, _ = _client(tmp_path)
    assert c.post("/api/device", json={"device": "cpu"}).json()["encoder"] == "libx264"
    assert c.post("/api/device", json={"device": "cuda"}).status_code == 200   # sin GPU cae a CPU
    assert c.post("/api/device", json={"device": "xx"}).status_code == 422
