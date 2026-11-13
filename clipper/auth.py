"""Acceso al panel con contraseña: hash scrypt, cookie de sesión firmada (HMAC) y freno a la fuerza bruta."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

COOKIE = "clipper_session"
SESSION_DAYS = 30
MIN_LEN = 8


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


def make_token(stored_hash: str, days: int = SESSION_DAYS) -> str:
    """Token = <caducidad>.<aleatorio>.<firma>. La clave deriva del hash: cambiar la contraseña invalida todas las sesiones."""
    exp = int(time.time()) + days * 86400
    nonce = secrets.token_hex(8)
    msg = f"{exp}.{nonce}"
    sig = hmac.new(stored_hash.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def valid_token(token: str | None, stored_hash: str) -> bool:
    if not token or not stored_hash:
        return False
    try:
        exp, nonce, sig = token.split(".")
        calc = hmac.new(stored_hash.encode(), f"{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(calc, sig) and int(exp) > time.time()
    except (ValueError, TypeError):
        return False


class Throttle:
    """Máximo `limit` intentos fallidos por IP en `window` s; después, bloqueo hasta que pase la ventana."""

    def __init__(self, limit: int = 5, window: int = 300):
        self.limit, self.window, self._f = limit, window, {}

    def blocked(self, ip: str) -> bool:
        now = time.time()
        self._f[ip] = [t for t in self._f.get(ip, []) if now - t < self.window]
        return len(self._f[ip]) >= self.limit

    def fail(self, ip: str) -> None:
        self._f.setdefault(ip, []).append(time.time())

    def ok(self, ip: str) -> None:
        self._f.pop(ip, None)


LOGIN_HTML = """<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>clipper · acceso</title><style>
:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0d12;color:#e8eaf0;font:15px system-ui,sans-serif}
form{width:min(340px,90vw);background:#141821;border:1px solid #232a38;border-radius:16px;padding:26px;box-shadow:0 8px 30px #0006}
h1{margin:0 0 4px;font-size:20px}p{margin:0 0 16px;color:#8b92a3;font-size:13px}
input{width:100%;box-sizing:border-box;padding:11px;border-radius:8px;border:1px solid #232a38;background:#10131a;color:inherit;font:inherit}
button{width:100%;margin-top:12px;padding:11px;border:0;border-radius:8px;background:#ffe600;color:#111;font-weight:700;font:inherit;font-weight:700;cursor:pointer}
#e{color:#ff6b6b;font-size:13px;min-height:18px;margin-top:10px}</style></head><body>
<form id="f"><h1>✂️ clipper</h1><p>Introduce la contraseña para entrar.</p>
<input id="p" type="password" autocomplete="current-password" placeholder="Contraseña" autofocus required>
<button>Entrar</button><div id="e"></div></form>
<script>f.onsubmit=async e=>{e.preventDefault();e_.textContent="";const r=await fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:p.value})});
if(r.ok){location.href="/"}else{let m="Error";try{m=(await r.json()).detail}catch{}e_.textContent=m}};const e_=document.getElementById("e");</script></body></html>"""
