import os
import stat
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper import links
from clipper.buffer import RollingBuffer
from clipper.config import Config
from clipper.engine import Engine
from clipper.ffutil import find_tool, probe
from clipper.sources import build_plan
from clipper.web.app import create_app


def fake_tool(bin_dir: Path, name: str, script: str) -> Path:
    p = bin_dir / name
    p.write_text("#!/bin/sh\n" + script + "\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.fixture
def sample_ts(tmp_path_factory):
    p = tmp_path_factory.mktemp("ts") / "live.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=30", "-f", "lavfi", "-i", "sine=f=300:d=30",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-c:a", "aac", "-f", "mpegts", str(p)], check=True)
    return p


# ------------------------------------------------------------------ plataformas y mensajes
def test_platform_detection():
    assert links.platform_of("https://www.twitch.tv/canal") == "twitch"
    assert links.platform_of("https://kick.com/canal") == "kick"
    assert links.platform_of("https://www.youtube.com/@x/live") == "youtube"
    assert links.platform_of("https://youtu.be/abc") == "youtube"
    assert links.platform_of("https://twitch.tv.evil.com/x") == "other"          # no se deja engañar por subdominios falsos
    assert links.platform_of("https://notkick.com/x") == "other"
    assert links.valid_url("https://kick.com/x") and not links.valid_url("--exec=x") and not links.valid_url("file:///etc/passwd")


def test_error_explanations_are_actionable():
    assert "robot" in links.explain_ytdlp("ERROR: Sign in to confirm you’re not a bot", "youtube")
    assert "no está en directo" in links.explain_ytdlp("error: No playable streams found on this URL")
    assert "privado" in links.explain_ytdlp("Video is private")
    assert "Actualiza yt-dlp" in links.explain_ytdlp("HTTP Error 403: Forbidden")


def test_tools_found_in_venv_when_not_in_path(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    import sys
    sibling = Path(sys.executable).parent
    for tool in ("yt-dlp", "streamlink"):
        if (sibling / tool).exists():
            assert find_tool(tool) == str(sibling / tool)         # bajo systemd el PATH no incluye el venv


# ------------------------------------------------------------------ probe con herramientas falsas
def test_probe_live_vod_offline_and_missing_tools(tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    monkeypatch.setenv("PATH", f"{b}:/usr/bin:/bin")
    monkeypatch.setattr("clipper.links.find_tool", lambda n: str(b / n) if (b / n).exists() else None)
    cfg = Config()
    r = links.probe_url(cfg, "https://kick.com/x")
    assert not r["ok"] and "Faltan herramientas" in r["message"]
    fake_tool(b, "yt-dlp", 'printf "Mi directo\\tNA\\tTrue\\n"')
    r = links.probe_url(cfg, "https://www.twitch.tv/x")
    assert r["ok"] and r["live"] is True and r["title"] == "Mi directo" and r["platform"] == "twitch"
    fake_tool(b, "yt-dlp", 'printf "Partida\\t3600\\tFalse\\n"')
    r = links.probe_url(cfg, "https://www.youtube.com/watch?v=1")
    assert r["ok"] and r["live"] is False and r["duration"] == 3600.0
    fake_tool(b, "yt-dlp", 'echo "ERROR: This channel is not currently live" >&2; exit 1')
    r = links.probe_url(cfg, "https://www.twitch.tv/off")
    assert not r["ok"] and r["live"] is False and "no está en directo" in r["message"]
    fake_tool(b, "streamlink", 'echo \'{"streams": {"best": {}}}\'')                    # yt-dlp falla pero streamlink ve el directo
    r = links.probe_url(cfg, "https://kick.com/live")
    assert r["ok"] and r["live"] is True
    assert not links.probe_url(cfg, "nada")["ok"]


# ------------------------------------------------------------------ plan de ingesta y ingesta real con productor falso
def test_live_plan_orders_producers_and_twitch_flag(tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    fake_tool(b, "streamlink", "true")
    fake_tool(b, "yt-dlp", "true")
    monkeypatch.setattr("clipper.sources.find_tool", lambda n: str(b / n))
    cfg = Config()
    cfg.source.type, cfg.source.url = "live", "https://www.twitch.tv/canal"
    plan = build_plan(cfg)
    assert [Path(c[0]).name for c in plan.producers] == ["streamlink", "yt-dlp"] and "--twitch-low-latency" in plan.producers[0]
    assert plan.ffmpeg_input == ["-i", "pipe:0"]
    cfg.source.url = "https://kick.com/canal"
    assert "--twitch-low-latency" not in build_plan(cfg).producers[0]
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Config.model_validate({"source": {"type": "live", "url": "ftp://x"}})
    monkeypatch.setattr("clipper.sources.find_tool", lambda n: None)
    with pytest.raises(ValueError, match="streamlink o yt-dlp"):
        build_plan(cfg)


def test_real_ingest_from_fake_streamlink_and_live_preview(tmp_path, monkeypatch, sample_ts):
    """Un «streamlink» falso vuelca vídeo por la tubería: se comprueba toda la ingesta y la previa 9:16 del directo."""
    b = tmp_path / "bin"
    b.mkdir()
    fake_tool(b, "streamlink", f'cat "{sample_ts}"')
    monkeypatch.setattr("clipper.sources.find_tool", lambda n: str(b / n) if n == "streamlink" else None)
    cfg = Config()
    cfg.source.type, cfg.source.url = "live", "https://www.twitch.tv/canal"
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "buf"), str(tmp_path / "out"), False
    cfg.reframe.detector = "none"
    eng = Engine(cfg, data_dir=tmp_path)
    c = TestClient(create_app(eng, manage_engine=False), base_url="http://localhost")
    assert c.post("/api/live/preview").status_code == 409                    # aún sin señal
    rb = RollingBuffer(cfg)
    rb.start()
    try:
        for _ in range(60):
            if rb.reader.bounds() and rb.reader.bounds()[1] - rb.reader.bounds()[0] >= 15:
                break
            time.sleep(0.5)
        bounds = rb.reader.bounds()
        assert bounds and bounds[1] - bounds[0] >= 15, rb.status()
        eng.buffer = rb
        eng.reader = rb.reader
        r = c.post("/api/live/preview?seconds=8")
        assert r.status_code == 200, r.text
        v = c.get(r.json()["url"])
        assert v.status_code == 200 and v.headers["cache-control"] == "no-store"
        out = tmp_path / "out" / "_src" / "live_preview.mp4"
        i = probe(str(out))
        assert (i.width, i.height) == (540, 960) and 6 <= i.duration <= 10
        # con un render en curso la previa no compite: 429
        assert eng.render_lock.acquire(blocking=False)
        try:
            assert c.post("/api/live/preview").status_code == 429
        finally:
            eng.render_lock.release()
    finally:
        rb.stop()


def test_source_probe_endpoint(tmp_path, monkeypatch):
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "b"), str(tmp_path / "o"), False
    c = TestClient(create_app(Engine(cfg, data_dir=tmp_path), manage_engine=False), base_url="http://localhost")
    monkeypatch.setattr(links, "probe_url", lambda cfg_, url, **k: {"platform": links.platform_of(url), "ok": True, "live": True,
                                                                     "title": "t", "duration": None, "message": "En directo ahora"})
    r = c.post("/api/source/probe", json={"url": "https://kick.com/x"})
    assert r.status_code == 200 and r.json()["platform"] == "kick" and r.json()["live"] is True
