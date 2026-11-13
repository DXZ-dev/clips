"""Panel de control local (FastAPI). Sin autenticación: escucha solo en 127.0.0.1 por defecto."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import os
from urllib.parse import urlparse

import anyio

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from pydantic import BaseModel

from .. import ai
from .. import auth
from .. import editor as ed
from ..engine import Engine
from ..spec import EditSpec
from ..subtitles import PRESETS
from ..publish.base import AuthError, OAuthPublisher, OAuthStates

STATIC = Path(__file__).parent / "static"


class ClipRequest(BaseModel):
    seconds: int | None = None


class EnqueueRequest(BaseModel):
    clip: str
    platforms: list[str]


class EditRequest(BaseModel):
    title: str | None = None
    description: str | None = None


class RenderRequest(BaseModel):
    spec: EditSpec
    as_new: bool = False


class SuggestRequest(BaseModel):
    clip: str | None = None
    transcript: str | None = None


class TestRequest(BaseModel):
    target: str = "llm"      # llm | transcribe


class ImportRequest(BaseModel):
    url: str
    mode: str = "highlights"
    max_clips: int = 0
    clip_seconds: int = 0
    transcribe: bool = True
    platforms: list[str] = []
    title: str = ""
    description: str = ""


class NamesRequest(BaseModel):
    names: list[str]


class DeleteAllRequest(BaseModel):
    confirm: bool = False


class LoginRequest(BaseModel):
    password: str


class PasswordRequest(BaseModel):
    current: str = ""
    new: str


class ProbeRequest(BaseModel):
    url: str


class DeviceRequest(BaseModel):
    device: str


def create_app(engine: Engine, manage_engine: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if manage_engine:
            engine.start()
        yield
        if manage_engine:
            engine.stop()

    app = FastAPI(title="clipper", lifespan=lifespan)
    loopback = {"localhost", "127.0.0.1", "::1"}

    throttle = auth.Throttle()
    PUBLIC = ("/login", "/api/auth/login")

    def _pw_hash() -> str:
        return engine.cfg.server.password_hash

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Anti DNS-rebinding / CSRF: el panel puede cambiar ajustes, así que solo acepta peticiones
        cuyo Host sea local (o esté en server.allowed_hosts) y, si traen Origin, que sea el mismo Host."""
        host_h = request.headers.get("host", "")
        host = urlparse("//" + host_h).hostname or ""
        pw = _pw_hash()
        # Con contraseña activa se acepta cualquier Host (la cookie solo viaja al nombre real, así que un ataque de
        # DNS-rebinding llegaría sin sesión). Sin contraseña, solo local o server.allowed_hosts.
        if not pw and host not in loopback | set(engine.cfg.server.allowed_hosts):
            return JSONResponse({"detail": "host no permitido"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin and urlparse(origin).netloc != host_h:
            return JSONResponse({"detail": "origen no permitido"}, status_code=403)
        if pw and request.url.path not in PUBLIC and not auth.valid_token(request.cookies.get(auth.COOKIE), pw):
            if request.url.path.startswith(("/api/", "/clip", "/clips/", "/static/")):
                return JSONResponse({"detail": "No has iniciado sesión"}, status_code=401)
            return RedirectResponse("/login")
        return await call_next(request)

    clips_dir = lambda: engine.cfg.path(engine.cfg.clip.output_dir)  # noqa: E731

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ------------------------------------------------------------------ acceso con contraseña
    @app.get("/login")
    def login_page():
        if not _pw_hash():
            return RedirectResponse("/")
        return HTMLResponse(auth.LOGIN_HTML)

    def _client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    @app.post("/api/auth/login")
    def login(req: LoginRequest, request: Request):
        pw, ip = _pw_hash(), _client_ip(request)
        if not pw:
            raise HTTPException(409, "El panel no tiene contraseña configurada.")
        if throttle.blocked(ip):
            raise HTTPException(429, "Demasiados intentos. Espera unos minutos.")
        if not auth.verify_password(req.password, pw):
            throttle.fail(ip)
            raise HTTPException(401, "Contraseña incorrecta.")
        throttle.ok(ip)
        r = JSONResponse({"ok": True})
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        r.set_cookie(auth.COOKIE, auth.make_token(pw), max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure)
        return r

    @app.post("/api/auth/logout")
    def logout():
        r = JSONResponse({"ok": True})
        r.delete_cookie(auth.COOKIE)
        return r

    @app.post("/api/auth/password")
    def change_password(req: PasswordRequest, request: Request):
        """Pone o cambia la contraseña. Si ya hay una, exige la actual. Sin contraseña previa solo se permite desde esta misma máquina."""
        pw = _pw_hash()
        if pw:
            if not auth.verify_password(req.current, pw):
                raise HTTPException(401, "La contraseña actual no es correcta.")
        elif _client_ip(request) not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "La primera contraseña se pone desde el propio servidor (clipper set-password).")
        if len(req.new) < auth.MIN_LEN:
            raise HTTPException(422, f"La contraseña debe tener al menos {auth.MIN_LEN} caracteres.")
        from ..settings import apply_update
        apply_update(engine.cfg, {"server": {"password_hash": auth.hash_password(req.new)}})
        r = JSONResponse({"ok": True})
        r.set_cookie(auth.COOKIE, auth.make_token(_pw_hash()), max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax")
        return r

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        return engine.status()

    @app.post("/clip")
    def clip(req: ClipRequest | None = None):
        """Guarda los últimos N segundos del buffer (15/30/60 desde el panel, o cualquiera 5-120)."""
        seconds = (req.seconds if req else None) or engine.cfg.clip.default_seconds
        if not 5 <= seconds <= 120:
            raise HTTPException(422, "seconds debe estar entre 5 y 120")
        try:
            job = engine.manual_clip(seconds)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"job": job.id, "status": job.status, "seconds": seconds}

    @app.get("/api/jobs")
    def jobs():
        return [{k: v for k, v in vars(j).items()} for j in sorted(
            engine.jobs.values(), key=lambda j: j.created, reverse=True)[:30]]

    @app.get("/api/clips")
    def clips():
        return engine.list_clips()

    @app.post("/api/device")
    def device(req: DeviceRequest):
        try:
            return engine.set_device(req.device)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/settings")
    def get_settings():
        from ..settings import public_view
        return public_view(engine.cfg)

    @app.put("/api/settings")
    def put_settings(patch: dict):
        """Actualización parcial. La clave de API solo se acepta aquí; nunca se devuelve."""
        try:
            return engine.apply_settings(patch)
        except ValidationError as e:
            msgs = [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]
            raise HTTPException(422, "; ".join(msgs))
        except ValueError as e:
            raise HTTPException(422, str(e))

    # ------------------------------------------------------------------ publicación
    pub = engine.publisher
    oauth_states = OAuthStates()

    @app.get("/api/publish")
    def publish_status():
        return pub.status()

    @app.post("/api/publish/enqueue")
    def publish_enqueue(req: EnqueueRequest):
        """Acción explícita del usuario: entra directamente en la cola (sin pasar por revisión)."""
        if not engine.cfg.publish.enabled:
            raise HTTPException(409, "La publicación está desactivada (Ajustes → Publicación)")
        try:
            path = pub.clip_path(req.clip)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not path.exists():
            raise HTTPException(404, "el clip no existe")
        ids = []
        for n in dict.fromkeys(req.platforms):
            if n not in pub.destinations():
                raise HTTPException(422, f"destino no activado: {n}")
            ids.append(pub.enqueue(req.clip, n, approved=True))
        return {"ids": ids}

    def _act(fn, pid: int, *a):
        try:
            fn(pid, *a)
        except KeyError:
            raise HTTPException(404, "no existe")
        except ValueError as e:
            raise HTTPException(409, str(e))
        return pub.get(pid)

    @app.post("/api/publish/{pid}/approve")
    def publish_approve(pid: int):
        return _act(pub.approve, pid)

    @app.post("/api/publish/{pid}/reject")
    def publish_reject(pid: int):
        return _act(pub.reject, pid)

    @app.put("/api/publish/{pid}")
    def publish_edit(pid: int, req: EditRequest):
        return _act(pub.edit, pid, req.title, req.description)

    @app.delete("/api/publish/{pid}")
    def publish_delete(pid: int):
        _act(pub.delete, pid)
        return {"deleted": pid}

    # ------------------------------------------------------------------ vídeos (subir / enlace -> clips destacados o publicar tal cual)
    vids = engine.videos

    def _video_opts(mode: str, max_clips: int, clip_seconds: int, transcribe: bool, platforms: list[str],
                    title: str, description: str) -> dict:
        if mode not in ("highlights", "asis"):
            raise HTTPException(422, "mode debe ser highlights o asis")
        if not 0 <= max_clips <= 50 or not 0 <= clip_seconds <= 120:
            raise HTTPException(422, "max_clips (0-50) o clip_seconds (0-120) fuera de rango")
        if platforms:
            if not engine.cfg.publish.enabled:
                raise HTTPException(409, "La publicación está desactivada (Ajustes → Publicación).")
            bad = [p for p in platforms if p not in pub.destinations()]
            if bad:
                raise HTTPException(422, f"destino no activado: {', '.join(bad)}")
        return {"max_clips": max_clips, "clip_seconds": clip_seconds, "transcribe": transcribe,
                "platforms": platforms, "title": title[:100], "description": description[:2000]}

    @app.post("/api/videos/upload")
    async def video_upload(request: Request, filename: str, mode: str = "highlights", max_clips: int = 0,
                           clip_seconds: int = 0, transcribe: bool = True, platforms: str = "", title: str = "",
                           description: str = ""):
        """Subida en streaming (cuerpo crudo): no carga el archivo en memoria, admite vídeos de varios GB."""
        opts = _video_opts(mode, max_clips, clip_seconds, transcribe, [p for p in platforms.split(",") if p], title, description)
        limit = int(engine.cfg.vod.max_upload_gb * 1e9)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > limit:
            raise HTTPException(413, f"El archivo supera el máximo de {engine.cfg.vod.max_upload_gb:g} GB (vod.max_upload_gb).")
        try:
            job, path = vids.begin_upload(filename, mode, opts)
        except ValueError as e:
            raise HTTPException(422, str(e))
        size = 0
        try:
            with open(path, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, "El archivo supera el tamaño máximo permitido.")
                    await anyio.to_thread.run_sync(f.write, chunk)
            if size == 0:
                raise HTTPException(422, "El archivo está vacío.")
        except BaseException:
            path.unlink(missing_ok=True)
            job.status, job.error, job.message = "error", "Subida interrumpida", "Subida interrumpida"
            raise
        vids.finish_upload(job)
        return {"id": job.id}

    @app.post("/api/videos/import")
    def video_import(req: ImportRequest):
        opts = _video_opts(req.mode, req.max_clips, req.clip_seconds, req.transcribe, req.platforms, req.title, req.description)
        try:
            job = vids.submit_url(req.url.strip(), req.mode, opts)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"id": job.id}

    @app.get("/api/videos")
    def video_list():
        return vids.list()

    @app.post("/api/videos/{jid}/cancel")
    def video_cancel(jid: str):
        if jid not in vids.jobs:
            raise HTTPException(404, "no existe")
        vids.cancel(jid)
        return {"ok": True}

    @app.delete("/api/videos/{jid}")
    def video_remove(jid: str):
        try:
            vids.remove(jid)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    # ------------------------------------------------------------------ fuente de directo y previa en vivo
    @app.post("/api/source/probe")
    def source_probe(req: ProbeRequest):
        """Comprueba un enlace de Twitch / Kick / YouTube: plataforma, si está en directo y su título."""
        from .. import links
        return links.probe_url(engine.cfg, req.url.strip())

    @app.post("/api/live/preview")
    def live_preview(seconds: float = 8.0):
        try:
            p = engine.live_preview(min(max(seconds, 4.0), 20.0))
        except Engine.Busy as e:
            raise HTTPException(429, str(e))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"url": f"/api/live/preview.mp4?v={int(p.stat().st_mtime * 1000)}"}

    # --- ventana en vivo: el buffer circular (segmentos .ts de 5 s) servido como HLS de ventana deslizante
    import math
    import re as _re
    seg_name = _re.compile(r"^seg_(\d{8})\.ts$")

    @app.get("/api/live/stream.m3u8")
    def live_playlist():
        segs = engine.reader.segments()[-5:]
        if not segs:
            raise HTTPException(404, "Todavía no llega señal del directo.")
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{max(1, math.ceil(max(s.dur for s in segs)))}"]
        first = int(seg_name.match(segs[0].path.name).group(1)) if seg_name.match(segs[0].path.name) else 0
        lines.append(f"#EXT-X-MEDIA-SEQUENCE:{first}")
        prev = None
        for s in segs:
            m = seg_name.match(s.path.name)
            idx = int(m.group(1)) if m else None
            if prev is not None and idx is not None and idx != prev + 1:
                lines.append("#EXT-X-DISCONTINUITY")               # hueco (p. ej. tras una reconexión)
            lines += [f"#EXTINF:{max(s.dur, 0.1):.3f},", f"/api/live/seg/{s.path.name}"]
            prev = idx
        return Response("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})

    @app.get("/api/live/seg/{name}")
    def live_segment(name: str):
        if not seg_name.match(name):
            raise HTTPException(400, "nombre inválido")
        p = engine.reader.dir / name
        if not p.exists():
            raise HTTPException(404, "el segmento ya salió del buffer")
        return FileResponse(p, media_type="video/mp2t", headers={"Cache-Control": "max-age=60"})

    @app.post("/api/auto/pause")
    def auto_pause():
        engine.pause()
        return {"paused": True}

    @app.post("/api/auto/resume")
    def auto_resume():
        engine.resume()
        return {"paused": False}

    def _live_jpeg(w: int) -> bytes:
        import subprocess
        from ..ffutil import require
        segs = engine.reader.segments()
        if not segs:
            raise HTTPException(409, "Todavía no llega señal del directo.")
        sg = segs[-1]
        r = subprocess.run([require("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{max(0.0, sg.dur - 1.0):.2f}", "-i", str(sg.path),
                            "-frames:v", "1", "-vf", f"scale={min(max(w, 320), 1920)}:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                           capture_output=True)
        if not r.stdout:
            raise HTTPException(409, "No se pudo extraer una imagen del directo.")
        return r.stdout

    @app.get("/api/live/frame")
    def live_frame(w: int = 960):
        """Imagen (JPEG) del directo ahora mismo."""
        return Response(_live_jpeg(w), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/autoconfig")
    def autoconfig():
        """Deja todo configurado para clips 9:16 profesionales (cámara, diseño, subtítulos, detectores, modelo de voz, modo automático)."""
        from .. import autoconfig as ac
        return ac.run(engine, lambda: _live_jpeg(960))

    @app.post("/api/live/facecam/ai")
    def live_facecam_ai():
        """Ubica la cámara del streamer y los rostros con el modelo de visión de tu API (Gemini, Claude, GPT-4o…)."""
        img = _live_jpeg(960)
        res = _ai(ai.locate_faces, engine.cfg.ai.llm, img)
        return {**res, "model": _ai(ai.resolve_model, engine.cfg.ai.llm)}

    @app.post("/api/live/facecam/detect")
    def live_facecam_detect():
        """Busca la cámara del streamer en los últimos segundos del directo."""
        from ..ffutil import probe
        from ..tracking import detect_facecam
        b = engine.reader.bounds()
        if not b or b[1] - b[0] < 4:
            raise HTTPException(409, "Aún hay muy poco directo en el buffer: espera unos segundos.")
        if not engine.render_lock.acquire(blocking=False):
            raise HTTPException(429, "Hay un clip renderizándose ahora mismo; inténtalo en unos segundos.")
        try:
            ci = engine.reader.prepare_range(max(b[0], b[1] - 10), b[1])
            try:
                info = probe(str(ci.workdir / "list.txt"), ["-f", "concat", "-safe", "0"])
                rect = detect_facecam(ci.args, info, engine.cfg)
            finally:
                ci.cleanup()
        finally:
            engine.render_lock.release()
        return {"facecam": rect.model_dump() if rect else None}

    _fd: dict = {}

    @app.post("/api/live/facecam/check")
    def live_facecam_check():
        """¿Se ve un rostro dentro del recuadro de la cámara ahora mismo? (el panel lo consulta cada ~4 s)"""
        import cv2
        import numpy as np
        from ..tracking import NullDetector, make_detector
        r = engine.cfg.layout.facecam
        if r is None:
            return {"face": None}
        if "d" not in _fd:
            _fd["d"] = make_detector(engine.cfg)
        d = _fd["d"]
        if isinstance(d, NullDetector):
            return {"face": None}
        img = cv2.imdecode(np.frombuffer(_live_jpeg(640), np.uint8), cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        x, y, cw, ch = int(r.x * w), int(r.y * h), max(8, int(r.w * w)), max(8, int(r.h * h))
        crop = img[y:y + ch, x:x + cw]
        if crop.size == 0:
            return {"face": False}
        crop = cv2.resize(crop, (320, max(2, int(320 * crop.shape[0] / crop.shape[1]))))
        return {"face": bool(d.detect(crop))}

    @app.get("/api/live/preview.mp4")
    def live_preview_file():
        p = ed.src_dir(engine.cfg) / "live_preview.mp4"
        if not p.exists():
            raise HTTPException(404, "sin previa")
        return FileResponse(p, media_type="video/mp4", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ actualizaciones desde GitHub
    from .. import updater

    @app.get("/api/update")
    def update_state():
        return updater.state()

    @app.post("/api/update/check")
    def update_check():
        """Consulta GitHub ahora mismo (tarda unos segundos)."""
        updater.check()
        return updater.state()

    @app.post("/api/update/apply")
    def update_apply():
        """Lanza la actualización (git pull + instalador) como una unidad aparte; el panel se reinicia al terminar."""
        try:
            updater.start_update(engine.cfg)
        except updater.UpdateError as e:
            raise HTTPException(409, str(e))
        engine.event("system", "actualización iniciada desde el panel")
        return {"started": True}

    # ------------------------------------------------------------------ IA (claves pegadas en Ajustes)
    def _ai(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except ai.AiError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/ai/suggest")
    def ai_suggest(req: SuggestRequest):
        """Gancho, título, descripción y hashtags para un clip (a partir de su transcripción)."""
        meta, transcript = {}, (req.transcript or "")
        if req.clip:
            try:
                j = ed.out_dir(engine.cfg) / f"{ed.stem_of(req.clip)}.json"
            except ed.EditorError as e:
                raise HTTPException(400, str(e))
            if j.exists():
                import json as _json
                meta = _json.loads(j.read_text())
            transcript = transcript or meta.get("transcript", "")
        return _ai(ai.suggest, engine.cfg.ai.llm, transcript, meta.get("reason", ""), meta.get("detail", ""))

    @app.get("/api/ai/models")
    def ai_models():
        """Modelos de texto disponibles con la clave configurada y cuál se usará (el elegido a mano o el mejor automático)."""
        cfg_llm = engine.cfg.ai.llm
        ids = _ai(ai.list_models, cfg_llm)
        return {"models": sorted(ids), "selected": cfg_llm.model or ai.pick_model(cfg_llm.provider, ids), "auto": not cfg_llm.model}

    @app.post("/api/ai/test")
    def ai_test(req: TestRequest):
        """Comprueba la conexión con la IA configurada (envía una petición mínima)."""
        if req.target == "llm":
            reply = _ai(ai.ping, engine.cfg.ai.llm)
            return {"ok": True, "reply": reply, "model": _ai(ai.resolve_model, engine.cfg.ai.llm)}
        if req.target == "transcribe":
            import numpy as np
            from ..detect.api_transcriber import transcribe_api
            tc = engine.cfg.transcribe
            if tc.provider != "api":
                raise HTTPException(409, "La transcripción está en modo local; elige «API» y guarda para probarla.")
            try:
                transcribe_api(tc.api, np.zeros(16000, np.float32), 0.0, tc.language, timeout=30)
            except RuntimeError as e:
                raise HTTPException(409, str(e))
            except Exception as e:  # noqa: BLE001
                raise HTTPException(502, f"No se pudo conectar: {type(e).__name__}")
            return {"ok": True, "reply": "conexión correcta"}
        raise HTTPException(422, "target debe ser llm o transcribe")

    @app.post("/api/publish/{pid}/ai")
    def publish_ai(pid: int):
        try:
            return pub.regenerate(pid)
        except ai.AiError as e:
            raise HTTPException(409, str(e))
        except KeyError:
            raise HTTPException(404, "no existe")
        except ValueError as e:
            raise HTTPException(409, str(e))

    def _oauth(provider: str) -> OAuthPublisher:
        p = pub.providers.get(provider)
        if not isinstance(p, OAuthPublisher):
            raise HTTPException(404, "proveedor desconocido")
        return p

    @app.get("/oauth/{provider}/start")
    def oauth_start(provider: str):
        p = _oauth(provider)
        try:
            return RedirectResponse(p.authorize_url(oauth_states.new(provider)))
        except AuthError as e:
            raise HTTPException(409, str(e))

    @app.get("/oauth/{provider}/callback")
    def oauth_callback(provider: str, state: str = "", code: str = "", error: str = ""):
        p = _oauth(provider)
        if not oauth_states.consume(state, provider):
            return HTMLResponse("<h3>Estado OAuth inválido o caducado. Vuelve a pulsar «Conectar».</h3>", 400)
        if error or not code:
            return HTMLResponse(f"<h3>{p.label}: autorización cancelada ({error or 'sin código'}).</h3>", 400)
        try:
            p.exchange_code(code)
        except Exception as e:  # noqa: BLE001
            return HTMLResponse(f"<h3>No se pudo conectar {p.label}: {str(e)[:300]}</h3>", 502)
        engine.event("publish", f"{p.label} conectado")
        return RedirectResponse("/")

    @app.post("/api/publish/{provider}/disconnect")
    def oauth_disconnect(provider: str):
        p = _oauth(provider)
        pub.tokens.delete(p.name)
        return {"disconnected": provider}

    def _safe(name: str) -> Path:
        if Path(name).name != name or not name.endswith(".mp4"):
            raise HTTPException(400, "nombre inválido")
        p = clips_dir() / name
        if not p.exists():
            raise HTTPException(404, "no existe")
        return p

    @app.get("/clips/{name}")
    def get_clip(name: str, download: bool = False):
        p = _safe(name)
        return FileResponse(p, media_type="video/mp4", filename=name if download else None)

    @app.get("/api/clips/download.zip")
    def clips_zip(names: str = "", all: bool = False):
        """ZIP con los clips elegidos (o todos): cada vídeo + un .txt con su título, transcripción y canal, listo para publicar."""
        import json as _json
        import tempfile
        import time as _time
        import zipfile
        from starlette.background import BackgroundTask
        out = ed.out_dir(engine.cfg)
        files = sorted(out.glob("*.mp4")) if all else []
        if not all:
            for n in names.split(","):
                n = n.strip()
                try:
                    ed.stem_of(n)
                except ed.EditorError:
                    continue
                if (out / n).is_file():
                    files.append(out / n)
        if not files:
            raise HTTPException(404, "No hay clips que descargar.")
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp.close()
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as z:           # los mp4 ya van comprimidos: sin recomprimir
            for p in files:
                z.write(p, p.name)
                jm = p.with_suffix(".json")
                if jm.exists():
                    try:
                        m = _json.loads(jm.read_text())
                        txt = (f"{m.get('title', '')}\n\n{m.get('transcript', '')}\n\nCanal: {m.get('platform', '')} {m.get('channel', '')}\n"
                               f"Motivo: {m.get('reason', '')} {m.get('detail', '')}\n")
                        z.writestr(p.stem + ".txt", txt)
                    except ValueError:
                        pass
        return FileResponse(tmp.name, media_type="application/zip", filename=f"clips_{_time.strftime('%Y%m%d_%H%M%S')}.zip",
                            background=BackgroundTask(os.unlink, tmp.name))

    @app.get("/api/storage")
    def storage():
        return {"used": ed.storage_used(engine.cfg), "limit": int(engine.cfg.clip.max_storage_gb * 1024 ** 3)}

    @app.get("/api/streams")
    def streams_list():
        from .. import streams
        return streams.load()

    @app.delete("/api/streams")
    def streams_forget(url: str):
        from .. import streams
        streams.forget(url)
        return {"ok": True}

    @app.post("/api/clips/delete")
    def clips_delete(req: NamesRequest):
        """Borra los clips elegidos (vídeo, metadatos, fuente si nadie más la usa) y sus entradas pendientes en la cola de publicación."""
        out, n = ed.out_dir(engine.cfg), 0
        for name in req.names[:1000]:
            try:
                stem = ed.stem_of(name)
            except ed.EditorError:
                continue
            if (out / name).exists():
                ed.remove_clip_files(engine.cfg, stem)
                pub.drop_clip(name)
                n += 1
        return {"deleted": n}

    @app.post("/api/clips/delete_all")
    def clips_delete_all(req: DeleteAllRequest):
        """Borra TODOS los clips y las fuentes guardadas. Hay que enviar confirm=true."""
        if not req.confirm:
            raise HTTPException(422, "Falta la confirmación.")
        out, n = ed.out_dir(engine.cfg), 0
        for p in sorted(out.glob("*.mp4")):
            ed.remove_clip_files(engine.cfg, p.stem)
            pub.drop_clip(p.name)
            n += 1
        for f in ed.src_dir(engine.cfg).glob("*"):                              # restos: fuentes compartidas, previas, ediciones
            if f.is_file():
                f.unlink(missing_ok=True)
        return {"deleted": n}

    @app.delete("/api/clips/{name}")
    def delete_clip(name: str):
        p = _safe(name)
        pub.drop_clip(name)
        ed.remove_clip_files(engine.cfg, p.stem)
        return JSONResponse({"deleted": name})

    # ------------------------------------------------------------------ editor
    def _editor(fn, *a):
        try:
            return fn(*a)
        except ed.EditorError as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(500, str(e))

    @app.get("/api/presets")
    def presets():
        from ..config import SubtitlesCfg
        from ..subtitles import with_preset
        out = {}
        for k in PRESETS:
            c = with_preset(SubtitlesCfg(), k)           # el estilo completo del preset sobre los valores de fábrica
            out[k] = {"primary": c.primary_color, "highlight": c.highlight_color, "size": c.font_size,
                      "style": {f: getattr(c, f) for f in ("font_size", "uppercase", "primary_color", "highlight_color", "outline_color",
                                                             "outline", "max_words_per_line", "bold", "shadow")}}
        return out

    _fonts: list[str] = []

    @app.get("/api/fonts")
    def fonts():
        """Familias de letra instaladas en el servidor (las que puede usar libass al quemar los subtítulos)."""
        if not _fonts:
            import shutil as _sh
            import subprocess as _sp
            names = set()
            if _sh.which("fc-list"):
                out = _sp.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
                for line in out.splitlines():
                    names.add(line.split(",")[0].strip())
            _fonts.extend(sorted(n for n in names if n) or ["DejaVu Sans"])
        return _fonts

    @app.get("/api/clips/{name}/edit")
    def clip_edit(name: str):
        _safe(name)
        data = _editor(ed.load_edit, engine.cfg, name)
        if data is None:
            raise HTTPException(404, "este clip no tiene fuente guardada: no se puede editar")
        return data

    @app.get("/api/clips/{name}/source")
    def clip_source(name: str):
        _safe(name)
        p = ed.source_path(engine.cfg, ed.stem_of(name))
        if not p.exists():
            raise HTTPException(404, "sin fuente")
        return FileResponse(p, media_type="video/mp4")

    @app.get("/api/clips/{name}/frame")
    def clip_frame(name: str, t: float = 0.0, w: int = 960):
        _safe(name)
        return Response(_editor(ed.frame_jpeg, engine.cfg, name, t, min(max(w, 160), 1920)), media_type="image/jpeg")

    @app.post("/api/clips/{name}/facecam")
    def clip_facecam(name: str, spec: EditSpec):
        _safe(name)
        with engine.render_lock:
            rect = _editor(ed.suggest_facecam, engine.cfg, name, spec)
        return {"facecam": rect.model_dump() if rect else None}

    @app.post("/api/clips/{name}/preview")
    def clip_preview(name: str, spec: EditSpec):
        _safe(name)
        with engine.render_lock:
            p = _editor(ed.preview, engine.cfg, name, spec)
        return {"url": f"/api/clips/{name}/preview.mp4?v={int(p.stat().st_mtime * 1000)}"}

    @app.get("/api/clips/{name}/preview.mp4")
    def clip_preview_file(name: str):
        _safe(name)
        p = ed.src_dir(engine.cfg) / f"{ed.stem_of(name)}.preview.mp4"
        if not p.exists():
            raise HTTPException(404, "sin previsualización")
        return FileResponse(p, media_type="video/mp4")

    @app.post("/api/clips/{name}/render")
    def clip_render(name: str, req: RenderRequest):
        _safe(name)
        with engine.render_lock:
            new = _editor(ed.apply, engine.cfg, name, req.spec, req.as_new)
        engine.event("clip", f"editado: {new}")
        return {"name": new}

    return app
