"""Construcción de la entrada para ffmpeg según el tipo de fuente configurado."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from .config import Config
from pathlib import Path

from .ffutil import find_tool, require


@dataclass
class IngestPlan:
    ffmpeg_input: list[str]                 # argumentos de entrada de ffmpeg
    producer: list[str] | None = None       # proceso que escribe a stdout (streamlink/yt-dlp)
    producers: list[list[str]] = field(default_factory=list)   # alternativas: si una falla se prueba la siguiente
    restart: bool = True                    # reiniciar si cae (directos). Un archivo sin loop no.
    description: str = field(default="")


def build_plan(cfg: Config) -> IngestPlan:
    s = cfg.source
    if s.type == "file":
        f = str(cfg.path(s.file))
        args = ["-re"]
        if s.file_follow:
            args += ["-follow", "1"]
        elif s.file_loop:
            args += ["-stream_loop", "-1"]
        return IngestPlan(args + ["-i", f], restart=s.file_loop or s.file_follow,
                          description=f"archivo simulando directo: {f}")

    if not s.url:
        raise ValueError(f"source.url vacío para type={s.type}")

    if s.type == "rtmp":
        args = ["-listen", "1"] if s.rtmp_listen else []
        return IngestPlan(args + ["-i", s.url], description=f"RTMP {s.url}")

    if s.type == "hls":
        args = []
        if s.url.startswith("http"):
            args = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        return IngestPlan(args + ["-i", s.url], description=f"HLS {s.url}")

    if s.type == "live":
        from .links import cookies_args, platform_of
        sl, yd = find_tool("streamlink"), find_tool("yt-dlp")
        plat = platform_of(s.url)
        cands: list[list[str]] = []
        if sl:
            extra = ["--twitch-low-latency"] if plat == "twitch" else []
            cands.append([sl, "--stdout", "--quiet", "--retry-streams", "5", *extra, s.url, s.streamlink_quality])
        if yd:
            cands.append([yd, "-q", "--no-part", "--hls-use-mpegts", "-f", "best", *cookies_args(cfg), "-o", "-", "--", s.url])
        if not cands:
            raise ValueError("Para enlaces de directo hace falta streamlink o yt-dlp (vienen en requirements.txt).")
        return IngestPlan(["-i", "pipe:0"], producer=cands[0], producers=cands,
                          description=f"directo {plat}: {s.url} ({' → '.join(Path(c[0]).name for c in cands)})")

    if s.type == "streamlink":
        require("streamlink")
        return IngestPlan(["-i", "pipe:0"],
                          producer=[find_tool("streamlink"), "--stdout", "--quiet",
                                    "--retry-streams", "5", s.url, s.streamlink_quality],
                          description=f"streamlink {s.url} ({s.streamlink_quality})")

    if s.type == "ytdlp":
        require("yt-dlp")
        return IngestPlan(["-i", "pipe:0"],
                          producer=[find_tool("yt-dlp"), "-q", "--no-part", "--hls-use-mpegts",
                                    "-f", "best", "-o", "-", s.url],
                          description=f"yt-dlp {s.url}")
    raise ValueError(s.type)
