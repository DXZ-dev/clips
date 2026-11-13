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


# ------------------------------------------------------------------ ventana en vivo (HLS) y modo automático
def make_segments(buf: Path, seconds: int = 30) -> None:
    buf.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=25:d={seconds}", "-f", "lavfi", "-i",
                    f"sine=f=300:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-c:a", "aac", "-f", "segment",
                    "-segment_time", "5", "-segment_format", "mpegts", "-segment_list", str(buf / "list.csv"), "-segment_list_type", "csv",
                    "-segment_list_flags", "live", "-reset_timestamps", "0", str(buf / "seg_%08d.ts")], check=True)


def test_hls_playlist_and_segments(tmp_path):
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "buf"), str(tmp_path / "out"), False
    c = TestClient(create_app(Engine(cfg, data_dir=tmp_path), manage_engine=False), base_url="http://localhost")
    assert c.get("/api/live/stream.m3u8").status_code == 404                      # sin señal
    make_segments(tmp_path / "buf", 40)
    r = c.get("/api/live/stream.m3u8")
    assert r.status_code == 200 and "mpegurl" in r.headers["content-type"] and r.headers["cache-control"] == "no-store"
    lines = r.text.splitlines()
    assert lines[0] == "#EXTM3U" and "#EXT-X-ENDLIST" not in r.text                  # sin ENDLIST = directo
    entries = [l for l in lines if l.startswith("/api/live/seg/")]
    assert len(entries) == 5 and entries[-1] == "/api/live/seg/seg_00000007.ts"      # ventana de los 5 últimos
    seq = [l for l in lines if l.startswith("#EXT-X-MEDIA-SEQUENCE")][0]
    assert seq == "#EXT-X-MEDIA-SEQUENCE:3"
    assert int([l for l in lines if l.startswith("#EXT-X-TARGETDURATION")][0].split(":")[1]) >= 5
    seg = c.get(entries[0])
    assert seg.status_code == 200 and seg.headers["content-type"] == "video/mp2t" and seg.content[0] == 0x47   # sincronismo MPEG-TS
    assert c.get("/api/live/seg/seg_1.ts").status_code == 400 and c.get("/api/live/seg/..%2Flist.csv").status_code in (400, 404)
    assert c.get("/api/live/seg/seg_00000999.ts").status_code == 404
    # hueco en la numeración (reconexión) => #EXT-X-DISCONTINUITY
    csv = (tmp_path / "buf" / "list.csv").read_text().splitlines()
    (tmp_path / "buf" / "seg_00000020.ts").write_bytes((tmp_path / "buf" / "seg_00000007.ts").read_bytes())
    (tmp_path / "buf" / "list.csv").write_text("\n".join(csv + ["seg_00000020.ts,40.0,45.0"]))
    assert "#EXT-X-DISCONTINUITY" in c.get("/api/live/stream.m3u8").text


def test_hls_requires_login_when_password_set(tmp_path):
    from clipper import auth
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "buf"), str(tmp_path / "out"), False
    cfg.server.password_hash = auth.hash_password("clave-larga-1")
    make_segments(tmp_path / "buf", 12)
    c = TestClient(create_app(Engine(cfg, data_dir=tmp_path), manage_engine=False), base_url="http://192.168.1.5:7848")
    assert c.get("/api/live/stream.m3u8").status_code == 401 and c.get("/api/live/seg/seg_00000000.ts").status_code == 401
    c.post("/api/auth/login", json={"password": "clave-larga-1"})
    assert c.get("/api/live/stream.m3u8").status_code == 200


def test_auto_mode_rate_limit_and_toggle(tmp_path):
    from clipper.detect.triggers import Trigger
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "buf"), str(tmp_path / "out"), False
    cfg.triggers.cooldown_seconds, cfg.triggers.max_per_hour = 10, 2
    eng = Engine(cfg, data_dir=tmp_path)
    for i in range(5):                                                   # 5 momentos separados más que el enfriamiento
        eng._auto(Trigger("keyword", 100 + i * 30, "mira esto"))
        eng._flush_pending(force=True)
    assert len(eng.jobs) == 2 and any("límite de 2" in e["detail"] for e in eng.events)
    assert eng.status()["auto"] == {"enabled": True, "max_per_hour": 2, "last_hour": 2}
    cfg.triggers.auto_enabled = False                                    # modo manual: no se crea nada solo
    n = len(eng.jobs)
    eng._auto(Trigger("keyword", 1000, "x"))
    eng._flush_pending(force=True)
    assert len(eng.jobs) == n and eng.status()["auto"]["enabled"] is False


