# clipper — clips verticales 9:16 desde directos

Herramienta personal (Debian/Ubuntu). Estado: **Fase 1 completa** (buffer + reframe 9:16 con tracking + subtítulos ASS).
Fase 2 (faster-whisper + silero-vad, disparadores) y Fase 3 (FastAPI + panel) pendientes.

## Instalación (Ubuntu/Debian)
```bash
sudo apt install -y ffmpeg python3-venv fonts-dejavu-core libegl1   # libegl1: lo exige mediapipe
python3.12 -m venv .venv && . .venv/bin/activate                    # 3.11/3.12 recomendado
pip install -r requirements.txt pytest
./scripts/download_models.sh                                        # modelo de rostros de MediaPipe
./scripts/check_env.sh && python -m clipper check                   # qué encoder se usará
```
Python 3.13 funciona para el núcleo, pero torch/mediapipe/faster-whisper (Fase 2) suelen tardar en publicar wheels: usa 3.12.

## Uso (Fase 1)
```bash
python -m clipper reframe entrada.mp4 -o salida.mp4 --words palabras.json -s 30   # últimos 30 s de un archivo
python -m clipper ingest                 # terminal 1: mantiene el buffer circular (/tmp/clip_buffer)
python -m clipper clip -s 30             # terminal 2: clip de los últimos 30 s del buffer
```
`palabras.json`: `[{"start":1.0,"end":1.4,"word":"mira"}, ...]` (segundos relativos al clip; en Fase 2 lo generará faster-whisper).

Fuentes (`config.yaml → source.type`): `file` (simula directo con `-re`), `rtmp` (ffmpeg escucha; OBS → `rtmp://IP:1935/live` clave `obs`),
`hls`, `streamlink`, `ytdlp`. Archivo en grabación: `file_follow: true` con `.ts/.mkv/.flv` (un `.mp4` no es legible hasta cerrarse).

## Estructura
```
config.yaml            toda la configuración (validada con pydantic en clipper/config.py)
clipper/sources.py     construye la entrada ffmpeg (RTMP/HLS/streamlink/yt-dlp/archivo)
clipper/buffer.py      RollingBuffer (escribe segmentos de 5 s, descarta >90 s) + BufferReader (otro proceso puede leer)
clipper/audio.py       extracción de audio y energía en dB
clipper/tracking.py    rostros (MediaPipe/Haar), hablante activo, movimiento, suavizado EMA+zona muerta+límite de velocidad
clipper/subtitles.py   generador .ass palabra a palabra
clipper/render.py      ffmpeg: crop móvil -> 1080x1920 -> ass; NVENC con fallback automático a libx264
clipper/pipeline.py    orquestación; clipper/cli.py  línea de comandos
tests/                 pytest (lógica pura)
```

## Decisiones/limitaciones a tener en cuenta
- **NVENC se prueba de verdad** (codifica 2 frames): ffmpeg de Ubuntu trae `h264_nvenc` compilado aunque no haya GPU.
- Buffer con `-c copy`: sin CPU, pero los cortes caen en keyframes (segmentos ≈5 s si la fuente tiene GOP ≤5 s; si no, `buffer.reencode: true`). Latencia: el segmento en curso (≤5 s) aún no está disponible.
- Si la ingesta se reconecta se **vacía el buffer** (los timestamps se reinician).
- Hablante activo = rostro con más movimiento en la zona de la boca mientras hay voz (proxy barato, sin landmarks). Se puede afinar con FaceLandmarker en una fase posterior.
- Los wheels de OpenCV 5 no traen los cascades Haar; por eso el detector por defecto es MediaPipe. Sin rostros, la cámara sigue el centro de masa del movimiento.
