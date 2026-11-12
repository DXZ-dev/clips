"""TikTok: Content Posting API (Direct Post, FILE_UPLOAD). Apps sin auditar: solo publican en privado."""
from __future__ import annotations

import time
from pathlib import Path

from .base import AuthError, OAuthPublisher, PublishError, PublishResult, RetryableError, check, iter_file

API = "https://open.tiktokapis.com"


class TikTok(OAuthPublisher):
    name, label = "tiktok", "TikTok"
    auth_url = "https://www.tiktok.com/v2/auth/authorize/"
    token_url = f"{API}/v2/oauth/token/"
    scope = "video.publish"
    client_id_field = "client_key"

    def title_limit(self):
        return 2200

    def publish(self, path: Path, title: str, description: str, meta: dict) -> PublishResult:
        token = self.access_token()
        size = path.stat().st_size
        if size > 64 * 1024 * 1024:
            raise PublishError("El clip supera 64 MB (límite de subida en un solo trozo)")
        auth = {"Authorization": f"Bearer {token}"}
        caption = (title + ("\n" + description if description and description != title else ""))[:2200]
        init = check(self.http.post(f"{API}/v2/post/publish/video/init/", headers=auth, json={
            "post_info": {"title": caption, "privacy_level": self.dest.privacy_level,
                          "disable_duet": False, "disable_comment": False, "disable_stitch": False},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size,
                            "total_chunk_count": 1}}), "TikTok iniciar")
        j = init.json()
        if j.get("error", {}).get("code", "ok") != "ok":
            raise PublishError(f"TikTok: {j['error']}")
        pid, up = j["data"]["publish_id"], j["data"]["upload_url"]
        check(self.http.put(up, content=iter_file(path), headers={
            "Content-Type": "video/mp4", "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}"}), "TikTok subir vídeo")
        for _ in range(40):                                 # sondeo del estado (hasta ~2 min)
            st = check(self.http.post(f"{API}/v2/post/publish/status/fetch/", headers=auth,
                                      json={"publish_id": pid}), "TikTok estado").json()["data"]
            if st["status"] == "PUBLISH_COMPLETE":
                ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
                return PublishResult(pid, f"https://www.tiktok.com/@/video/{ids[0]}" if ids else "")
            if st["status"] == "FAILED":
                raise PublishError(f"TikTok rechazó el vídeo: {st.get('fail_reason', 'sin motivo')}")
            time.sleep(3)
        raise RetryableError("TikTok sigue procesando el vídeo (se reintentará)")
