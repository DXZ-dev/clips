"""Panel de control local (FastAPI). Sin autenticación: escucha solo en 127.0.0.1 por defecto."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from pydantic import BaseModel

from ..engine import Engine

STATIC = Path(__file__).parent / "static"


class ClipRequest(BaseModel):
    seconds: int | None = None


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
        p.unlink()
        p.with_suffix(".json").unlink(missing_ok=True)
        return JSONResponse({"deleted": name})

    return app
