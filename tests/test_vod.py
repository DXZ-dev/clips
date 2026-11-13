import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper import editor as ed
from clipper.config import Config
from clipper.detect.triggers import Trigger
from clipper.engine import Engine
from clipper.subtitles import Word
from clipper.vod import Moment, clip_window, find_highlights, iter_audio, select_moments
from clipper.web.app import create_app


def T(kind, t, strength=1.0, detail=""):
    return Trigger(kind, t, detail, strength)


# ------------------------------------------------------------------ puntuación y selección
def test_select_moments_clusters_scores_and_spacing():
    trig = [T("keyword", 100, detail="mira esto"), T("energy", 108, 1.2), T("laughter", 112),   # 1 momento fuerte
            T("cadence", 300, 1.0),                                                             # débil
            T("keyword", 330), T("keyword", 340),                                               # otro, cerca de 300
            T("energy", 600, 0.6)]
    ms = select_moments(trig, max_clips=3, min_gap=45, merge_window=20)
    assert len(ms) == 3 and [round(m.t) for m in ms][0] == 100
    assert ms[0].kinds == ["keyword", "laughter", "energy"] and ms[0].score > 3.0      # motivo principal primero
    assert all(b.t - a.t >= 45 for a, b in zip(ms, ms[1:]))
    assert len(select_moments(trig, 1, 45, 20)) == 1 and select_moments(trig, 1, 45, 20)[0].t == 100
    assert select_moments([], 5, 45, 20) == []


def test_clip_window_bounds_and_word_snap():
    m = Moment(t=50, score=3, kinds=["keyword"])
    assert clip_window(m, 600, 30, 8) == (28.0, 58.0)
    assert clip_window(Moment(2, 1, ["energy"]), 600, 30, 8) == (0.0, 30.0)            # pegado al inicio
    t0, t1 = clip_window(Moment(595, 1, ["energy"]), 600, 30, 8)
    assert t1 == 600 and t0 == 570                                                      # pegado al final
    words = [Word(27.0, 27.4, "hola"), Word(57.6, 58.2, "fin.")]
    assert clip_window(m, 600, 30, 8, words) == (27.0, 58.2)                            # ajustado a frases
    assert clip_window(m, 12, 30, 8)[1] <= 12                                           # vídeo más corto que el clip


# ------------------------------------------------------------------ análisis real sobre un vídeo sintético
@pytest.fixture(scope="module")
def vod_file(tmp_path_factory):
    """70 s: ruido bajo + dos ráfagas fuertes (a 30 s y 55 s)."""
    p = tmp_path_factory.mktemp("vod") / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=70", "-f", "lavfi",
                    "-i", "anoisesrc=d=70:c=white:a=0.01:r=16000", "-af", "volume='if(between(t,30,32)+between(t,55,57),30,1)':eval=frame",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(p)], check=True)
    return p


def cfg_for(tmp_path):
    c = Config()
    c.clip.output_dir, c.buffer.dir = str(tmp_path / "out"), str(tmp_path / "buf")
    c.transcribe.enabled = False
    c.reframe.detector = "none"
    c.triggers.energy.warmup_seconds = 10
    return c


def test_iter_audio_chunks_cover_whole_video(vod_file):
    chunks = list(iter_audio(str(vod_file), chunk_s=20))
    assert [round(t) for t, _ in chunks] == [0, 20, 40, 60]
    assert abs(sum(len(a) for _, a in chunks) / 16000 - 70) < 0.5


def test_find_highlights_energy_only_progress_and_cancel(vod_file, tmp_path):
    cfg, seen = cfg_for(tmp_path), []
    trigs, words, dur = find_highlights(cfg, str(vod_file), False, lambda p, m: seen.append(p))
    assert abs(dur - 70) < 1 and words == []
    ts = sorted(round(t.t) for t in trigs if t.kind == "energy")
    assert any(29 <= t <= 33 for t in ts) and any(54 <= t <= 58 for t in ts)
    assert seen[-1] == pytest.approx(1.0, abs=0.01) and seen == sorted(seen)
    from clipper.vod import Cancelled
    with pytest.raises(Cancelled):
        find_highlights(cfg, str(vod_file), False, cancelled=lambda: True)