def test_live_frame_and_facecam_detect_endpoints(tmp_path):
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "buf"), str(tmp_path / "out"), False
    cfg.reframe.detector = "none"
    eng = Engine(cfg, data_dir=tmp_path)
    c = TestClient(create_app(eng, manage_engine=False), base_url="http://localhost")
    assert c.get("/api/live/frame").status_code == 409 and c.post("/api/live/facecam/detect").status_code == 409
    make_segments(tmp_path / "buf", 30)
    r = c.get("/api/live/frame?w=480")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    d = c.post("/api/live/facecam/detect")
    assert d.status_code == 200 and d.json() == {"facecam": None}                 # sin detector no hay cámara
    # guardar la cámara a mano desde el panel (y volver a la autodetección)
    ok = c.put("/api/settings", json={"layout": {"mode": "split", "facecam": {"x": 0.0, "y": 0.0, "w": 0.285, "h": 0.37}}})
    assert ok.status_code == 200 and cfg.layout.facecam.w == 0.285 and cfg.layout.mode == "split"
    assert c.put("/api/settings", json={"layout": {"facecam": {"x": 0.9, "y": 0, "w": 0.3, "h": 0.3}}}).status_code == 422   # se sale del fotograma
    assert c.put("/api/settings", json={"layout": {"facecam": None}}).status_code == 200 and cfg.layout.facecam is None


# ------------------------------------------------------------------ pausa, títulos y canal de cada clip
def test_pause_resume_blocks_only_automatic_clips(tmp_path):
    from clipper.detect.triggers import Trigger
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "b"), str(tmp_path / "o"), False
    eng = Engine(cfg, data_dir=tmp_path)
    c = TestClient(create_app(eng, manage_engine=False), base_url="http://localhost")
    assert c.get("/api/status").json()["paused"] is False
    assert c.post("/api/auto/pause").json() == {"paused": True} and c.get("/api/status").json()["paused"] is True
    eng._auto(Trigger("keyword", 100, "mira esto"))
    eng._flush_pending(force=True)
    assert len(eng.jobs) == 0                                              # en pausa no se crea nada solo
    assert any("PAUSA" in e["detail"] for e in eng.events)
    assert c.post("/api/auto/resume").json() == {"paused": False}
    eng._auto(Trigger("keyword", 200, "mira esto"))
    eng._flush_pending(force=True)
    assert len(eng.jobs) == 1                                              # reanudado: vuelve a funcionar


def test_clip_labels_title_and_channel(tmp_path):
    from clipper.pipeline import clip_labels
    from clipper.subtitles import Word
    cfg = Config()
    cfg.source.type, cfg.source.url = "live", "https://kick.com/epidemic77"
    words = [Word(1, 1.4, "¡Mira"), Word(1.4, 1.8, "esto!"), Word(2, 2.5, "Es increíble.")]
    lab = clip_labels(cfg, words, "keyword", "mira esto")
    assert lab == {"title": "Mira esto!", "platform": "kick", "channel": "epidemic77"} or lab["title"].startswith("Mira esto")
    assert lab["platform"] == "kick" and lab["channel"] == "epidemic77"
    assert clip_labels(cfg, [], "laughter", "")["title"] == "Se lía una buena 😂"                 # sin texto: título por tipo de momento
    cfg.source.type, cfg.source.url = "file", ""
    assert clip_labels(cfg, words, "keyword", "")["platform"] == "archivo"
    cfg.source.type, cfg.source.url = "live", "https://www.youtube.com/@MrBeast/live"
    assert clip_labels(cfg, words, "keyword", "")["channel"] == "@MrBeast"
    # con la IA activada pero caída, se queda el texto del clip
    cfg.ai.auto_metadata, cfg.ai.llm.enabled, cfg.ai.llm.model, cfg.ai.llm.base_url = True, True, "m", "http://127.0.0.1:9/v1"
    assert clip_labels(cfg, words, "keyword", "")["title"].startswith("Mira esto")


