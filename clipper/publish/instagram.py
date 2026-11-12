"""Instagram Reels vía Graph API (cuenta profesional + token largo de Meta). Subida reanudable: no necesita URL pública."""
from __future__ import annotations

import time
from pathlib import Path

from .base import AuthError, PublishError, PublishResult, Publisher, RetryableError, check, iter_file


class Instagram(Publisher):
    name, label = "instagram", "Instagram Reels"

    def configured(self) -> bool:
        return bool(self.dest.ig_user_id and self.dest.access_token)

    def title_limit(self):
        return 2200

    def publish(self, path: Path, title: str, description: str, meta: dict) -> PublishResult:
        if not self.configured():
            raise AuthError("Faltan el ID de usuario de Instagram y el token (Ajustes → Publicación)")
        g = f"https://graph.facebook.com/{self.dest.api_version}"
        tok = self.dest.access_token
        caption = (title if title == description or not description else f"{title}\n\n{description}")[:2200]
        size = path.stat().st_size
        c = check(self.http.post(f"{g}/{self.dest.ig_user_id}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption, "share_to_feed": "true",
            "access_token": tok}), "Instagram crear contenedor").json()
        cid, uri = c["id"], c.get("uri")
        if not uri:
            raise RetryableError("Instagram no devolvió la URL de subida")
        check(self.http.post(uri, content=iter_file(path), headers={
            "Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)}), "Instagram subir vídeo")
        for _ in range(40):
            st = check(self.http.get(f"{g}/{cid}", params={"fields": "status_code,status", "access_token": tok}),
                       "Instagram estado").json()
            if st.get("status_code") == "FINISHED":
                break
            if st.get("status_code") in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram rechazó el vídeo: {st.get('status', st)}")
            time.sleep(3)
        else:
            raise RetryableError("Instagram sigue procesando el vídeo (se reintentará)")
        mid = check(self.http.post(f"{g}/{self.dest.ig_user_id}/media_publish", data={
            "creation_id": cid, "access_token": tok}), "Instagram publicar").json()["id"]
        try:
            link = self.http.get(f"{g}/{mid}", params={"fields": "permalink", "access_token": tok}).json().get("permalink", "")
        except Exception:  # noqa: BLE001
            link = ""
        return PublishResult(mid, link)
