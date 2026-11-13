"""Varias transmisiones a la vez: cada una es una instancia de clipper con su propio buffer, ajustes y puerto,
pero todas guardan sus clips en la misma carpeta (la pestaña Clips muestra todo y se filtra por canal)."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from . import config as C
from .config import Config


def plan(cfg: Config, n: int, launcher: str | None = None) -> dict:
    """Calcula puerto, carpeta de estado y unidad systemd de la instancia n (2 o 3)."""
    if n not in (2, 3):
        raise ValueError("solo se admiten las transmisiones 2 y 3 (la 1 es la instancia principal)")
    state = C.STATE.parent / f"{C.STATE.name}-{n}"
    port = cfg.server.port + n - 1
    out = str(cfg.path(cfg.clip.output_dir))
    cmd = launcher or shutil.which("clipper") or f"{sys.executable} -m clipper"
    unit = f"""[Unit]
Description=clipper transmisión {n}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={C.STATE}
Environment=CLIPPER_STATE_DIR={state}
Environment=CLIPPER__SERVER__PORT={port}
Environment=CLIPPER__SERVER__HOST={cfg.server.host}
Environment=CLIPPER__CLIP__OUTPUT_DIR={out}
ExecStart={cmd} serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    return {"n": n, "state": state, "port": port, "unit": unit, "unit_path": Path(f"/etc/systemd/system/clipper-{n}.service")}


def prepare_state(cfg: Config, p: dict) -> None:
    """Carpeta de estado propia: copia config, claves (IA, contraseña) y ajustes; la fuente queda vacía para elegir otro canal."""
    st: Path = p["state"]
    st.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", "secrets.local.yaml", "settings.local.yaml"):
        src = C.STATE / name
        if not src.exists():
            continue
        dst = st / name
        if name == "settings.local.yaml":
            d = yaml.safe_load(src.read_text()) or {}
            d.pop("source", None)
            dst.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False))
        else:
            shutil.copy2(src, dst)
        dst.chmod(0o600)


def install(cfg: Config, n: int) -> str:
    p = plan(cfg, n)
    prepare_state(cfg, p)
    p["unit_path"].write_text(p["unit"])
    for cmd in (["systemctl", "daemon-reload"], ["systemctl", "enable", f"clipper-{n}.service"], ["systemctl", "restart", f"clipper-{n}.service"]):
        subprocess.run(cmd, check=True, capture_output=True)
    return f"Transmisión {n} activa en el puerto {p['port']} (http://IP-DEL-SERVIDOR:{p['port']}). Abre ese panel y pon allí el enlace del otro canal."
