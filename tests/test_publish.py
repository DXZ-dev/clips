import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from clipper.config import Config
from clipper.engine import Engine
from clipper.publish import metadata
from clipper.publish.base import AuthError, PublishError, RetryableError, TokenStore
from clipper.publish.instagram import Instagram
from clipper.publish.queue import PublishQueue
from clipper.publish.tiktok import TikTok
from clipper.publish.webhook import Webhook
from clipper.publish.youtube import YouTube
from clipper.web.app import create_app


@pytest.fixture
def clip(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00" * 2048)
    return p


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def cfg_with(tmp_path, **kw):
    cfg = Config()
    cfg.clip.output_dir = str(tmp_path)
    cfg.publish.enabled = True
    for k, v in kw.items():
        for f, val in v.items():
            setattr(getattr(cfg.publish, k), f, val)
    return cfg


# ---------------------------------------------------------------- metadatos
def test_metadata_templates():
    cfg = Config().publish
    t, d = metadata.render(cfg, {"transcript": "Mira esto, es increíble. Y sigue.", "reason": "keyword"})
    assert t == "Mira esto, es increíble." and "#shorts" in d
    t, _ = metadata.render(cfg, {"reason": "laughter"})
    assert "😂" in t
    long = metadata.headline("palabra " * 40, "", "")
    assert len(long) <= 72 and long.endswith("…")
    cfg.title_template = "{inventado} {reason}"
    assert metadata.render(cfg, {"reason": "x"})[0] == "{inventado} x"    # variable desconocida: no rompe


# ---------------------------------------------------------------- YouTube
def test_youtube_resumable_flow(tmp_path, clip):
    calls = []

    def h(req: httpx.Request):
        calls.append((req.method, str(req.url).split("?")[0], req.headers.get("authorization")))
        if req.url.host == "oauth2.googleapis.com":
            assert b"refresh_token=r1" in req.content
            return httpx.Response(200, json={"access_token": "A1", "expires_in": 3600})
        if req.method == "POST":
            body = json.loads(req.content)
            assert body["status"]["privacyStatus"] == "private" and body["snippet"]["title"] == "T"
            assert req.headers["x-upload-content-length"] == "2048"
            return httpx.Response(200, headers={"location": "https://upload.example/u1"})
        assert len(req.read()) == 2048
        return httpx.Response(200, json={"id": "vid123"})

    cfg = cfg_with(tmp_path, youtube={"client_id": "cid", "client_secret": "sec"})
    ts = TokenStore(tmp_path / "t.json")
    ts.put("youtube", {"refresh_token": "r1"})
    res = YouTube(cfg, ts, client(h)).publish(clip, "T", "D", {})
    assert res.url == "https://www.youtube.com/shorts/vid123"
    assert [c[0] for c in calls] == ["POST", "POST", "PUT"] and calls[1][2] == "Bearer A1"


def test_youtube_not_connected_is_auth_error(tmp_path, clip):
    cfg = cfg_with(tmp_path, youtube={"client_id": "c", "client_secret": "s"})
    with pytest.raises(AuthError):
        YouTube(cfg, TokenStore(tmp_path / "t.json"), client(lambda r: httpx.Response(200))).publish(clip, "t", "d", {})


def test_oauth_authorize_url_and_exchange(tmp_path):
    cfg = cfg_with(tmp_path, tiktok={"client_key": "ck", "client_secret": "cs"})
    ts = TokenStore(tmp_path / "t.json")

    def h(req):
        assert b"client_key=ck" in req.content and b"grant_type=authorization_code" in req.content
        return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 100})

    tt = TikTok(cfg, ts, client(h))
    url = tt.authorize_url("st")
    assert "client_key=ck" in url and "state=st" in url and "scope=video.publish" in url
    assert tt.redirect_uri() == "http://localhost:8000/oauth/tiktok/callback"
    tt.exchange_code("CODE")
    assert ts.get("tiktok")["refresh_token"] == "RT" and tt.connected()
    assert oct(ts.path.stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------- TikTok
def test_tiktok_flow_and_failure(tmp_path, clip):
    state = {"polls": 0}

    def h(req):
        u = str(req.url)
        if "init" in u:
            b = json.loads(req.content)
            assert b["post_info"]["privacy_level"] == "SELF_ONLY" and b["source_info"]["video_size"] == 2048
            return httpx.Response(200, json={"data": {"publish_id": "p1", "upload_url": "https://up.example/x"},
                                             "error": {"code": "ok"}})
        if req.method == "PUT":
            assert req.headers["content-range"] == "bytes 0-2047/2048"
            return httpx.Response(201)
        state["polls"] += 1
        return httpx.Response(200, json={"data": {"status": "PUBLISH_COMPLETE" if state["polls"] > 1 else "PROCESSING_UPLOAD",
                                                  "publicly_available_post_id": ["999"]}})

    cfg = cfg_with(tmp_path, tiktok={"client_key": "k", "client_secret": "s"})
    ts = TokenStore(tmp_path / "t.json")
    ts.put("tiktok", {"access_token": "AT", "refresh_token": "RT", "expires_at": time.time() + 999})
    import clipper.publish.tiktok as tk
    tk.time.sleep = lambda s: None
    assert TikTok(cfg, ts, client(h)).publish(clip, "t", "d", {}).id == "p1"

    def bad(req):
        if "init" in str(req.url):
            return httpx.Response(200, json={"data": {"publish_id": "p", "upload_url": "https://u/x"}, "error": {"code": "ok"}})
        if req.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(200, json={"data": {"status": "FAILED", "fail_reason": "spam_risk"}})

    with pytest.raises(PublishError, match="spam_risk"):
        TikTok(cfg, ts, client(bad)).publish(clip, "t", "d", {})


# ---------------------------------------------------------------- Instagram y webhook
def test_instagram_reels_flow(tmp_path, clip):
    import clipper.publish.instagram as ig
    ig.time.sleep = lambda s: None
    seq = []

    def h(req):
        u = str(req.url)
        seq.append(req.method + " " + u.split("?")[0].split("com/")[-1])
        if u.endswith("/media") and req.method == "POST":
            assert b"media_type=REELS" in req.content and b"upload_type=resumable" in req.content
            return httpx.Response(200, json={"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v21.0/C1"})
        if "rupload" in u:
            assert req.headers["authorization"] == "OAuth TOK" and req.headers["file_size"] == "2048"
            return httpx.Response(200, json={"success": True})
        if u.split("?")[0].endswith("/C1"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if "media_publish" in u:
            return httpx.Response(200, json={"id": "M1"})
        return httpx.Response(200, json={"permalink": "https://instagram.com/reel/abc"})

    cfg = cfg_with(tmp_path, instagram={"ig_user_id": "17", "access_token": "TOK"})
    res = Instagram(cfg, TokenStore(tmp_path / "t.json"), client(h)).publish(clip, "Hola", "Hola", {})
    assert res.url == "https://instagram.com/reel/abc" and "POST v21.0/17/media_publish" in seq


def test_webhook_sends_file_and_auth(tmp_path, clip):
    def h(req):
        assert req.headers["authorization"] == "Bearer z" and b"audio" not in req.content[:0]
        assert b'name="file"' in req.content and b'name="title"' in req.content
        return httpx.Response(200, json={"id": "r1", "url": "https://x.example/p/1"})

    cfg = cfg_with(tmp_path, webhook={"url": "http://localhost:5678/hook", "auth_header": "Bearer z"})
    assert Webhook(cfg, TokenStore(tmp_path / "t.json"), client(h)).publish(clip, "T", "D", {"a": 1}).url.endswith("/p/1")


# ---------------------------------------------------------------- cola
def make_queue(tmp_path, handler=None, **kw):
    cfg = cfg_with(tmp_path, **kw)
    q = PublishQueue(cfg, tmp_path / "q.db", TokenStore(tmp_path / "t.json"), client(handler or (lambda r: httpx.Response(200, json={}))))
    return cfg, q


def test_review_mode_requires_approval(tmp_path, clip):
    cfg, q = make_queue(tmp_path, webhook={"enabled": True, "url": "http://localhost/h", "mode": "review"})
    cfg.publish.dry_run = True
    (pid,) = q.on_clip(clip, {"transcript": "Hola mundo."})
    assert q.get(pid)["status"] == "pending_review" and q.process_one() is None      # no se sube sola
    q.edit(pid, "Mi título", None)
    q.approve(pid)
    assert q.process_one() == pid
    it = q.get(pid)
    assert it["status"] == "published" and it["title"] == "Mi título" and it["url"].startswith("dry-run://")


def test_auto_mode_limits_and_manual_clips(tmp_path, clip):
    cfg, q = make_queue(tmp_path, webhook={"enabled": True, "url": "http://localhost/h", "mode": "auto"})
    cfg.publish.dry_run, cfg.publish.min_gap_minutes, cfg.publish.max_per_day = True, 0, 2
    assert q.on_clip(clip, {}, manual=True) == []                    # manuales fuera por defecto
    ids = [q.on_clip(clip, {})[0] for _ in range(3)]
    assert [q.get(i)["status"] for i in ids] == ["queued"] * 3
    assert q.process_one() == ids[0] and q.process_one() == ids[1]
    assert q.process_one() is None and q.get(ids[2])["status"] == "queued"   # tope diario


def test_min_gap_between_posts(tmp_path, clip):
    cfg, q = make_queue(tmp_path, webhook={"enabled": True, "url": "http://localhost/h", "mode": "auto"})
    cfg.publish.dry_run, cfg.publish.min_gap_minutes = True, 20
    a, b = q.on_clip(clip, {})[0], q.on_clip(clip, {})[0]
    assert q.process_one() == a and q.process_one() is None


def test_retry_backoff_then_fail_and_permanent_error(tmp_path, clip):
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(503, text="busy")

    cfg, q = make_queue(tmp_path, h, webhook={"enabled": True, "url": "http://localhost/h", "mode": "auto"})
    cfg.publish.min_gap_minutes, cfg.publish.max_attempts = 0, 2
    pid = q.on_clip(clip, {})[0]
    q.process_one()
    it = q.get(pid)
    assert it["status"] == "queued" and it["attempts"] == 1 and it["not_before"] > time.time() + 100
    assert q.process_one() is None                                    # espera de reintento
    q._set(pid, not_before=None)
    q.process_one()
    assert q.get(pid)["status"] == "failed" and len(calls) == 2

    cfg2, q2 = make_queue(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path,
                          lambda r: httpx.Response(401, text="no"),
                          webhook={"enabled": True, "url": "http://localhost/h", "mode": "auto"})
    cfg2.publish.min_gap_minutes = 0
    pid2 = q2.enqueue(clip.name, "webhook", {}, approved=True)
    q2.process_one()
    assert q2.get(pid2)["status"] == "failed" and q2.get(pid2)["attempts"] == 1   # 401: sin reintentos


def test_missing_clip_and_path_traversal(tmp_path):
    cfg, q = make_queue(tmp_path, webhook={"enabled": True, "url": "http://localhost/h", "mode": "auto"})
    cfg.publish.dry_run = True
    pid = q.enqueue("nope.mp4", "webhook", {}, approved=True)
    q.process_one()
    assert q.get(pid)["status"] == "failed"
    with pytest.raises(ValueError):
        q.clip_path("../etc/passwd")


def test_crash_recovery_requeues_uploading(tmp_path, clip):
    cfg, q = make_queue(tmp_path, webhook={"enabled": True, "url": "http://localhost/h"})
    pid = q.enqueue(clip.name, "webhook", {}, approved=True)
    q._set(pid, status="uploading")
    q2 = PublishQueue(cfg, tmp_path / "q.db", TokenStore(tmp_path / "t.json"))
    assert q2.get(pid)["status"] == "queued"


# ---------------------------------------------------------------- API web
def test_web_publish_flow(tmp_path, clip, monkeypatch):
    cfg = cfg_with(tmp_path, webhook={"enabled": True, "url": "http://localhost/h", "mode": "review"},
                   youtube={"client_id": "cid", "client_secret": "S3CR3T-VALUE"})
    cfg.buffer.dir, cfg.transcribe.enabled, cfg.publish.dry_run = str(tmp_path / "buf"), False, True
    eng = Engine(cfg, data_dir=tmp_path)
    c = TestClient(create_app(eng, manage_engine=False), base_url="http://localhost")
    assert c.get("/api/publish").json()["destinations"]["webhook"]["configured"] is True
    r = c.post("/api/publish/enqueue", json={"clip": "clip.mp4", "platforms": ["webhook"]})
    pid = r.json()["ids"][0]
    assert eng.publisher.get(pid)["status"] == "queued"                    # acción explícita = aprobada
    assert c.post("/api/publish/enqueue", json={"clip": "clip.mp4", "platforms": ["tiktok"]}).status_code == 422
    assert c.post("/api/publish/enqueue", json={"clip": "../x.mp4", "platforms": ["webhook"]}).status_code in (400, 404)
    assert c.post(f"/api/publish/{pid}/approve").status_code == 409        # ya en cola
    assert c.post(f"/api/publish/{pid}/reject").json()["status"] == "rejected"
    assert c.put(f"/api/publish/{pid}", json={"title": "x"}).status_code == 409
    assert c.delete(f"/api/publish/{pid}").status_code == 200
    # OAuth: state obligatorio y de un solo uso
    loc = c.get("/oauth/youtube/start", follow_redirects=False)
    assert loc.status_code in (302, 307) and "accounts.google.com" in loc.headers["location"]
    assert c.get("/oauth/youtube/callback?code=x&state=falso").status_code == 400
    assert c.get("/oauth/webhook/start").status_code == 404
    st = [x for x in loc.headers["location"].split("&") if x.startswith("state=")][0][6:]
    monkeypatch.setattr(type(eng.publisher.providers["youtube"]), "exchange_code", lambda self, code: self.tokens.put("youtube", {"refresh_token": "R-TOKEN-VALUE"}))
    assert c.get(f"/oauth/youtube/callback?code=ok&state={st}", follow_redirects=False).status_code in (302, 307)
    assert c.get(f"/oauth/youtube/callback?code=ok&state={st}").status_code == 400      # no reutilizable
    assert c.get("/api/publish").json()["destinations"]["youtube"]["connected"] is True
    assert "R-TOKEN-VALUE" not in c.get("/api/publish").text
    assert "S3CR3T-VALUE" not in c.get("/api/settings").text and "S3CR3T-VALUE" not in c.get("/api/status").text
    assert c.post("/api/publish/youtube/disconnect").status_code == 200