def test_list_clips_exposes_title_and_channel_and_title_feeds_publication(tmp_path):
    import json as _json
    from clipper.publish import metadata
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "b"), str(tmp_path / "o"), False
    (tmp_path / "o").mkdir()
    (tmp_path / "o" / "c.mp4").write_bytes(b"x")
    meta = {"reason": "keyword", "title": "Mi título automático", "platform": "kick", "channel": "epidemic77", "score": 4.2, "transcript": "hola"}
    (tmp_path / "o" / "c.json").write_text(_json.dumps(meta))
    row = Engine(cfg, data_dir=tmp_path).list_clips()[0]
    assert (row["title"], row["platform"], row["channel"], row["score"]) == ("Mi título automático", "kick", "epidemic77", 4.2)
    assert metadata.render(cfg.publish, meta)[0] == "Mi título automático"          # el título del clip pasa a la publicación


# ------------------------------------------------------------------ configuración automática
def _eng(tmp_path, with_segments=True, models=("small",)):
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "buf"), str(tmp_path / "out"), False
    cfg.compute.device = "cpu"
    import clipper.config as CC
    (tmp_path / "models").mkdir(exist_ok=True)
    for m in models:
        (tmp_path / "models" / f"whisper-{m}").mkdir(exist_ok=True)
    CC.STATE = tmp_path
    if with_segments:
        make_segments(tmp_path / "buf", 20)
    return Engine(cfg, data_dir=tmp_path), cfg


def test_autoconfig_with_camera_found(tmp_path, monkeypatch):
    from clipper import autoconfig
    eng, cfg = _eng(tmp_path)
    cam = {"x": 0.0, "y": 0.0, "w": 0.285, "h": 0.37}
    monkeypatch.setattr(autoconfig, "_detect_cam_local", lambda e: type("R", (), {"model_dump": lambda s: cam})())
    cfg.publish.tiktok.enabled = True
    res = autoconfig.run(eng, lambda: b"")
    assert all(s["status"] != "warn" or s["name"] == "Transcripción" for s in res["steps"])
    assert cfg.layout.mode == "split" and cfg.layout.facecam.w == 0.285 and cfg.layout.cam_follow and cfg.layout.gameplay == "center"
    assert cfg.subtitles.position == "seam" and cfg.subtitles.highlight_color == "#00E676" and cfg.subtitles.reviewed is True
    assert cfg.triggers.auto_enabled and cfg.triggers.min_score == 2.5 and cfg.triggers.visual.enabled
    assert cfg.publish.tiktok.mode == "review"                                   # nunca publica solo sin que lo decidas
    assert cfg.transcribe.enabled and cfg.transcribe.model == "small"             # el que hay disponible (CPU de pocos núcleos pediría base: sube a lo que exista ≤ deseado)


def test_autoconfig_without_camera_uses_auto_layout_and_ai_when_available(tmp_path, monkeypatch):
    from clipper import ai, autoconfig
    eng, cfg = _eng(tmp_path, models=("base", "small"))
    monkeypatch.setattr(autoconfig, "_detect_cam_local", lambda e: None)
    res = autoconfig.run(eng, lambda: b"")
    assert cfg.layout.mode == "auto" and cfg.layout.facecam is None and cfg.subtitles.position == "lower_third"
    # con la IA de visión configurada, es la que ubica la cámara
    cfg.ai.llm.enabled = True
    monkeypatch.setattr(ai, "locate_faces", lambda llm, jpeg, http=None: {"facecam": {"x": 0.7, "y": 0.6, "w": 0.28, "h": 0.37}, "faces": []})
    res = autoconfig.run(eng, lambda: b"jpeg")
    assert cfg.layout.mode == "split" and cfg.layout.facecam.x == 0.7 and any("IA de tu API" in s["detail"] for s in res["steps"])


def test_autoconfig_without_signal_keeps_layout_and_endpoint(tmp_path):
    from clipper import autoconfig
    eng, cfg = _eng(tmp_path, with_segments=False, models=())
    cfg.layout.mode = "fit"
    c = TestClient(create_app(eng, manage_engine=False), base_url="http://localhost")
    r = c.post("/api/autoconfig").json()
    names = {s["name"]: s for s in r["steps"]}
    assert names["Señal del directo"]["status"] == "warn" and names["Cámara y diseño"]["status"] == "skip"
    assert names["Transcripción"]["status"] == "warn" and "download-model" in names["Transcripción"]["detail"]
    assert cfg.layout.mode == "fit" and cfg.subtitles.reviewed is True and cfg.triggers.auto_enabled


