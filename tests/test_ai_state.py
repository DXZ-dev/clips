import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

import clipper.config as C
from clipper import ai
from clipper.config import Config, LlmCfg
from clipper.engine import Engine
from clipper.web.app import create_app


def llm(**kw) -> LlmCfg:
    base = dict(enabled=True, provider="openai", base_url="http://localhost:11434/v1", model="m", api_key="K")
    return LlmCfg(**{**base, **kw})


def mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


GOOD = json.dumps({"hook": "¡No te lo vas a creer!", "title": "Lo que pasó al final", "description": "Una jugada épica.",
                   "hashtags": ["gaming", "#epico", "clutch"]})


# ------------------------------------------------------------------ cliente de IA
def test_openai_compatible_request_and_parse():
    seen = {}

    def h(req):
        seen.update(url=str(req.url), auth=req.headers.get("authorization"), body=json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Aquí tienes:\n```json\n" + GOOD + "\n```"}}]})

    out = ai.suggest(llm(), "mira esto es increíble", "keyword", "mira esto", http=mock(h))
    assert seen["url"] == "http://localhost:11434/v1/chat/completions" and seen["auth"] == "Bearer K"
    assert seen["body"]["messages"][0]["role"] == "system" and "<transcripcion>" in seen["body"]["messages"][1]["content"]
    assert out["hook"].startswith("¡No te") and out["hashtags"] == ["#gaming", "#epico", "#clutch"]


def test_anthropic_request():
    seen = {}

    def h(req):
        seen.update(url=str(req.url), key=req.headers.get("x-api-key"), ver=req.headers.get("anthropic-version"),
                    body=json.loads(req.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": GOOD}]})

    cfg = llm(provider="anthropic", base_url="", model="claude-x")
    assert ai.suggest(cfg, "hola", http=mock(h))["title"] == "Lo que pasó al final"
    assert seen["url"] == "https://api.anthropic.com/v1/messages" and seen["key"] == "K" and seen["ver"]
    assert seen["body"]["system"] and seen["body"]["messages"][0]["role"] == "user"
    with pytest.raises(ai.AiError, match="clave"):
        ai.complete(llm(provider="anthropic", api_key=""), "s", "u", http=mock(lambda r: httpx.Response(200)))


def test_openai_new_models_param_fallback():
    calls = []

    def h(req):
        b = json.loads(req.content)
        calls.append(sorted(b))
        if "max_tokens" in b:
            return httpx.Response(400, text="Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead.")
        if "temperature" in b:
            return httpx.Response(400, text="'temperature' does not support 0.7 with this model")
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    assert ai.ping(llm(), http=mock(h)) == "OK" and len(calls) == 3


def test_errors_are_clear_and_never_leak_key():
    for code, frag in [(401, "clave"), (404, "modelo"), (429, "Límite")]:
        with pytest.raises(ai.AiError, match=frag) as e:
            ai.complete(llm(api_key="SECRETKEY"), "s", "u", http=mock(lambda r, c=code: httpx.Response(c, text="nope")))
        assert "SECRETKEY" not in str(e.value)
    with pytest.raises(ai.AiError, match="desactivada"):
        ai.complete(llm(enabled=False), "s", "u")
    with pytest.raises(ai.AiError, match="modelo"):
        ai.complete(llm(model=""), "s", "u")
    with pytest.raises(ai.AiError, match="transcripción"):
        ai.suggest(llm(), "   ")

    def boom(req):
        raise httpx.ConnectError("x")
    with pytest.raises(ai.AiError, match="conectar"):
        ai.complete(llm(), "s", "u", http=mock(boom))


def test_parse_rejects_garbage_and_sanitizes():
    with pytest.raises(ai.AiError):
        ai.parse_suggestion("no hay json")
    with pytest.raises(ai.AiError):
        ai.parse_suggestion("{no: valid}")
    d = ai.parse_suggestion(json.dumps({"hook": "x" * 500 + "\x00\n", "title": "t", "description": "d", "hashtags": "a, b; #c!"}))
    assert len(d["hook"]) <= 70 and "\x00" not in d["hook"] and d["hashtags"] == ["#a", "#b", "#c"]


# ------------------------------------------------------------------ estado y variables de entorno
def test_env_overrides_and_secret_from_env(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "SETTINGS_FILE", tmp_path / "s.yaml")
    monkeypatch.setattr(C, "SECRETS_FILE", tmp_path / "x.yaml")
    monkeypatch.setenv("CLIPPER__SERVER__HOST", "0.0.0.0")
    monkeypatch.setenv("CLIPPER__COMPUTE__DEVICE", "cpu")
    monkeypatch.setenv("CLIPPER__TRIGGERS__COOLDOWN_SECONDS", "15")
    monkeypatch.setenv("CLIPPER__AI__LLM__API_KEY", "sk-from-env")
    monkeypatch.setenv("CLIPPER__AI__LLM__ENABLED", "true")
    c = C.load_config()
    assert (c.server.host, c.compute.device, c.triggers.cooldown_seconds) == ("0.0.0.0", "cpu", 15)
    assert c.ai.llm.api_key == "sk-from-env" and c.ai.llm.enabled is True
    assert "api_key" not in c.ai.llm.model_dump()


def test_state_dir_overlay_for_models(tmp_path, monkeypatch):
    state, root = tmp_path / "state", tmp_path / "root"
    (root / "models").mkdir(parents=True)
    (root / "models" / "m.tflite").write_text("x")
    monkeypatch.setattr(C, "STATE", state)
    monkeypatch.setattr(C, "ROOT", root)
    state.mkdir()
    cfg = Config()
    assert cfg.path("models/m.tflite") == (root / "models" / "m.tflite").resolve()      # viene de la imagen
    (state / "models").mkdir()
    (state / "models" / "m.tflite").write_text("y")
    assert cfg.path("models/m.tflite") == (state / "models" / "m.tflite").resolve()     # el volumen manda
    assert cfg.path("output_clips") == (state / "output_clips").resolve()               # lo nuevo va al volumen


def test_state_dir_env_in_fresh_process(tmp_path):
    code = "from clipper.config import STATE, SETTINGS_FILE, TOKENS_FILE; print(STATE); print(SETTINGS_FILE); print(TOKENS_FILE)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "CLIPPER_STATE_DIR": str(tmp_path)}).stdout.split()
    assert out == [str(tmp_path.resolve()), str(tmp_path.resolve() / "settings.local.yaml"), str(tmp_path.resolve() / "tokens.local.json")]


# ------------------------------------------------------------------ API web
@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SETTINGS_FILE", tmp_path / "settings.local.yaml")
    monkeypatch.setattr(C, "SECRETS_FILE", tmp_path / "secrets.local.yaml")
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "b"), str(tmp_path / "o"), False
    (tmp_path / "o").mkdir()
    (tmp_path / "o" / "c.json").write_text(json.dumps({"transcript": "mira esto increíble", "reason": "keyword", "detail": "mira esto"}))
    eng = Engine(cfg, data_dir=tmp_path)
    return TestClient(create_app(eng, manage_engine=False), base_url="http://localhost"), cfg, eng, tmp_path


def test_llm_key_pasted_in_panel_is_stored_privately(web):
    c, cfg, _, tp = web
    r = c.put("/api/settings", json={"ai": {"llm": {"enabled": True, "provider": "openai", "model": "gpt-x", "api_key": "sk-PASTED"}}})
    assert r.status_code == 200 and "sk-PASTED" not in r.text and "sk-PASTED" not in c.get("/api/settings").text
    assert c.get("/api/settings").json()["ai"]["llm"]["api_key_set"] is True
    assert cfg.ai.llm.api_key == "sk-PASTED"
    assert "sk-PASTED" not in (tp / "settings.local.yaml").read_text()
    assert yaml.safe_load((tp / "secrets.local.yaml").read_text())["ai.llm.api_key"] == "sk-PASTED"
    assert oct((tp / "secrets.local.yaml").stat().st_mode & 0o777) == "0o600"
    assert c.put("/api/settings", json={"ai": {"llm": {"base_url": "ftp://x"}}}).status_code == 422
    c.put("/api/settings", json={"ai": {"llm": {"clear_api_key": True}}})
    assert cfg.ai.llm.api_key == ""


def test_ai_endpoints(web, monkeypatch):
    c, cfg, eng, tp = web
    assert c.post("/api/ai/suggest", json={"clip": "c.mp4"}).status_code == 409           # desactivada
    cfg.ai.llm = llm()
    monkeypatch.setattr(ai, "_client", lambda http: mock(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": GOOD}}]})))
    r = c.post("/api/ai/suggest", json={"clip": "c.mp4"})
    assert r.status_code == 200 and r.json()["title"] == "Lo que pasó al final"
    assert c.post("/api/ai/test", json={"target": "llm"}).json()["ok"] is True
    assert c.post("/api/ai/test", json={"target": "transcribe"}).status_code == 409        # modo local
    assert c.post("/api/ai/test", json={"target": "otro"}).status_code == 422
    assert c.post("/api/ai/suggest", json={"clip": "../x.mp4"}).status_code == 400
    # regenerar el texto de una publicación pendiente
    cfg.publish.enabled = True
    cfg.publish.webhook.enabled, cfg.publish.webhook.url = True, "http://localhost/h"
    (tp / "o" / "c.mp4").write_bytes(b"x")
    pid = eng.publisher.enqueue("c.mp4", "webhook")
    out = c.post(f"/api/publish/{pid}/ai").json()
    assert out["title"] == "Lo que pasó al final" and "#gaming" in out["description"]


def test_auto_metadata_falls_back_to_template(web, monkeypatch):
    _, cfg, eng, tp = web
    cfg.publish.enabled = True
    cfg.publish.webhook.enabled, cfg.publish.webhook.url = True, "http://localhost/h"
    cfg.ai.llm, cfg.ai.auto_metadata = llm(), True
    monkeypatch.setattr(ai, "_client", lambda http: mock(lambda r: httpx.Response(500, text="caído")))
    pid = eng.publisher.enqueue("c.mp4", "webhook", {"transcript": "Hola mundo. Adiós."})
    assert eng.publisher.get(pid)["title"] == "Hola mundo."                                  # plantilla
    monkeypatch.setattr(ai, "_client", lambda http: mock(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": GOOD}}]})))
    pid = eng.publisher.enqueue("c.mp4", "webhook", {"transcript": "Hola mundo. Adiós."})
    assert eng.publisher.get(pid)["title"] == "Lo que pasó al final"                          # IA


# ------------------------------------------------------------------ ensure-models (Docker)
def test_ensure_whisper_logic(tmp_path, monkeypatch):
    from clipper import models
    monkeypatch.setattr(C, "STATE", tmp_path)
    cfg = Config()
    cfg.transcribe.model = "tiny"
    calls = []
    import faster_whisper.utils as fu
    monkeypatch.setattr(fu, "download_model", lambda name, output_dir=None, **k: (calls.append(name), Path(output_dir).mkdir(parents=True, exist_ok=True), (Path(output_dir) / "model.bin").write_text("x")))
    assert models.ensure_whisper(cfg) == "downloaded" and calls == ["tiny"]
    assert models.ensure_whisper(cfg) == "present" and calls == ["tiny"]            # ya está: no vuelve a bajar
    cfg.transcribe.provider = "api"
    assert models.ensure_whisper(cfg) == "skip"                                     # con API no hace falta modelo
    cfg.transcribe.provider, cfg.transcribe.model = "local", "base"
    monkeypatch.setenv("CLIPPER_AUTO_DOWNLOAD", "0")
    assert models.ensure_whisper(cfg) == "skip" and calls == ["tiny"]
