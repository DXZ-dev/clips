"""`clipper selftest`: comprueba en ESTA máquina (su ffmpeg, su Python) que el camino completo funciona y dice qué falla."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .config import Config
from .ffutil import find_tool, hw_report, probe, require
from .spec import EditSpec


def _ff(*args: str) -> None:
    subprocess.run([require("ffmpeg"), "-y", "-v", "error", "-nostdin", *args], check=True, capture_output=True)


def run(cfg: Config, out=print) -> int:
    """Devuelve 0 si todo va bien, 1 si algo falla."""
    from .buffer import BufferReader
    from .pipeline import clip_from_range
    from .render import render_spec
    bad = 0

    def line(ok: bool | None, name: str, detail: str = "") -> None:
        nonlocal bad
        mark = {True: "OK   ", False: "FALLA", None: "AVISO"}[ok]
        bad += ok is False
        out(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))

    ver = subprocess.run([require("ffmpeg"), "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    out(f"ffmpeg: {ver}")
    hw = hw_report()
    out(f"encoder: {hw['encoder']}   GPU NVIDIA: {hw['nvidia_gpu']}   streamlink: {bool(hw['streamlink'])}   yt-dlp: {bool(hw['yt-dlp'])}")
    with tempfile.TemporaryDirectory(prefix="selftest_") as td:
        t = Path(td)
        src = t / "src.mp4"
        _ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=8", "-f", "lavfi", "-i", "sine=f=300:d=8", "-c:v", "libx264",
            "-preset", "ultrafast", "-g", "25", "-c:a", "aac", "-shortest", str(src))
        c = cfg.model_copy(deep=True)
        c.reframe.detector = "none"
        out("Render de un archivo:")
        for layout, extra in (("auto", {}), ("fit", {}), ("split", {"facecam": {"x": 0.7, "y": 0.6, "w": 0.3, "h": 0.4}}),
                              ("split", {"facecam": {"x": 0.7, "y": 0.6, "w": 0.3, "h": 0.4}, "gameplay": "cover"})):
            name = f"diseño {layout}" + (" (gameplay recortado)" if extra.get("gameplay") == "cover" else "")
            try:
                meta = render_spec(c, src, EditSpec(layout=layout, trim_start=1, trim_end=6, **extra), t / f"o_{layout}.mp4")
                i = probe(str(t / f"o_{layout}.mp4"))
                if (i.width, i.height) != (1080, 1920):
                    line(False, name, f"salida {i.width}x{i.height}")
                elif meta.get("fallback"):
                    line(None, name, f"funciona SOLO con el respaldo '{meta['fallback']}': el recorte normal falla en tu ffmpeg")
                else:
                    line(True, name)
            except Exception as e:  # noqa: BLE001
                line(False, name, str(e)[-300:])

        out("Camino de directo (segmentos → clip):")
        buf = t / "buf"
        buf.mkdir()
        try:
            _ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=40", "-f", "lavfi", "-i", "sine=f=300:d=40", "-c:v", "libx264",
                "-preset", "ultrafast", "-g", "25", "-c:a", "aac", "-f", "segment", "-segment_time", "5", "-segment_format", "mpegts",
                "-segment_list", str(buf / "list.csv"), "-segment_list_type", "csv", "-segment_list_flags", "live",
                "-reset_timestamps", "0", str(buf / "seg_%08d.ts"))
            c.buffer.dir, c.clip.output_dir = str(buf), str(t / "out")
            b = BufferReader(buf, 5, 90).bounds()
            if not b:
                line(False, "buffer de segmentos", "ffmpeg no generó la lista de segmentos")
            else:
                line(True, "buffer de segmentos", f"{b[1] - b[0]:.0f} s")
                o = clip_from_range(c, b[1] - 20, b[1] - 2, None, "selftest")
                i = probe(str(o))
                meta = __import__("json").loads(o.with_suffix(".json").read_text())
                if (i.width, i.height) != (1080, 1920):
                    line(False, "clip desde el buffer", f"salida {i.width}x{i.height}")
                elif meta.get("fallback") or meta.get("adjusted"):
                    line(None, "clip desde el buffer", f"funciona con ajustes: {meta.get('fallback') or ''} {meta.get('adjusted') or ''}".strip())
                else:
                    line(True, "clip desde el buffer", f"{i.duration:.0f} s")
        except Exception as e:  # noqa: BLE001
            line(False, "camino de directo", str(e)[-400:])
    out("Resultado: " + ("todo correcto" if not bad else f"{bad} comprobación(es) fallida(s): envía esta salida para diagnosticar"))
    return 1 if bad else 0
