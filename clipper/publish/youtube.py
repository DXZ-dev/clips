"""YouTube Shorts: YouTube Data API v3, subida reanudable (videos.insert). Requiere un proyecto de Google Cloud."""
from __future__ import annotations

from pathlib import Path

from .base import OAuthPublisher, PublishResult, RetryableError, check, iter_file

UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTube(OAuthPublisher):
    name, label = "youtube", "YouTube Shorts"
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scope = "https://www.googleapis.com/auth/youtube.upload"
    auth_extra = {"access_type": "offline", "prompt": "consent"}

    def title_limit(self):
        return 100

    def description_limit(self):
        return 5000

    def publish(self, path: Path, title: str, description: str, meta: dict) -> PublishResult:
        token = self.access_token()
        size = path.stat().st_size
        tags = [t.lstrip("#") for t in self.dest.tags]
        body = {"snippet": {"title": title, "description": description, "tags": tags,
                            "categoryId": self.dest.category_id},
                "status": {"privacyStatus": self.dest.privacy, "selfDeclaredMadeForKids": False}}
        init = check(self.http.post(
            UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"}, json=body,
            headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                     "X-Upload-Content-Length": str(size)}), "YouTube iniciar subida")
        loc = init.headers.get("location")
        if not loc:
            raise RetryableError("YouTube no devolvió la URL de subida")
        r = check(self.http.put(loc, content=iter_file(path), headers={
            "Authorization": f"Bearer {token}", "Content-Type": "video/mp4", "Content-Length": str(size)}),
            "YouTube subir vídeo")
        vid = r.json()["id"]
        return PublishResult(vid, f"https://www.youtube.com/shorts/{vid}")
