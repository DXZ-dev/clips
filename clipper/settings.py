"""Edición de la configuración desde el panel: validación, aplicación en caliente y persistencia local."""
from __future__ import annotations

import os

import yaml
from pydantic import BaseModel

from .config import SECRETS_FILE, SETTINGS_FILE, Config, deep_merge

# Rutas que el panel NO puede cambiar (se borran/escriben ficheros en ellas).
PROTECTED = [("buffer", "dir"), ("clip", "output_dir"), ("reframe", "mediapipe_model")]


def public_view(cfg: Config) -> dict:
    d = cfg.model_dump()
    d["transcribe"]["api"]["api_key_set"] = bool(cfg.transcribe.api.api_key)
    return d


def _assign(dst: BaseModel, src: BaseModel) -> None:
    """Copia src sobre dst EN SITIO: los detectores guardan referencias a los sub-objetos de la config."""
    for name in type(src).model_fields:
        v = getattr(src, name)
        if isinstance(v, BaseModel):
            _assign(getattr(dst, name), v)
        else:
            setattr(dst, name, v)


def _write_private(path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


def apply_update(cfg: Config, patch: dict, persist: bool = True) -> list[str]:
    """Valida y aplica `patch` (dict parcial). Devuelve las secciones de primer nivel que cambiaron.
    Lanza ValueError / pydantic.ValidationError si algo no es válido (no se aplica nada)."""
    patch = {k: dict(v) if isinstance(v, dict) else v for k, v in patch.items()}
    api = (patch.get("transcribe") or {}).get("api")
    new_key = None
    if isinstance(api, dict):
        api = dict(api)
        patch["transcribe"] = {**patch["transcribe"], "api": api}
        new_key = api.pop("api_key", None)
        api.pop("api_key_set", None)
        if api.pop("clear_api_key", False):
            new_key = ""

    before = cfg.model_dump()
    new = Config.model_validate(deep_merge(before, patch))
    for sec, key in PROTECTED:
        if getattr(getattr(new, sec), key) != getattr(getattr(cfg, sec), key):
            raise ValueError(f"{sec}.{key} no se puede cambiar desde la interfaz (edita config.yaml)")

    key = cfg.transcribe.api.api_key if new_key is None else new_key
    new.transcribe.api.api_key = key
    changed = [k for k in before if before[k] != new.model_dump()[k]]
    if new_key is not None and (new_key != cfg.transcribe.api.api_key):
        changed.append("transcribe")
    _assign(cfg, new)

    if persist:
        existing = (yaml.safe_load(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else None) or {}
        _write_private(SETTINGS_FILE, deep_merge(existing, patch))
        if new_key is not None:
            sec = (yaml.safe_load(SECRETS_FILE.read_text()) if SECRETS_FILE.exists() else None) or {}
            if new_key:
                sec["transcribe_api_key"] = new_key
            else:
                sec.pop("transcribe_api_key", None)
            _write_private(SECRETS_FILE, sec)
    return sorted(set(changed))
