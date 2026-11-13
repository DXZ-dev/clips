import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import clipper.config as C
from clipper import auth
from clipper.config import Config
from clipper.engine import Engine
from clipper.web.app import create_app


def test_hash_and_tokens():
    h = auth.hash_password("correcta-123")
    assert h.startswith("scrypt$") and h != auth.hash_password("correcta-123")       # sal distinta cada vez
    assert auth.verify_password("correcta-123", h) and not auth.verify_password("otra", h)
    assert not auth.verify_password("x", "basura") and not auth.verify_password("x", "scrypt$zz$zz")
    t = auth.make_token(h)
    assert auth.valid_token(t, h)
    assert not auth.valid_token(t, auth.hash_password("correcta-123"))               # otra contraseña => sesión inválida
    assert not auth.valid_token(t[:-3] + "abc", h) and not auth.valid_token("", h) and not auth.valid_token(None, h)
    assert not auth.valid_token(auth.make_token(h, days=-1), h)                      # caducado


def test_throttle():
    t = auth.Throttle(limit=3, window=60)
    for _ in range(3):
        assert not t.blocked("a")
        t.fail("a")
    assert t.blocked("a") and not t.blocked("b")
    t.ok("a")
    assert not t.blocked("a")


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SETTINGS_FILE", tmp_path / "s.yaml")
    monkeypatch.setattr(C, "SECRETS_FILE", tmp_path / "x.yaml")
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "b"), str(tmp_path / "o"), False
    cfg.server.password_hash = auth.hash_password("clave-segura-1")
    eng = Engine(cfg, data_dir=tmp_path)
    mk = lambda **kw: TestClient(create_app(eng, manage_engine=False), base_url="http://192.168.1.50:7848", **kw)  # noqa: E731
    return mk, cfg, tmp_path


def test_everything_requires_login(web):
    mk, cfg, _ = web
    c = mk()
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/login"
    for path in ("/api/status", "/api/clips", "/api/settings", "/api/publish", "/api/videos", "/static/editor.js", "/clips/a.mp4"):
        assert c.get(path).status_code == 401, path
    assert c.post("/clip", json={"seconds": 30}).status_code == 401
    assert c.put("/api/settings", json={}).status_code == 401
    assert c.post("/api/videos/upload?filename=a.mp4", content=b"x").status_code == 401
    assert c.get("/login").status_code == 200 and "Contraseña" in c.get("/login").text


def test_login_flow_cookie_and_logout(web):
    mk, cfg, _ = web
    c = mk()
    assert c.post("/api/auth/login", json={"password": "mala"}).status_code == 401
    r = c.post("/api/auth/login", json={"password": "clave-segura-1"})
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert c.get("/api/status").status_code == 200 and c.get("/").status_code == 200
    assert c.get("/api/status").json()["auth"] is True
    c.post("/api/auth/logout")
    c.cookies.clear()
    assert c.get("/api/status").status_code == 401


def test_bruteforce_lockout(web):
    mk, _, _ = web
    c = mk()
    codes = [c.post("/api/auth/login", json={"password": f"mala{i}"}).status_code for i in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
    assert c.post("/api/auth/login", json={"password": "clave-segura-1"}).status_code == 429     # ni con la buena durante el bloqueo


def test_csrf_origin_still_enforced_when_logged_in(web):
    mk, _, _ = web
    c = mk()
    c.post("/api/auth/login", json={"password": "clave-segura-1"})
    assert c.put("/api/settings", json={}, headers={"origin": "http://evil.example"}).status_code == 403
    assert c.put("/api/settings", json={}, headers={"origin": "http://192.168.1.50:7848"}).status_code == 200


def test_change_password_and_old_sessions_die(web):
    mk, cfg, tp = web
    a, b = mk(), mk()
    a.post("/api/auth/login", json={"password": "clave-segura-1"})
    b.post("/api/auth/login", json={"password": "clave-segura-1"})
    assert a.post("/api/auth/password", json={"current": "mala", "new": "nueva-clave-2"}).status_code == 401
    assert a.post("/api/auth/password", json={"current": "clave-segura-1", "new": "corta"}).status_code == 422
    assert a.post("/api/auth/password", json={"current": "clave-segura-1", "new": "nueva-clave-2"}).status_code == 200
    assert a.get("/api/status").status_code == 200                       # quien cambia sigue dentro
    assert b.get("/api/status").status_code == 401                       # las demás sesiones se cierran
    assert "nueva-clave-2" not in (tp / "x.yaml").read_text() and "scrypt$" in (tp / "x.yaml").read_text()
    assert oct((tp / "x.yaml").stat().st_mode & 0o777) == "0o600"
    assert "scrypt" not in a.get("/api/settings").text                   # el hash tampoco se expone
    assert a.get("/api/settings").json()["server"]["password_hash_set"] is True


def test_without_password_stays_local_only(tmp_path):
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "b"), str(tmp_path / "o"), False
    eng = Engine(cfg, data_dir=tmp_path)
    c = TestClient(create_app(eng, manage_engine=False), base_url="http://192.168.1.50:7848")
    assert c.get("/api/status").status_code == 403                       # Host de red sin contraseña: rechazado
    assert c.get("/login", headers={"host": "localhost"}, follow_redirects=False).status_code in (302, 307)
    local = TestClient(create_app(eng, manage_engine=False), base_url="http://localhost")
    assert local.get("/api/status").status_code == 200
    assert local.get("/api/status").json()["auth"] is False
    # la primera contraseña solo se pone desde el propio servidor (el cliente de pruebas llega como "testclient")
    assert local.post("/api/auth/password", json={"new": "una-clave-larga"}).status_code == 403


def test_cli_refuses_lan_without_password_and_set_password(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "CLIPPER_STATE_DIR": str(tmp_path), "HOME": str(tmp_path)}
    run = lambda *a, inp=None: subprocess.run([sys.executable, "-m", "clipper", *a], capture_output=True, text=True, input=inp,  # noqa: E731
                                              env={**env, "PYTHONPATH": str(C.ROOT)}, cwd=C.ROOT)
    r = run("serve", "--host", "0.0.0.0", "--port", "7999")
    assert r.returncode == 2 and "sin contraseña" in r.stdout
    assert run("set-password", "--stdin", inp="corta\n").returncode == 1
    r = run("set-password", "--stdin", inp="una-contraseña-larga\n")
    assert r.returncode == 0 and "guardada" in r.stdout
    assert "server.password_hash" in (tmp_path / "secrets.local.yaml").read_text()
