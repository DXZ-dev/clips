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
python -m clipper serve                  # motor + panel en http://localhost:7848
python -m clipper serve --device cpu     # solo CPU   (también: --device cuda | auto)
python -m clipper run                    # sin web: Enter/c = clip, 15|30|60 = clip de N s, q = salir
curl -X POST localhost:7848/clip -H 'content-type: application/json' -d '{"seconds":30}'   # trigger manual HTTP
```
El panel permite lanzar clips (15/30/60 s, tecla **C**), ver el buffer, la transcripción y los eventos,
previsualizar/descargar/borrar clips y **cambiar entre GPU NVIDIA y solo CPU en caliente**.

## Diseños de clip y editor (Fase 5)
**Diseños** (`layout.mode`, o por clip en el editor):
- `auto`: recorte 9:16 que sigue al hablante / la acción (como hasta ahora).
- `split`: **cámara del streamer arriba y gameplay abajo** (o al revés). La cámara se **autodetecta** buscando un rostro pequeño y fijo en las esquinas
  y se encuadra centrada en la cara; si no la encuentra cae a `auto` con un aviso, y siempre puedes dibujar el rectángulo a mano en el editor
  (o fijarlo en `layout.facecam`). El gameplay puede ir completo sobre fondo difuminado (`contain`) o recortado siguiendo la acción sin contar la cámara (`cover`).
- `fit`: pantalla completa centrada sobre su propio fondo difuminado.
CLI: `python -m clipper reframe stream.mp4 --layout split [--facecam 0.75,0.62,0.25,0.38]`.

**Editor** (botón ✂️ Editar en cada clip del panel). Cada clip guarda su vídeo fuente (con 6 s extra por lado, ~10-30 MB) en `output_clips/_src/`:
recortar inicio/fin (puedes **ampliar** más allá del clip original) · cambiar diseño, cámara arriba/abajo y altura · **6 estilos de subtítulos**
(clasico, hormozi, beast, neon, fuego, minimal) con color/tamaño/posición propios (incluida la unión cámara/gameplay) · **corregir o borrar palabras** de la transcripción ·
**gancho** (rótulo al empezar) · **marca de agua** (@usuario) · previsualización rápida a 540×960 · «Aplicar» sobrescribe o «Guardar como nuevo» crea `_v2`, `_v3`…
Desactívalo con `editor.keep_sources: false` si prefieres ahorrar disco (los clips dejan de ser editables).
Limitaciones: la cámara se amplía a 1080 px de ancho, así que una cámara de poca resolución se verá blanda; con `contain` la cámara aparece también en la esquina del gameplay.

## Publicación automática en redes (Fase 4)
Activa **Ajustes → Publicación**. Cada clip nuevo crea una entrada por destino activado y, según el modo, espera tu aprobación
(**Revisión**, por defecto) o se sube solo (**Auto**). Cola persistente en `data/publish.db`; reintentos con espera (1-2-4 min),
tope diario por destino (`max_per_day`) y separación mínima entre posts (`min_gap_minutes`). Los clips manuales no entran solos
(`include_manual`); usa el botón «→ Destino» de cada clip, que equivale a aprobarlo. **«Simulacro»** recorre todo el flujo sin tocar redes.

| Destino | API | Qué necesitas |
|---|---|---|
| YouTube Shorts | YouTube Data API v3 (subida reanudable) | Proyecto de Google Cloud, cliente OAuth «Web» con el *redirect URI* que muestra el panel, y pulsar **Conectar** |
| TikTok | Content Posting API (Direct Post) | App en TikTok for Developers con `video.publish`, *redirect URI* registrado, **Conectar** |
| Instagram Reels | Graph API (subida reanudable) | Cuenta profesional + token largo de Meta + ID de usuario (se pegan en Ajustes) |
| Webhook | tu URL | n8n/Make/Zapier/bot propio: recibe `file`, `title`, `description`, `meta` y lo reparte (X, Telegram, Discord…) |

Plantillas de título/descripción con `{headline} {transcript} {reason} {detail} {hashtags} {date}`.
Los secretos van a `secrets.local.yaml` y los tokens OAuth a `tokens.local.json` (ambos 600, fuera de git, nunca se devuelven por la API).

**Límites reales de las plataformas** (no los puede resolver el código): una app de YouTube sin auditar sube los vídeos como *privados*;
TikTok sin auditar solo permite `SELF_ONLY`; los tokens de Meta caducan a los ~60 días; TikTok exige que el usuario revise el contenido
antes de publicar, así que `auto` puede no pasar su auditoría. Por eso la visibilidad por defecto es *privada* y el modo es *Revisión*.
Eres responsable de los derechos de lo que emites (música, juegos, terceros) y de cumplir las normas de cada plataforma.
Los cuatro destinos están probados **solo con respuestas HTTP simuladas** (según la documentación de cada API), no contra los servicios reales.

## Privacidad: 100 % local por defecto
- En ejecución **no se contacta ningún servidor externo**: Whisper corre en tu máquina con `local_files_only` (`transcribe.allow_downloads: false`) y `HF_HUB_OFFLINE=1`; el panel no carga CDNs. Verificado transcribiendo con la red bloqueada.
- Sin publicación activada, la única operación con red es **descargar el modelo una vez**, y es explícita: `python -m clipper download-model small` (guarda en `./models/whisper-small`). El modelo de rostros también se baja una vez con `scripts/download_models.sh`.
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
clipper/subtitles.py   generador .ass palabra a palabra + estilos predefinidos, gancho y marca de agua
clipper/spec.py        EditSpec: lo que guarda el editor (recorte, diseño, subtítulos, gancho…)
clipper/editor.py      previsualización / render final / fotogramas del editor
clipper/render.py      ffmpeg: diseños auto/split/fit -> 1080x1920 -> ass; NVENC con fallback automático a libx264
clipper/pipeline.py    orquestación; clipper/cli.py  línea de comandos
clipper/detect/        vad.py (silero) · transcriber.py (faster-whisper) · triggers.py
clipper/engine.py      motor: buffer + transcripción + disparadores + cola de clips
clipper/publish/       base.py (OAuth, tokens) · youtube/tiktok/instagram/webhook.py · metadata.py · queue.py (SQLite + worker)
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
