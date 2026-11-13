# clipper — clips verticales 9:16 desde directos

Herramienta personal (Debian/Ubuntu). Panel por secciones: **Directo · Vídeos · Clips · Publicación · Ajustes** (todo se configura desde el navegador, incluidas las claves de IA/APIs).

## Instalación automática (Linux)
El instalador clona el repositorio, instala las dependencias del sistema (ffmpeg, Python venv, fuentes, libs gráficas), crea el entorno virtual,
instala los paquetes de Python (torch en versión CPU, ~200 MB), descarga los modelos, crea el comando `clipper` y **verifica** todo con una
prueba de render real. Es idempotente: volver a ejecutarlo actualiza el código y las dependencias.

**Un solo comando** (repositorio público; pide `sudo` solo para instalar paquetes del sistema):
```bash
curl -fsSL https://raw.githubusercontent.com/DXZ-dev/clips/claude/determined-johnson-p5vd5b/install.sh | bash
```
Sin preguntas y con arranque automático del panel como servicio:
```bash
curl -fsSL https://raw.githubusercontent.com/DXZ-dev/clips/claude/determined-johnson-p5vd5b/install.sh | bash -s -- --yes --service
```
(Hoy el repo solo tiene esa rama; cuando exista `main`, sustitúyela en la URL. El instalador clona la rama por defecto salvo que uses `--branch`.)
Alternativa: `git clone https://github.com/DXZ-dev/clips.git && cd clips && ./install.sh`. Si el repo vuelve a ser privado, exporta `GITHUB_TOKEN` (lectura de *Contents*).

Opciones útiles (`./install.sh --help`): `--service` (servicio systemd de usuario que arranca el panel), `--port N`, `--whisper tiny|base|small|medium|large-v3`,
`--gpu`/`--cpu` (librerías CUDA para Whisper; se autodetecta NVIDIA), `--no-models`, `--dry-run` (muestra qué haría), `--uninstall [--purge]`
(`--purge` borra además modelos, ajustes, secretos, tokens y cola; nunca borra tus clips).
Probado en Ubuntu 24.04 con Python 3.11 (también 3.12 en desarrollo). Debian/Ubuntu: completo. Fedora y Arch: instalan lo básico pero **sin probar** (en Fedora, ffmpeg con libx264 requiere RPM Fusion).
Tras instalar: `clipper serve` → **http://localhost:7848** (puerto configurable con `--port` o `server.port`).

## Windows y Docker
**Recomendado en Windows: Docker Desktop (WSL2).** Ejecuta exactamente el mismo código Linux que está probado y usa tu GPU NVIDIA para Whisper.
No hay `.exe` nativo: no puedo probar Windows desde el entorno de desarrollo y prefiero no prometer lo que no he podido comprobar.

En PowerShell (instala Docker Desktop antes con `winget install -e --id Docker.DockerDesktop` si no lo tienes):
```powershell
irm https://raw.githubusercontent.com/DXZ-dev/clips/claude/determined-johnson-p5vd5b/install.ps1 | iex
```
Descarga el código, detecta la GPU, construye la imagen, arranca el contenedor y abre `http://localhost:7848`. Tus clips quedan en `<carpeta>\output_clips`.

A mano (Linux, Windows o Mac): `docker compose up -d --build` (CPU) o `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build` (GPU NVIDIA; en Linux necesita NVIDIA Container Toolkit).
- Todos los datos viven en el volumen `/data` (ajustes, secretos, tokens, modelos, cola); los clips en `./output_clips`. Variables opcionales con el formato `CLIPPER__SECCION__CLAVE`
  (p. ej. `CLIPPER__COMPUTE__DEVICE=cpu`, `CLIPPER__AI__LLM__API_KEY=…`); la primera vez descarga el modelo Whisper (`CLIPPER_AUTO_DOWNLOAD=0` para evitarlo).
- El panel solo se publica en `127.0.0.1:7848` (no tiene autenticación). OBS → `rtmp://127.0.0.1:1935/live/obs` si pones `source.type: rtmp`.
- **Estado de la imagen Docker:** en el entorno de desarrollo no pude construirla entera (no cabía en el disco de la sesión). Sí verifiqué la capa de sistema
  (Ubuntu 24.04 + ffmpeg + venv) y los pasos de Python son los del instalador de Linux, ya probado; el primer `build` en tu equipo es la prueba definitiva.
