"""Base común: errores, resultado, almacén de tokens OAuth y flujo OAuth2 de código de autorización."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode

import httpx

from .. import config as C
from ..config import Config


class PublishError(Exception):
    """Fallo no recuperable (contenido rechazado, cuenta sin permiso...): no se reintenta."""


class AuthError(PublishError):
    """Falta conectar la cuenta o el token no sirve."""


class RetryableError(Exception):
    """Fallo transitorio (red, 5xx, límite de peticiones): se reintenta con espera."""


@dataclass
class PublishResult:
    id: str
    url: str = ""


def check(r: httpx.Response, what: str) -> httpx.Response:
    """Convierte códigos HTTP en el tipo de error correcto."""
    if r.status_code < 400:
        return r
    msg = f"{what}: HTTP {r.status_code} {r.text[:300]}"
    if r.status_code in (401, 403):
        raise AuthError(msg)
    if r.status_code in (408, 429) or r.status_code >= 500:
        raise RetryableError(msg)
    raise PublishError(msg)


def iter_file(path: Path, chunk: int = 1 << 20) -> Iterator[bytes]:
    with open(path, "rb") as f:
        while b := f.read(chunk):
            yield b


class TokenStore:
    """Tokens OAuth en tokens.local.json (0600). Nunca se devuelven por la API."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path or C.TOKENS_FILE

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def get(self, provider: str) -> dict:
        with self._lock:
            return self._read().get(provider, {})

    def put(self, provider: str, tok: dict) -> None:
        with self._lock:
            data = self._read()
            data[provider] = tok
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)

    def delete(self, provider: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(provider, None) is not None:
                tmp = self.path.with_suffix(".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, self.path)


class Publisher:
    """Interfaz de un destino. `publish` sube un mp4 y devuelve el resultado o lanza un error tipado."""
    name = ""
    label = ""
    needs_oauth = False

    def __init__(self, cfg: Config, tokens: TokenStore, http: httpx.Client | None = None):
        self.cfg, self.tokens = cfg, tokens
        self.http = http or httpx.Client(timeout=httpx.Timeout(60, read=300))

    @property
    def dest(self):                       # sub-configuración de este destino
        return getattr(self.cfg.publish, self.name)

    def configured(self) -> bool:         # tiene lo necesario para intentar conectar/publicar
        raise NotImplementedError

    def connected(self) -> bool:
        return self.configured()

    def title_limit(self) -> int:
        return 100

    def description_limit(self) -> int:
        return 2200

    def publish(self, path: Path, title: str, description: str, meta: dict) -> PublishResult:
        raise NotImplementedError


class OAuthPublisher(Publisher):
    """Destinos con OAuth2 (código de autorización con redirect a este mismo panel)."""
    needs_oauth = True
    auth_url = token_url = scope = ""
    client_id_field, client_secret_field = "client_id", "client_secret"
    auth_extra: dict = {}

    def client_id(self) -> str:
        return getattr(self.dest, self.client_id_field)

    def client_secret(self) -> str:
        return getattr(self.dest, self.client_secret_field)

    def configured(self) -> bool:
        return bool(self.client_id() and self.client_secret())

    def connected(self) -> bool:
        return self.configured() and bool(self.tokens.get(self.name).get("refresh_token"))

    def redirect_uri(self) -> str:
        return f"http://localhost:{self.cfg.server.port}/oauth/{self.name}/callback"

    def authorize_url(self, state: str) -> str:
        if not self.configured():
            raise AuthError("Falta el ID y el secreto de cliente (Ajustes → Publicación)")
        q = {self.client_id_field: self.client_id(), "redirect_uri": self.redirect_uri(), "response_type": "code",
             "scope": self.scope, "state": state, **self.auth_extra}
        return f"{self.auth_url}?{urlencode(q)}"

    def _token_request(self, data: dict) -> dict:
        r = check(self.http.post(self.token_url, data=data), f"{self.label} token")
        j = r.json()
        j = j.get("data", j)                      # algunos proveedores anidan en "data"
        if "access_token" not in j:
            raise AuthError(f"{self.label}: respuesta de token sin access_token: {str(j)[:200]}")
        return j

    def exchange_code(self, code: str) -> None:
        j = self._token_request({self.client_id_field: self.client_id(), self.client_secret_field: self.client_secret(),
                                 "code": code, "grant_type": "authorization_code",
                                 "redirect_uri": self.redirect_uri()})
        self.tokens.put(self.name, {"access_token": j["access_token"], "refresh_token": j.get("refresh_token", ""),
                                    "expires_at": time.time() + int(j.get("expires_in", 3600))})

    def access_token(self) -> str:
        tok = self.tokens.get(self.name)
        if not tok.get("refresh_token"):
            raise AuthError(f"{self.label} no está conectado (Ajustes → Publicación → Conectar)")
        if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60:
            return tok["access_token"]
        j = self._token_request({self.client_id_field: self.client_id(), self.client_secret_field: self.client_secret(),
                                 "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        self.tokens.put(self.name, {"access_token": j["access_token"],
                                    "refresh_token": j.get("refresh_token") or tok["refresh_token"],
                                    "expires_at": time.time() + int(j.get("expires_in", 3600))})
        return j["access_token"]


class OAuthStates:
    """`state` de un solo uso con caducidad (anti-CSRF en el callback)."""

    def __init__(self, ttl: int = 600):
        self.ttl, self._s, self._lock = ttl, {}, threading.Lock()

    def new(self, provider: str) -> str:
        st = secrets.token_urlsafe(24)
        with self._lock:
            self._s = {k: v for k, v in self._s.items() if v[1] > time.time()}
            self._s[st] = (provider, time.time() + self.ttl)
        return st

    def consume(self, state: str, provider: str) -> bool:
        with self._lock:
            v = self._s.pop(state, None)
        return bool(v and v[0] == provider and v[1] > time.time())