# ------------------------------------------------------------------ IA de visión: ubicar cámara y rostros
def test_vision_locate_faces_for_each_provider(monkeypatch):
    import base64
    import json as _json
    import httpx
    from clipper import ai
    from clipper.config import LlmCfg
    reply = _json.dumps({"facecam": {"box": [0, 0, 285, 370]}, "faces": [{"box": [60, 40, 230, 300]}, {"box": [0.6, 0.5, 0.7, 0.7]}]})
    jpeg = b"\xff\xd8fakejpeg"
    seen = {}

    def gem(req):
        b = _json.loads(req.content)
        seen["g"] = b["contents"][0]["parts"][0]["inlineData"]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": reply}]}}]})

    def ant(req):
        b = _json.loads(req.content)
        seen["a"] = b["messages"][0]["content"][0]
        return httpx.Response(200, json={"content": [{"type": "text", "text": reply}]})

    def oai(req):
        b = _json.loads(req.content)
        seen["o"] = b["messages"][1]["content"][1]
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    for prov, h, key in (("gemini", gem, "g"), ("anthropic", ant, "a"), ("openai", oai, "o")):
        llm = LlmCfg(enabled=True, provider=prov, model="m", api_key="K", base_url="https://x.example/v1")
        out = ai.locate_faces(llm, jpeg, http=httpx.Client(transport=httpx.MockTransport(h)))
        assert out["facecam"] == {"x": 0.0, "y": 0.0, "w": 0.285, "h": 0.37}                  # 0..1000 -> 0..1
        assert out["faces"][0] == {"x": 0.06, "y": 0.04, "w": 0.17, "h": 0.26} and out["faces"][1]["x"] == 0.6   # admite 0..1 y 0..1000
    assert base64.b64decode(seen["g"]["data"]) == jpeg and seen["a"]["source"]["media_type"] == "image/jpeg"
    assert seen["o"]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # sin cámara / cajas inválidas
    none = _json.dumps({"facecam": None, "faces": [{"box": [0.1, 0.1, 0.11, 0.11]}]})
    llm = LlmCfg(enabled=True, provider="openai", model="m", base_url="https://x.example/v1")
    assert ai.locate_faces(llm, jpeg, http=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"choices": [{"message": {"content": none}}]})))) == {"facecam": None, "faces": []}


# ------------------------------------------------------------------ seguimiento real del rostro
def test_pl_expr_matches_samples():
    from clipper.tracking import pl_expr
    assert pl_expr([10.0, 10.0, 10.0], 6) == "10.00"                                  # quieto: constante
    e = pl_expr([0.0, 6.0, 6.0], 6)
    assert e.startswith("0.00+(36.000)*clip(t-0.0000,0,0.1667)") and e.count("clip(") == 1


def test_face_tracking_follows_a_moving_face(tmp_path):
    pytest.importorskip("skimage")
    import subprocess as sp
    import cv2
    from skimage import data
    from clipper.config import load_config
    from clipper.config import RectCfg
    from clipper.tracking import track_face_in
    cfg = load_config()
    if not cfg.path(cfg.reframe.mediapipe_model).exists():
        pytest.skip("falta el modelo de MediaPipe")
    cv2.imwrite(str(tmp_path / "f.png"), data.astronaut()[:, :, ::-1])
    vid = tmp_path / "m.mp4"
    # una cámara de 640x360 donde el rostro se desplaza de izquierda a derecha
    sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=1280x720:r=25:d=8", "-loop", "1", "-framerate", "25", "-i", str(tmp_path / "f.png"),
            "-filter_complex", "[1]crop=330:330:85:0,scale=240:240[f];[0][f]overlay=x='60+t*60':y=60", "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", str(vid)], check=True)
    fp = track_face_in(["-i", str(vid)], probe(str(vid)), cfg, RectCfg(x=0.0, y=0.0, w=0.5, h=0.5))
    assert fp is not None and len(fp.fx) >= 40
    assert fp.fx[-1] - fp.fx[0] > 0.2                                                       # sigue el desplazamiento
    assert max(abs(b - a) for a, b in zip(fp.fx, fp.fx[1:])) < 0.08                         # sin saltos (suavizado)