def test_find_highlights_uses_transcript_keywords(vod_file, tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.transcribe.enabled = True

    class Fake:   # transcriptor falso: "mira esto" a los 12 s
        def __init__(self, cfg, on_words):
            self.on, self.done = on_words, False
        def load(self): pass
        def feed(self, audio, t0):
            if not self.done and t0 >= 0:
                self.done = True
                self.on([Word(12.0, 12.4, "Mira"), Word(12.4, 12.8, "esto,")])
        def flush(self): pass

    trigs, words, _ = find_highlights(cfg, str(vod_file), True, transcriber_factory=Fake)
    assert any(t.kind == "keyword" and abs(t.t - 12.8) < 0.1 for t in trigs) and len(words) == 2


# ------------------------------------------------------------------ trabajo completo: subir -> clips editables -> borrar
def test_video_job_end_to_end_and_shared_source(vod_file, tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.vod.max_clips, cfg.vod.clip_seconds, cfg.vod.min_gap_seconds = 2, 15, 20
    eng = Engine(cfg, data_dir=tmp_path)
    vm = eng.videos
    job, path = vm.begin_upload("mi partida.mp4", "highlights", {"transcribe": False, "max_clips": 2, "clip_seconds": 15})
    path.write_bytes(vod_file.read_bytes())
    vm.finish_upload(job)
    vm._run(vm.jobs[job.id])
    j = vm.jobs[job.id]
    assert j.status == "done" and len(j.clips) == 2, j.message
    assert j.progress == 1.0
    out = ed.out_dir(cfg)
    metas = [json.loads((out / c).with_suffix(".json").read_text()) for c in j.clips]
    assert all(m["reason"] == "energy" and m["video"] == "mi partida.mp4" and 14 <= m["duration"] <= 16 for m in metas)
    shared = ed.src_dir(cfg) / f"vod_{job.id}.src.mp4"
    assert shared.exists() and all(ed.source_path(cfg, Path(c).stem) == shared for c in j.clips)   # una sola fuente
    spec = ed.load_edit(cfg, j.clips[0])["spec"]
    assert spec["trim_end"] - spec["trim_start"] == pytest.approx(15, abs=0.1)
    # «Guardar como nuevo» no copia la fuente compartida
    from clipper.spec import EditSpec
    new = ed.apply(cfg, j.clips[0], EditSpec.model_validate(spec), as_new=True)
    assert ed.source_path(cfg, Path(new).stem) == shared
    # la fuente compartida solo se borra cuando ya nadie la usa
    for c in [*j.clips, new]:
        assert shared.exists()
        ed.remove_clip_files(cfg, Path(c).stem)
    assert not shared.exists()


def test_video_without_highlights_and_cancel(tmp_path):
    cfg = cfg_for(tmp_path)
    eng = Engine(cfg, data_dir=tmp_path)
    quiet = tmp_path / "q.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=10:d=25", "-f", "lavfi", "-i",
                    "anoisesrc=d=25:a=0.01:r=16000", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(quiet)], check=True)
    job, path = eng.videos.begin_upload("q.mp4", "highlights", {"transcribe": False})
    path.write_bytes(quiet.read_bytes())
    eng.videos.finish_upload(job)
    eng.videos._run(job)
    assert job.status == "done" and job.clips == [] and "No se detectaron" in job.message
    j2, p2 = eng.videos.begin_upload("q.mp4", "highlights", {})
    p2.write_bytes(b"x")
    eng.videos.finish_upload(j2)
    eng.videos.cancel(j2.id)
    assert eng.videos.jobs[j2.id].status == "cancelled"


def test_upload_validation_and_asis(tmp_path):
    cfg = cfg_for(tmp_path)
    eng = Engine(cfg, data_dir=tmp_path)
    with pytest.raises(ValueError, match="Formato"):
        eng.videos.begin_upload("virus.exe", "highlights", {})
    with pytest.raises(ValueError, match="enlace"):
        eng.videos.submit_url("--exec=rm -rf", "highlights", {})
    with pytest.raises(ValueError, match="enlace"):
        eng.videos.submit_url("file:///etc/passwd", "highlights", {})


# ------------------------------------------------------------------ API
def test_video_api_upload_and_asis_publish(vod_file, tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.publish.enabled, cfg.publish.dry_run = True, True
    cfg.publish.webhook.enabled, cfg.publish.webhook.url = True, "http://localhost:9/h"
    cfg.publish.min_gap_minutes = 0
    eng = Engine(cfg, data_dir=tmp_path)
    c = TestClient(create_app(eng, manage_engine=False), base_url="http://localhost")
    data = vod_file.read_bytes()
    assert c.post("/api/videos/upload?filename=x.exe", content=b"1").status_code == 422
    assert c.post("/api/videos/upload?filename=a.mp4&mode=zzz", content=data).status_code == 422
    assert c.post("/api/videos/upload?filename=a.mp4&platforms=tiktok", content=data).status_code == 422   # destino no activado
    assert c.post("/api/videos/upload?filename=a.mp4", content=b"").status_code == 422                      # vacío
    cfg.vod.max_upload_gb = 0.00001
    assert c.post("/api/videos/upload?filename=a.mp4", content=data).status_code == 413
    cfg.vod.max_upload_gb = 20
    r = c.post("/api/videos/upload?filename=clip.mp4&mode=asis&platforms=webhook&title=Mi%20t%C3%ADtulo&description=hola", content=data)
    assert r.status_code == 200
    jid = r.json()["id"]
    eng.videos._run(eng.videos.jobs[jid])                               # el hilo de fondo no corre en este test
    j = [x for x in c.get("/api/videos").json() if x["id"] == jid][0]
    assert j["status"] == "done" and j["path"] is None and j["clips"] == [f"upload_{jid}.mp4"]
    items = c.get("/api/publish").json()["items"]
    assert items[0]["status"] == "queued" and items[0]["title"] == "Mi título"
    assert eng.publisher.process_one() == items[0]["id"]                # simulacro: publicado
    assert c.post("/api/videos/import", json={"url": "no-es-url"}).status_code == 422
    assert c.post("/api/videos/nope/cancel").status_code == 404
    assert c.delete(f"/api/videos/{jid}").status_code == 200
