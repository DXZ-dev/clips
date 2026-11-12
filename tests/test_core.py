import os
import time
from pathlib import Path

from clipper.buffer import BufferReader, RollingBuffer
from clipper.config import Config, load_config
from clipper.ffutil import VideoInfo
from clipper.subtitles import Word, _color, _ts, build_ass
from clipper.tracking import CropPath, crop_geometry, smooth_targets


def test_config_defaults_load():
    cfg = load_config()
    assert cfg.buffer.window_seconds == 90 and cfg.reframe.width == 1080


def test_crop_geometry_16_9_to_9_16():
    g = crop_geometry(VideoInfo(1920, 1080, 30, 10), 1080, 1920)
    assert (g.crop_w, g.crop_h, g.y) == (606, 1080, 0)
    p = crop_geometry(VideoInfo(720, 1280, 30, 10), 1080, 1920)   # ya vertical
    assert p.crop_w <= 720 and p.crop_h <= 1280


def test_smoothing_is_smooth_and_clamped():
    cfg = Config()
    frac = 0.31
    jump = [0.2] * 20 + [0.8] * 40
    out = smooth_targets(jump, 6, frac, cfg)
    steps = [abs(b - a) for a, b in zip(out, out[1:])]
    assert max(steps) <= cfg.reframe.smoothing.max_speed * frac / 6 + 1e-9   # sin saltos
    assert all(frac / 2 - 1e-9 <= v <= 1 - frac / 2 + 1e-9 for v in out)    # dentro del encuadre
    assert out[-1] > out[0]                                                   # sí llega a moverse


def test_smoothing_deadzone_keeps_still():
    out = smooth_targets([0.5, 0.51, 0.49, 0.5], 6, 0.31, Config())
    assert max(out) - min(out) < 1e-9


def test_x_expr_piecewise_linear():
    g = crop_geometry(VideoInfo(1280, 720, 25, 10), 1080, 1920)
    p = CropPath(6, [0.3, 0.3, 0.5], g)
    e = p.x_expr()
    assert e.startswith("%.2f" % max(0.3 * 1280 - g.crop_w / 2, 0)) and "clip(t-" in e
    assert e.count("clip(") == 1      # el primer tramo está quieto y no genera término


def test_ass_words_highlight_and_format():
    cfg = Config().subtitles
    ass = build_ass([Word(0, .4, "mira"), Word(.4, .8, "esto"), Word(.8, 1.2, "{x}")], cfg)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert ass.count("Dialogue:") == 3
    assert _color("#FFE600") in ass and "MIRA" in ass and "{x}" not in ass
    assert _ts(3661.5) == "1:01:01.50"


def test_buffer_prune_and_reader(tmp_path):
    cfg = Config()
    cfg.buffer.dir = str(tmp_path)
    cfg.buffer.window_seconds = 20
    rows = []
    for i in range(10):
        (tmp_path / f"seg_{i:08d}.ts").write_bytes(b"x")
        rows.append(f"seg_{i:08d}.ts,{i * 5}.0,{i * 5 + 5}.0")
    (tmp_path / "list.csv").write_text("\n".join(rows))
    rb = RollingBuffer(cfg)
    assert rb.prune() > 0
    left = BufferReader(tmp_path).segments()
    assert left[-1].end == 50.0 and left[0].start >= 25.0            # solo ~ventana
    ci = BufferReader(tmp_path).prepare(12)
    assert ci.duration == 12 and "-ss" in ci.args
    assert all(os.stat(p).st_nlink >= 2 for p in ci.workdir.glob("seg_*.ts"))   # hardlinks
    ci.cleanup()
    assert not ci.workdir.exists()
