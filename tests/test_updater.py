import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import clipper.config as C
from clipper import updater
from clipper.config import Config
from clipper.engine import Engine
from clipper.web.app import create_app


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Un «GitHub» local, un clon superficial (como el del instalador) y un directorio de estado."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(origin), str(seed))
    (seed / "install.sh").write_text("#!/bin/sh\necho instalador\n")
    (seed / "a.txt").write_text("v1")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "primera versión")
    git(seed, "branch", "-M", "main")
    git(seed, "push", "-u", "origin", "main")
    work = tmp_path / "work"
    git(tmp_path, "clone", "--depth", "1", f"file://{origin}", str(work))
    (work / ".venv").mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(C, "ROOT", work)
    monkeypatch.setattr(C, "STATE", state)
    monkeypatch.setattr(sys, "prefix", str(work / ".venv"))
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(updater, "_cache", {"checked_at": 0.0, "update_available": False, "latest": None, "error": None})
    return seed, work, state


def push_change(seed: Path, msg: str):
    (seed / "a.txt").write_text(msg)
    git(seed, "commit", "-am", msg)
    git(seed, "push")


def test_check_detects_new_commits(repo):
    seed, work, _ = repo
    st = updater.check()
    assert st["supported"] and st["update_available"] is False and st["error"] is None
    assert updater.current()["subject"] == "primera versión"
    push_change(seed, "arreglo importante")
    st = updater.check()
    assert st["update_available"] is True and st["latest"]["subject"] == "arreglo importante"
    assert updater.summary() == {"available": True, "latest": st["latest"]["short"]}
    # tras actualizar (pull) vuelve a estar al día
    git(work, "pull", "--ff-only")
    assert updater.check()["update_available"] is False and updater.summary()["available"] is False


def test_check_reports_network_error_instead_of_crashing(repo):
    _, work, _ = repo
    git(work, "remote", "set-url", "origin", "file:///no/existe.git")
    st = updater.check()
    assert st["update_available"] is False and "No se pudo consultar GitHub" in st["error"]


def test_unsupported_installs_are_explained(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "ROOT", tmp_path)                                      # sin .git (Docker o ZIP)
    ok, why = updater.supported()
    assert not ok and "clon de git" in why
    st = updater.check()
    assert st["supported"] is False and st["update_available"] is False


def test_apply_requires_systemd_service(repo):
    ok, why = updater.can_apply()
    assert not ok and "servicio de systemd" in why
    with pytest.raises(updater.UpdateError, match="servicio"):
        updater.start_update(Config())


def test_apply_command_runs_detached_and_keeps_settings(repo, monkeypatch):
    _, work, state = repo
    cmd = updater.build_apply_command(Config())
    assert cmd[0] == "systemd-run" and "--unit=clipper-update" in cmd and "--no-block" in cmd and "--collect" in cmd
    assert f"StandardOutput=append:{state / 'update.log'}" in cmd and f"WorkingDirectory={work}" in cmd
    script = cmd[cmd.index("-c") + 1]
    assert "git -C \"$R\" pull --ff-only" in script and 'install.sh' in script and "set -e" in script
    args = cmd[cmd.index("_") + 2:]
    assert cmd[cmd.index("_") + 1] == str(work)
    assert "--yes" in args and "--no-models" in args and "--service" in args
    assert "--lan" not in args and "--server" not in args                           # no toca host/contraseña ya guardados
    assert any(a.startswith("HOME=") for a in cmd[2].split("=", 1)[1:]) or any(c.startswith("--setenv=HOME=") for c in cmd)


def test_start_update_launches_and_blocks_double_run(repo, tmp_path, monkeypatch):
    _, _, state = repo
    bindir = tmp_path / "bin"
    bindir.mkdir()
    rec = tmp_path / "rec.txt"
    fake = bindir / "systemd-run"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {rec}\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("INVOCATION_ID", "abc")
    (state / "update.log").write_text("log viejo")
    updater.start_update(Config())
    assert "--unit=clipper-update" in rec.read_text() and (state / "update.log").read_text() == ""     # registro nuevo
    monkeypatch.setattr(updater, "running", lambda: True)
    with pytest.raises(updater.UpdateError, match="en curso"):
        updater.start_update(Config())


def test_state_reads_result_from_log(repo, monkeypatch):
    _, _, state = repo
    (state / "update.log").write_text("== actualizando\n...\n✔ clipper instalado   (registro: x)\n")
    s = updater.state()
    assert s["result"] == "ok" and s["running"] is False and "clipper instalado" in s["log"][-1]
    (state / "update.log").write_text("== actualizando\n✘ Falló la línea 5\n")
    assert updater.state()["result"] == "error"


def test_update_endpoints(repo, tmp_path):
    seed, work, _ = repo
    cfg = Config()
    cfg.buffer.dir, cfg.clip.output_dir, cfg.transcribe.enabled = str(tmp_path / "b"), str(tmp_path / "o"), False
    eng = Engine(cfg, data_dir=tmp_path)
    c = TestClient(create_app(eng, manage_engine=False), base_url="http://localhost")
    assert c.get("/api/update").json()["supported"] is True
    push_change(seed, "otra mejora")
    r = c.post("/api/update/check").json()
    assert r["update_available"] is True and r["latest"]["subject"] == "otra mejora" and r["can_apply"] is False
    assert c.get("/api/status").json()["update"]["available"] is True                   # el menú puede avisar
    assert c.post("/api/update/apply").status_code == 409                                # sin systemd en el test
    assert c.put("/api/settings", json={"update": {"auto_check": False}}).status_code == 200 and cfg.update.auto_check is False
