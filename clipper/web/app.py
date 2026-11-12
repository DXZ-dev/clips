"""Panel de control local (FastAPI). Sin autenticación: escucha solo en 127.0.0.1 por defecto."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from pydantic import BaseModel

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

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Anti DNS-rebinding / CSRF: el panel puede cambiar ajustes, así que solo acepta peticiones
        cuyo Host sea local (o esté en server.allowed_hosts) y, si traen Origin, que sea el mismo Host."""
        host_h = request.headers.get("host", "")
        host = urlparse("//" + host_h).hostname or ""
        if host not in loopback | set(engine.cfg.server.allowed_hosts):
            return JSONResponse({"detail": "host no permitido"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin and urlparse(origin).netloc != host_h:
            return JSONResponse({"detail": "origen no permitido"}, status_code=403)
        return await call_next(request)

    clips_dir = lambda: engine.cfg.path(engine.cfg.clip.output_dir)  # noqa: E731

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

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

    @app.delete("/api/clips/{name}")
    def delete_clip(name: str):
        p = _safe(name)
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
        from ..subtitles import with_preset
        out = {}
        for k in PRESETS:
            c = with_preset(engine.cfg.subtitles, k)
            out[k] = {"primary": c.primary_color, "highlight": c.highlight_color, "size": c.font_size}
        return out

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