- NVENC dentro de Docker/WSL2 depende del sistema: si no está disponible, el render cae solo a `libx264` (CPU). El gran beneficio de la GPU es Whisper.

**Para tu PC (i9-10900K · 32 GB · RTX 3060 Ti 8 GB):** sobra potencia. Whisper `small` (o `medium`) en GPU transcribe mucho más rápido que el tiempo real y cabe de sobra en 8 GB de VRAM,
aunque comparte la VRAM con el juego y OBS. Si notas tirones mientras juegas, limita WSL2 creando `%USERPROFILE%\.wslconfig` con `[wsl2]` `processors=6` `memory=12GB`
y no analices vídeos largos mientras juegas. Alternativa si el PC de juego va justo: deja el análisis en otra máquina (tu Proxmox) y dale la URL del directo de Twitch/YouTube (`streamlink`).

## Directo y vídeos grabados
- **En directo:** con la ingesta en marcha, clipper saca clips solo de los momentos destacados (palabras clave, risas, picos de volumen, habla acelerada). Se activa/desactiva en **Directo → Clips automáticos**.
- **Vídeo ya grabado** (pestaña **Vídeos**): arrastra el archivo (hasta `vod.max_upload_gb`, 20 GB) o pega un enlace (YouTube, VOD de Twitch… vía yt-dlp). Se analiza entero,
  se puntúan los momentos (palabra clave 3 · risa 2,5 · pico de volumen 2 · cadencia 1,5, y los cercanos se suman), se eligen los mejores separados `vod.min_gap_seconds` y se crean hasta `vod.max_clips` clips
  con subtítulos, ajustados al inicio/fin de frase. Todos comparten la misma fuente, así que en el editor puedes ampliar el recorte a cualquier punto del vídeo.
  Sin transcripción solo busca picos de volumen (mucho más rápido). Con CPU, Whisper `small` puede tardar del orden de la duración del vídeo; con GPU, una fracción.
- **Subir y publicar tal cual:** el vídeo se guarda sin tocar y entra en la cola de publicación de las redes que elijas (con los mismos límites y modo de revisión).
- Los clips generados de ambas formas entran en la cola de publicación según el modo de cada destino (Revisión por defecto).

## Instalación manual (Ubuntu/Debian)
```bash
sudo apt install -y ffmpeg python3-venv fonts-dejavu-core libegl1   # libegl1: lo exige mediapipe
python3.12 -m venv .venv && . .venv/bin/activate                    # 3.11/3.12 recomendado
pip install -r requirements.txt && pip install --no-deps -e .
./scripts/download_models.sh                                        # modelo de rostros de MediaPipe
clipper download-model small                                        # Whisper (única descarga; luego todo es local)
./scripts/check_env.sh && clipper check                             # qué encoder se usará
```
Python 3.13 funciona para el núcleo, pero torch/mediapipe/faster-whisper suelen tardar en publicar wheels: usa 3.12.

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

## Claves y proveedores de IA (se pegan en el panel, nunca en el código)
**Ajustes → IA de texto** (ganchos, títulos y descripciones; botón «✨ Sugerir con IA» en el editor y «✨ Generar con IA» en la cola de publicación) y
**Ajustes → Transcripción → API** (alternativa a Whisper local). Cada sección tiene **«Guardar y probar conexión»**.
- Proveedores de texto: cualquier API compatible con OpenAI (OpenAI, OpenRouter, Groq, Ollama, LM Studio… con su URL base y modelo) o Anthropic.
- Las claves se guardan en `secrets.local.yaml` (permisos 600, fuera de git; en Docker, en el volumen `/data`), no se muestran de nuevo y nunca van a `config.yaml`.
  También se aceptan por variable de entorno (`CLIPPER__AI__LLM__API_KEY`, `CLIPPER_API_KEY`).
- Con la IA activada se envía a ese servicio la transcripción del clip; usa Ollama/LM Studio en tu red si no quieres que salga. «Generar título al encolar» + modo Auto de publicación publica sin revisión humana (y el texto del directo podría intentar manipular a la IA): déjalo apagado salvo que lo quieras.

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
