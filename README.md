# clipper — clips verticales 9:16 desde directos

Herramienta personal (Debian/Ubuntu). Estado: **Fases 1, 2 y 3 completas** (buffer + reframe 9:16 + subtítulos ASS · transcripción y disparadores · panel web).

## Instalación (Ubuntu/Debian)
```bash
sudo apt install -y ffmpeg python3-venv fonts-dejavu-core libegl1   # libegl1: lo exige mediapipe
python3.12 -m venv .venv && . .venv/bin/activate                    # 3.11/3.12 recomendado
pip install -r requirements.txt
./scripts/download_models.sh                                        # modelo de rostros de MediaPipe
./scripts/check_env.sh && python -m clipper check                   # qué encoder se usará
```
Python 3.13 funciona para el núcleo, pero torch/mediapipe/faster-whisper (Fase 2) suelen tardar en publicar wheels: usa 3.12.

## Arranque rápido
```bash
python -m clipper serve                  # motor + panel en http://localhost:8000
python -m clipper serve --device cpu     # solo CPU   (también: --device cuda | auto)
python -m clipper run                    # sin web: Enter/c = clip, 15|30|60 = clip de N s, q = salir
curl -X POST localhost:8000/clip -H 'content-type: application/json' -d '{"seconds":30}'   # trigger manual HTTP
```
El panel permite lanzar clips (15/30/60 s, tecla **C**), ver el buffer, la transcripción y los eventos,
previsualizar/descargar/borrar clips y **cambiar entre GPU NVIDIA y solo CPU en caliente**.

## Privacidad: 100 % local por defecto
- En ejecución **no se contacta ningún servidor externo**: Whisper corre en tu máquina con `local_files_only` (`transcribe.allow_downloads: false`) y `HF_HUB_OFFLINE=1`; el panel no carga CDNs. Verificado transcribiendo con la red bloqueada.
- La única operación con red es **descargar el modelo una vez**, y es explícita: `python -m clipper download-model small` (guarda en `./models/whisper-small`). El modelo de rostros también se baja una vez con `scripts/download_models.sh`.
- Si prefieres un servicio de transcripción por API, se configura **desde el panel → ⚙️ Ajustes → Transcripción → API** (URL base, modelo y clave; cualquier servicio compatible con OpenAI `/audio/transcriptions`, incluido uno tuyo en `localhost`). Con «API» el audio del directo sale hacia esa URL. La clave se guarda en `secrets.local.yaml` (permisos 600, fuera de git; también vale `CLIPPER_API_KEY`) y la API del panel nunca la devuelve.
- Los ajustes del panel se guardan en `settings.local.yaml` y se aplican en caliente (cambiar la fuente reinicia la ingesta y vacía el buffer). `buffer.dir` y `clip.output_dir` solo se editan en `config.yaml`.
- El panel rechaza peticiones con `Host` no local u `Origin` ajeno (anti DNS-rebinding/CSRF). Si lo sirves en otro nombre, añádelo a `server.allowed_hosts`.

## GPU o CPU (`compute.device` en config.yaml, `--device`, o el panel)
| valor | Whisper | Encoder |
|---|---|---|
| `auto` | CUDA si está disponible | NVENC si funciona, si no libx264 |
| `cuda` | CUDA (avisa y cae a CPU si no hay) | NVENC (avisa y cae a libx264 si no hay) |
| `cpu`  | CPU (`int8`) | `libx264 -preset veryfast` |

Para Whisper en GPU necesitas CUDA 12 + cuDNN 9 (`pip install nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*`). Con CPU de 4 núcleos
el modelo `small` siguió el ritmo del directo (retraso 5–7 s); si tu CPU se queda atrás usa `transcribe.model: base` o `tiny`.

## Disparadores (`triggers:` en config.yaml)
- **Palabras clave** (`keywords.phrases`, sin acentos/mayúsculas) y **risas** escritas por Whisper ("jajaja").
- **Energía**: subida ≥ `spike_db` sobre la mediana de los últimos 60 s. **Cadencia**: ≥ `words_per_second` palabras/s.
- **Manual**: `POST /clip`, botón/tecla C del panel, o Enter en `run`.
- Los automáticos respetan `cooldown_seconds`; el clip incluye `post_roll_seconds` tras el momento clave.
- La risa solo se detecta por texto y los picos por volumen: no hay clasificador de audio de risa.

## Uso por piezas (Fase 1)
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
clipper/detect/        vad.py (silero) · transcriber.py (faster-whisper) · triggers.py
clipper/engine.py      motor: buffer + transcripción + disparadores + cola de clips
clipper/web/           app.py (FastAPI) + static/index.html (panel)
tests/                 pytest (lógica pura)
```

## Decisiones/limitaciones a tener en cuenta
- **NVENC se prueba de verdad** (codifica 2 frames): ffmpeg de Ubuntu trae `h264_nvenc` compilado aunque no haya GPU.
- Buffer con `-c copy`: sin CPU, pero los cortes caen en keyframes (segmentos ≈5 s si la fuente tiene GOP ≤5 s; si no, `buffer.reencode: true`). Latencia: el segmento en curso (≤5 s) aún no está disponible.
- El panel no tiene autenticación: escucha en 127.0.0.1; no lo expongas a la red.
- Si la ingesta se reconecta se **vacía el buffer** (los timestamps se reinician).
- Hablante activo = rostro con más movimiento en la zona de la boca mientras hay voz (proxy barato, sin landmarks). Se puede afinar con FaceLandmarker en una fase posterior.
- Los wheels de OpenCV 5 no traen los cascades Haar; por eso el detector por defecto es MediaPipe. Sin rostros, la cámara sigue el centro de masa del movimiento.
