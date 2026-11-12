"""Edición de la configuración desde el panel: validación, aplicación en caliente y persistencia local."""
from __future__ import annotations

import copy
import os

import yaml
from pydantic import BaseModel

from . import config as C
from .config import SECRET_PATHS, Config, deep_merge, get_path, set_path

# Rutas que el panel NO puede cambiar (se borran/escriben ficheros en ellas).
PROTECTED = [("buffer", "dir"), ("clip", "output_dir"), ("reframe", "mediapipe_model")]


def public_view(cfg: Config) -> dict:
    """Configuración sin secretos; cada secreto aparece solo como `<nombre>_set: true/false`."""
    d = cfg.model_dump()
    for sp in SECRET_PATHS:
        *head, leaf = sp.split(".")
        node = d
        for part in head:
            node = node[part]
        node[leaf + "_set"] = bool(get_path(cfg, sp))
    return d


def _assign(dst: BaseModel, src: BaseModel) -> None:
    """Copia src sobre dst EN SITIO: los detectores guardan referencias a los sub-objetos de la config."""
    for name in type(src).model_fields:
        v = getattr(src, name)
        if isinstance(v, BaseModel):
            _assign(getattr(dst, name), v)
        else:
            setattr(dst, name, v)


def _write_private(path, data, dump) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        dump(data, f)
    os.replace(tmp, path)


def _yaml_dump(data, f):
    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def _extract_secrets(patch: dict) -> dict[str, str]:
    """Saca del patch los secretos: valor no vacío = poner; `clear_<campo>: true` = borrar; vacío = conservar."""
    found: dict[str, str] = {}
    for sp in SECRET_PATHS:
        *head, leaf = sp.split(".")
        node = patch
        for part in head:
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        if not isinstance(node, dict):
            continue
        val, clear = node.pop(leaf, None), node.pop(f"clear_{leaf}", False)
        node.pop(f"{leaf}_set", None)
        if clear:
            found[sp] = ""
        elif val:
            found[sp] = str(val)
    return found


def apply_update(cfg: Config, patch: dict, persist: bool = True) -> list[str]:
    """Valida y aplica `patch` (dict parcial). Devuelve las secciones de primer nivel que cambiaron.
    Lanza ValueError / pydantic.ValidationError si algo no es válido (no se aplica nada)."""
    patch = copy.deepcopy(patch)
    secrets = _extract_secrets(patch)

    before = cfg.model_dump()
    new = Config.model_validate(deep_merge(before, patch))
    for sec, key in PROTECTED:
        if getattr(getattr(new, sec), key) != getattr(getattr(cfg, sec), key):
            raise ValueError(f"{sec}.{key} no se puede cambiar desde la interfaz (edita config.yaml)")

    changed = {k for k in before if before[k] != new.model_dump()[k]}
    for sp in SECRET_PATHS:
        live = get_path(cfg, sp)
        set_path(new, sp, secrets.get(sp, live))
        if sp in secrets and secrets[sp] != live:
            changed.add(sp.split(".")[0])
    _assign(cfg, new)

    if persist:
        existing = (yaml.safe_load(C.SETTINGS_FILE.read_text()) if C.SETTINGS_FILE.exists() else None) or {}
        _write_private(C.SETTINGS_FILE, deep_merge(existing, patch), _yaml_dump)
        if secrets:
            sec = (yaml.safe_load(C.SECRETS_FILE.read_text()) if C.SECRETS_FILE.exists() else None) or {}
            for sp, v in secrets.items():
                sec.pop(sp, None) if not v else sec.__setitem__(sp, v)
            _write_private(C.SECRETS_FILE, sec, _yaml_dump)
    return sorted(changed)
