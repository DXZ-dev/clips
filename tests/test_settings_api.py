import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

import clipper.settings as st
from clipper.config import Config
from clipper.detect.api_transcriber import parse_words, to_wav, transcribe_api
from clipper.engine import Engine
from clipper.web.app import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "SETTINGS_FILE", tmp_path / "settings.local.yaml")
    monkeypatch.setattr(st, "SECRETS_FILE", tmp_path / "secrets.local.yaml")
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir = str(tmp_path / "buf"), str(tmp_path / "out")
    cfg.transcribe.enabled = False
    eng = Engine(cfg)
    return TestClient(create_app(eng, manage_engine=False), base_url="http://localhost"), cfg, tmp_path


def test_settings_roundtrip_hides_key_and_persists(env):
    c, cfg, tp = env
    r = c.put("/api/settings", json={"transcribe": {"provider": "api", "api": {
        "base_url": "http://localhost:9000/v1/", "api_key": "sk-secret"}},
        "triggers": {"keywords": {"phrases": ["vamos"]}}})
    assert r.status_code == 200 and "triggers" in r.json()["changed"]
    assert "sk-secret" not in r.text and "sk-secret" not in c.get("/api/settings").text
    assert c.get("/api/settings").json()["transcribe"]["api"]["api_key_set"] is True
    assert cfg.transcribe.api.base_url == "http://localhost:9000/v1" and cfg.transcribe.api.api_key == "sk-secret"
    assert "sk-secret" not in (tp / "settings.local.yaml").read_text()          # la clave no va en settings
    assert yaml.safe_load((tp / "secrets.local.yaml").read_text())["transcribe_api_key"] == "sk-secret"
    assert oct((tp / "secrets.local.yaml").stat().st_mode & 0o777) == "0o600"
    # dejar la clave vacía en el formulario la conserva; clear_api_key la borra
    c.put("/api/settings", json={"transcribe": {"api": {"model": "x"}}})
    assert cfg.transcribe.api.api_key == "sk-secret"
    c.put("/api/settings", json={"transcribe": {"api": {"clear_api_key": True}}})
    assert cfg.transcribe.api.api_key == ""


def test_settings_validation_and_protected_paths(env):
    c, cfg, _ = env
    assert c.put("/api/settings", json={"buffer": {"dir": "/home"}}).status_code == 422
    assert c.put("/api/settings", json={"clip": {"output_dir": "/etc"}}).status_code == 422
    assert c.put("/api/settings", json={"source": {"type": "ytdlp", "url": "--exec=rm"}}).status_code == 422
    assert c.put("/api/settings", json={"source": {"type": "rtmp", "url": "ftp://x"}}).status_code == 422
    assert c.put("/api/settings", json={"transcribe": {"api": {"base_url": "file:///etc/passwd"}}}).status_code == 422
    assert cfg.buffer.dir.endswith("buf")                                  # nada se aplicó


def test_host_and_origin_guard(env):
    c, _, _ = env
    assert c.get("/api/status", headers={"host": "evil.example.com"}).status_code == 403    # DNS rebinding
    r = c.put("/api/settings", json={}, headers={"origin": "http://evil.example.com"})
    assert r.status_code == 403                                                              # CSRF
    assert c.put("/api/settings", json={}, headers={"origin": "http://localhost"}).status_code == 200


def test_live_reference_semantics(env):
    c, cfg, _ = env
    energy = c.app.router  # noqa: F841
    eng_energy_cfg = cfg.triggers.energy
    c.put("/api/settings", json={"triggers": {"energy": {"spike_db": 20}}})
    assert eng_energy_cfg.spike_db == 20          # los detectores ven el cambio sin reiniciarse


class _H(BaseHTTPRequestHandler):
    seen = {}

    def do_POST(self):
        n = int(self.headers["content-length"])
        _H.seen = {"path": self.path, "auth": self.headers.get("authorization"), "body": self.rfile.read(n)}
        out = json.dumps({"words": [{"word": " hola", "start": 0.1, "end": 0.4},
                                    {"word": "mundo", "start": 0.5, "end": 0.9}]}).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_api_transcriber_against_local_server():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = Config().transcribe.api
    cfg.base_url, cfg.api_key = f"http://127.0.0.1:{srv.server_port}/v1", "k"
    words = transcribe_api(cfg, np.zeros(16000, np.float32), 10.0, "es")
    srv.shutdown()
    assert [(w.text, round(w.start, 1)) for w in words] == [("hola", 10.1), ("mundo", 10.5)]
    assert _H.seen["path"] == "/v1/audio/transcriptions" and _H.seen["auth"] == "Bearer k"
    assert b"RIFF" in _H.seen["body"] and b"word" in _H.seen["body"]


def test_api_fallback_without_word_timestamps():
    w = parse_words({"segments": [{"start": 0, "end": 2, "text": "uno dos"}]}, 5)
    assert [x.text for x in w] == ["uno", "dos"] and w[1].start == 6.0
    assert to_wav(np.zeros(100, np.float32))[:4] == b"RIFF"
