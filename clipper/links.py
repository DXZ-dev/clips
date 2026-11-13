"""Enlaces de Twitch, Kick y YouTube: detección de plataforma, comprobación («¿está en directo?») y mensajes claros."""
from __future__ import annotations

import re
import subprocess
from urllib.parse import urlparse

from .config import STATE, Config
from .ffutil import find_tool

PLATFORMS = {"twitch": ("twitch.tv",), "kick": ("kick.com",), "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com")}


def platform_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for name, doms in PLATFORMS.items():
        if any(host == d or host.endswith("." + d) for d in doms):
            return name
    return "other"


def channel_of(url: str) -> dict:
    """{"platform": "twitch|kick|youtube|otro", "channel": "nombre"} a partir del enlace del canal o directo."""
    plat = platform_of(url)
    parts = [p for p in (urlparse(url).path or "").split("/") if p]
    ch = ""
    if plat in ("twitch", "kick") and parts:
        ch = parts[0] if parts[0].lower() not in ("videos", "video", "clip", "directory") else (parts[1] if len(parts) > 1 else "")
    elif plat == "youtube":
        for i, p in enumerate(parts):
            if p.startswith("@"):
                ch = p
            elif p in ("channel", "c", "user") and i + 1 < len(parts):
                ch = parts[i + 1]
    return {"platform": plat if plat != "other" else "otro", "channel": ch}


def valid_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s]+$", url)) and not url.startswith("-")


def cookies_args(cfg: Config) -> list[str]:
    """yt-dlp con cookies (para vídeos que YouTube sólo da a cuentas con sesión o tras comprobar que no eres un bot)."""
    c = getattr(cfg.vod, "cookies_file", "") or ""
    if not c:
        return []
    p = cfg.path(c)
    return ["--cookies", str(p)] if p.is_file() else []


def explain_ytdlp(text: str, platform: str = "other") -> str:
    """Traduce el error de yt-dlp/streamlink a algo accionable."""
    t = text.lower()
    if "sign in to confirm" in t or "not a bot" in t or "cookies" in t and platform == "youtube":
        return ("YouTube pide verificar que no eres un robot (suele pasar desde IPs de servidor). Soluciones: exporta las cookies de tu navegador a un "
                "archivo cookies.txt, guárdalo en la carpeta de datos y pon su ruta en Ajustes → Vídeos → Cookies; o descarga el vídeo y súbelo como archivo.")
    if "private" in t or "members-only" in t or "members only" in t:
        return "El vídeo es privado o solo para miembros."
    if "is not currently live" in t or "no playable streams" in t or "offline" in t or "no streams found" in t:
        return "El canal no está en directo ahora mismo."
    if "unsupported url" in t:
        return "Enlace no admitido. Usa un enlace de un vídeo o canal de Twitch, Kick o YouTube."
    if "unavailable" in t or "does not exist" in t or "404" in t:
        return "El vídeo o canal no existe o no está disponible."
    if "geo" in t and "restrict" in t:
        return "El vídeo está bloqueado en tu país/región."
    if "http error 403" in t or "forbidden" in t:
        return "La plataforma rechazó la petición (403). Actualiza yt-dlp (vuelve a ejecutar el instalador) e inténtalo de nuevo."
    return ("No se pudo leer el enlace. Si es YouTube, actualiza yt-dlp (vuelve a ejecutar el instalador): cambian a menudo. Detalle: " + text.strip()[-160:])


def probe_url(cfg: Config, url: str, timeout: int = 45) -> dict:
    """¿Es válido el enlace y está en directo? Devuelve {platform, ok, live, title, duration, message}."""
    plat = platform_of(url)
    out = {"platform": plat, "ok": False, "live": None, "title": "", "duration": None, "message": ""}
    if not valid_url(url):
        out["message"] = "Pega un enlace http(s) válido."
        return out
    yd = find_tool("yt-dlp")
    sl = find_tool("streamlink")
    if not yd and not sl:
        out["message"] = "Faltan herramientas: instala yt-dlp y streamlink (viene en requirements.txt)."
        return out
    err = ""
    if yd:
        try:
            r = subprocess.run([yd, "--no-playlist", "--skip-download", "--no-warnings", "--print",
                                "%(title)s\t%(duration)s\t%(is_live)s", *cookies_args(cfg), "--", url],
                               capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0 and r.stdout.strip():
                title, dur, live = (r.stdout.strip().splitlines()[-1].split("\t") + ["", "", ""])[:3]
                out.update(ok=True, title=title[:140], live=(live == "True"),
                           duration=float(dur) if dur not in ("", "NA", "None") else None)
                out["message"] = "En directo ahora" if out["live"] else "Vídeo disponible"
                return out
            err = r.stderr or r.stdout
        except subprocess.TimeoutExpired:
            err = "tiempo de espera agotado"
    if sl and plat in ("twitch", "kick", "youtube", "other"):
        try:
            r = subprocess.run([sl, "--json", url], capture_output=True, text=True, timeout=timeout)
            if r.returncode == 0 and '"streams"' in r.stdout and '"error"' not in r.stdout[:200]:
                out.update(ok=True, live=True, message="En directo ahora")
                return out
            err = err or (r.stdout + r.stderr)
        except subprocess.TimeoutExpired:
            err = err or "tiempo de espera agotado"
    out["message"] = explain_ytdlp(err, plat)
    # un canal que existe pero está apagado es un caso normal para un enlace de directo
    out["live"] = False if "no está en directo" in out["message"] else None
    return out
