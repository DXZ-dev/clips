"""Actualización desde GitHub: comprobar si hay cambios nuevos y actualizar con un clic desde el panel.

La actualización la hace el propio instalador (idempotente) lanzado como una unidad de systemd APARTE (`systemd-run`):
así sobrevive al reinicio del servicio de clipper, que de otro modo mataría al actualizador a mitad de camino."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config as C
from .config import Config

UNIT = "clipper-update"
_cache: dict = {"checked_at": 0.0, "update_available": False, "latest": None, "error": None}


class UpdateError(RuntimeError):
    pass


def log_path() -> Path:
    return C.STATE / "update.log"


def _git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_HTTP_LOW_SPEED_LIMIT": "1000", "GIT_HTTP_LOW_SPEED_TIME": "30"}
    return subprocess.run(["git", "-C", str(C.ROOT), *args], capture_output=True, text=True, timeout=timeout, env=env)


def supported() -> tuple[bool, str]:
    """¿Es una instalación que se puede actualizar con git? (clon + entorno virtual propio)."""
    if sys.platform != "linux":
        return False, "La actualización desde el panel solo está disponible en Linux."
    if not shutil.which("git"):
        return False, "git no está instalado."
    if not (C.ROOT / ".git").exists() or not (C.ROOT / "install.sh").exists():
        return False, "Esta instalación no es un clon de git (¿Docker o ZIP?). Reinstala con el instalador para poder actualizar desde el panel."
    if Path(sys.prefix).resolve() != (C.ROOT / ".venv").resolve():
        return False, "clipper no corre desde el entorno virtual del instalador."
    return True, ""


def can_apply() -> tuple[bool, str]:
    ok, why = supported()
    if not ok:
        return ok, why
    if not os.environ.get("INVOCATION_ID") or not shutil.which("systemd-run"):
        return False, ("Para actualizar con un clic clipper debe correr como servicio de systemd (instala con --server o --service). "
                       "Mientras tanto: git pull y vuelve a ejecutar install.sh.")
    return True, ""


def running() -> bool:
    if not shutil.which("systemctl"):
        return False
    cmd = ["systemctl"] + ([] if os.geteuid() == 0 else ["--user"]) + ["is-active", UNIT]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out in ("active", "activating")


def _commit(rev: str) -> dict | None:
    r = _git("log", "-1", "--format=%H|%h|%cs|%s", rev)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    full, short, date, subject = (r.stdout.strip().split("|", 3) + ["", "", "", ""])[:4]
    return {"sha": full, "short": short, "date": date, "subject": subject}


def current() -> dict | None:
    ok, _ = supported()
    return _commit("HEAD") if ok else None


def check() -> dict:
    """Consulta GitHub (git fetch de la rama actual) y compara. Actualiza la caché."""
    ok, why = supported()
    st = {"checked_at": time.time(), "update_available": False, "latest": None, "error": None, "supported": ok, "why": why}
    if ok:
        try:
            branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if not branch or branch == "HEAD":
                st["error"] = "El repositorio está en HEAD desacoplado: no hay rama que seguir."
            else:
                # Sin --depth: en un clon superficial trae solo lo nuevo, conectado a tu commit actual. Con --depth 1 el commit
                # nuevo quedaría sin padre local y el `git pull --ff-only` de la actualización fallaría.
                r = _git("fetch", "origin", branch, timeout=90)
                if r.returncode != 0:
                    st["error"] = "No se pudo consultar GitHub: " + (r.stderr.strip().splitlines() or ["sin conexión"])[-1][:200]
                else:
                    latest, cur = _commit("FETCH_HEAD"), _commit("HEAD")
                    st["latest"] = latest
                    behind = bool(latest and cur and latest["sha"] != cur["sha"]
                                  and _git("merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD").returncode != 0)
                    st["update_available"] = behind
        except (OSError, subprocess.TimeoutExpired) as e:
            st["error"] = f"No se pudo comprobar: {type(e).__name__}"
    _cache.update(st)
    return dict(_cache)


def summary() -> dict:
    """Lo que ya se sabe sin consultar la red (para el estado general del panel)."""
    return {"available": bool(_cache.get("update_available")), "latest": (_cache.get("latest") or {}).get("short")}


def log_tail(n: int = 40) -> list[str]:
    try:
        return log_path().read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def state() -> dict:
    ok, why = supported()
    can, why_apply = can_apply()
    tail = log_tail()
    text = "\n".join(tail)
    result = None
    if tail and not running():
        result = "ok" if "clipper instalado" in text else ("error" if ("✘" in text or "ERROR" in text) else None)
    return {**_cache, "supported": ok, "why": why, "current": current(), "can_apply": can, "why_apply": why_apply,
            "running": running(), "log": tail, "result": result}


# ------------------------------------------------------------------ aplicar
def _venv_python_arg() -> list[str]:
    """Versión de Python con la que se creó el entorno virtual (para que el instalador no lo recree con otra)."""
    try:
        for line in (C.ROOT / ".venv" / "pyvenv.cfg").read_text().splitlines():
            if line.startswith("version"):
                maj, mnr = line.split("=")[1].strip().split(".")[:2]
                name = f"python{maj}.{mnr}"
                if shutil.which(name):
                    return ["--python", name]
    except OSError:
        pass
    return []


def _bin_dir_arg() -> list[str]:
    """Carpeta donde está el comando `clipper` de esta instalación (para regenerarlo en el mismo sitio)."""
    cands = [Path.home() / ".local" / "bin" / "clipper"]
    w = shutil.which("clipper")
    if w:
        cands.append(Path(w))
    for p in cands:
        try:
            if p.is_file() and f'APP="{C.ROOT}"' in p.read_text():
                return ["--bin-dir", str(p.parent)]
        except OSError:
            continue
    return []


def installer_args(cfg: Config) -> list[str]:
    # --service: se mantiene como servicio. Sin --lan: los ajustes (host/contraseña) ya están guardados y no se tocan.
    # --no-models: los modelos ya están descargados; la actualización no debe bajar nada grande.
    return ["--yes", "--no-models", "--service", *_venv_python_arg(), *_bin_dir_arg()]


def build_apply_command(cfg: Config) -> list[str]:
    script = ('set -e; R="$1"; shift; echo "== $(date "+%F %T") actualizando clipper desde GitHub"; '
              'git -C "$R" pull --ff-only; exec bash "$R/install.sh" "$@"')
    return ["systemd-run", *([] if os.geteuid() == 0 else ["--user"]), f"--unit={UNIT}", "--collect", "--no-block",
            f"--setenv=HOME={Path.home()}", f"--setenv=PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
            "-p", f"StandardOutput=append:{log_path()}", "-p", f"StandardError=append:{log_path()}",
            "-p", f"WorkingDirectory={C.ROOT}", "bash", "-c", script, "_", str(C.ROOT), *installer_args(cfg)]


def start_update(cfg: Config) -> None:
    ok, why = can_apply()
    if not ok:
        raise UpdateError(why)
    if running():
        raise UpdateError("Ya hay una actualización en curso.")
    log_path().parent.mkdir(parents=True, exist_ok=True)
    log_path().write_text("")                                   # registro nuevo para esta actualización
    r = subprocess.run(build_apply_command(cfg), capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise UpdateError("No se pudo lanzar la actualización: " + (r.stderr.strip() or r.stdout.strip())[:300])
