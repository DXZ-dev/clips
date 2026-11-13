# clipper — clips verticales 9:16 desde directos

Herramienta personal (Debian/Ubuntu). Panel por secciones: **Directo · Vídeos · Clips · Publicación · Ajustes** (todo se configura desde el navegador, incluidas las claves de IA/APIs).

## Instalación en un servidor Linux (recomendado)
clipper corre en un servidor Linux (un LXC/VM de Proxmox, un mini PC, una Raspberry de 64 bits…) y lo usas **desde el navegador de cualquier PC de tu red**.
No hace falta Docker, Windows ni ningún token: el repositorio es público.

**Un solo comando, como root o con sudo, en una máquina Debian 12/13 o Ubuntu 22.04/24.04:**
```bash
curl -fsSL https://raw.githubusercontent.com/DXZ-dev/clips/HEAD/install.sh | bash -s -- --server
```
Te pide una contraseña para el panel (mín. 8 caracteres) y hace todo lo demás: dependencias del sistema, entorno de Python, modelos, servicio systemd que arranca solo
con el servidor, y una prueba de render real al final. Al terminar te dice la dirección, p. ej. **`http://192.168.1.50:7848`**: ábrela en el navegador de tu PC e inicia sesión.

- Sin `--server` queda solo para ese equipo (`http://localhost:7848`, sin contraseña). Puerto: `--port N` (por defecto **7848**).
- Para no teclear la contraseña (automatizado): `CLIPPER_PASSWORD='mi-clave-larga' curl … | bash -s -- --server --yes`.
- Actualizar: vuelve a ejecutar el mismo comando (conserva ajustes, claves, clips y contraseña). Desinstalar: `bash ~/.local/share/clipper/install.sh --uninstall [--purge]`.
- Otras opciones (`--help`): `--whisper tiny|base|small|medium|large-v3`, `--gpu`/`--cpu`, `--no-models`, `--dry-run`, `--dir`, `--branch`.
- **Seguridad:** el panel **se niega a abrirse a la red sin contraseña**. Con contraseña usa sesión por cookie firmada, bloqueo tras 5 intentos fallidos y protección CSRF. Va por HTTP:
  úsalo solo en tu red local, o ponle delante un proxy HTTPS (Caddy/Nginx/Cloudflare Tunnel) si lo expones fuera. Cambiar la contraseña: Ajustes → Seguridad, o `clipper set-password`.

**Crear el servidor en Proxmox (LXC):** *Create CT* → plantilla Debian 13 o Ubuntu 24.04, 4 núcleos, 8 GB de RAM, 40+ GB de disco (los clips y fuentes ocupan espacio), red con IP fija → arráncalo, entra por consola y pega el comando de arriba.
No instales clipper directamente en el host de Proxmox. Un LXC no ve tu GPU de forma sencilla: en CPU, Whisper `small` va aproximadamente a tiempo real; si quieres más velocidad sin GPU,
usa una API de transcripción (Ajustes → Transcripción → API, p. ej. un servicio compatible con OpenAI) o un modelo más pequeño (`base`).

Desde un clon local también vale: `git clone https://github.com/DXZ-dev/clips.git && cd clips && ./install.sh --server`.
Probado: Ubuntu 24.04 (Python 3.11, 3.12 y 3.13). Debian 13 y el servicio systemd, sin probar en una máquina real. Fedora y Arch: sin probar.

## Alternativa: Windows y Docker (opcional)
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

## Pestaña Directo: todo en un sitio
- **Ventana en vivo:** el directo que recibe clipper, reproducido en el navegador (HLS; la librería `hls.js` va incluida, sin CDN). Retraso aproximado 10-20 s.
- **Previa del clip (9:16):** cómo quedaría un clip ahora mismo, con el diseño y los subtítulos actuales. «Cámara + gameplay» pone **tu cámara arriba y el centro del gameplay recortado abajo** (llenando el ancho,
  sin la cámara original de la esquina). **📷 Marcar la cámara:** toma una imagen del directo, ajusta el rectángulo (o pulsa Detectar) y guárdalo una vez: se usa en todos los clips.
- **Subtítulos:** estilo rápido, letra, tamaño, borde, sombra, colores, posición (incluida la unión cámara/gameplay), subir/bajar y palabras por línea; la previa se actualiza sola.
  «✔ Los subtítulos están bien» los marca como revisados (el modo automático te avisa si no lo hiciste).
- **Modo automático de clips** (Manual · Automático a revisión · Automático total) con **pausar / reanudar** (también en la cabecera). Sensibilidad Baja/Media/Alta, duración, máximo por hora y separación mínima.
  Los clips automáticos se deciden **por puntuación**: se espera ~6 s a que lleguen más señales del mismo momento y solo se crea el clip si supera la puntuación mínima.
  Señales: palabras clave (3) · risas (2,5) · reacción del streamer en cámara (2,2) · pico de volumen (2) · movimiento en pantalla (1,8, más si es céntrico) · habla rápida (1,5); +15 % si hay personajes en pantalla.
  **Sin cámara** el movimiento pesa 1,5× y el recorte «Automático» sigue a personas/personajes y a la acción con preferencia por el centro (y vuelve al centro si no hay nada claro).
- **Redes inline:** activa YouTube/TikTok/Instagram/Webhook desde la propia tarjeta (con «Conectar»/«Configurar»); cada clip también tiene sus botones «→ red» en **Clips** para publicar a mano.
- **Cada clip** lleva un **título automático** (lo que dice el clip; con IA si activas «generar con IA») y su **canal de origen** (p. ej. `kick · epidemic77`), con filtro por canal en **Clips**. El título pasa a la publicación.
- **Actualizar:** Ajustes → Actualizaciones compara con GitHub, avisa si hay versión nueva y la instala con un clic (requiere el servicio de systemd: instala con `--server`).

## Dónde pongo el enlace de mi canal (Twitch, Kick, YouTube)
Pestaña **Directo → Fuente del directo → 📺 Twitch · Kick · YouTube**: pega el enlace de tu canal (`twitch.tv/tucanal`, `kick.com/tucanal`, `youtube.com/@canal/live`) o el de un directo concreto,
pulsa **Probar enlace** (te dice la plataforma, si está en directo y el título) y luego **Guardar y conectar**. No hace falta cuenta, token ni clave: se lee el directo público
con `streamlink` y, si falla, `yt-dlp`. Si el canal no está en directo, queda esperando y se conecta solo (reintenta cada 15 s). Para OBS elige «OBS (RTMP)»: la propia tarjeta te da el servidor y la clave.
Con enlace hay ~10-20 s de retraso respecto al directo real, y los directos de Twitch pueden insertar anuncios en la señal.
Los enlaces de **vídeos** (YouTube, VOD de Twitch/Kick) van en la pestaña **Vídeos** y usan `yt-dlp`; desde servidores YouTube a veces pide verificar que no eres un robot:
exporta tus cookies a un `cookies.txt` y pon la ruta en Ajustes → Vídeos. `yt-dlp` y `streamlink` se actualizan solos cada vez que ejecutas el instalador (los sitios cambian a menudo).
*Estas integraciones se probaron con herramientas simuladas; desde el entorno de desarrollo no hay acceso a Twitch, Kick ni YouTube, así que conviene que pruebes con tu canal real.*

## Previa del clip en tiempo real
En **Directo → Previa del clip (9:16)** ves cómo quedaría un clip de los últimos 8 s con el diseño y los subtítulos actuales; cambia entre *Automático / Cámara + gameplay / Completo*
y se regenera. Con «Previa en vivo» se actualiza cada ~12 s (consume algo de CPU; en baja resolución). No es vídeo continuo: es una previa casi en tiempo real.

## Diagnóstico: `clipper selftest`
Comprueba **en tu máquina** (con su ffmpeg y su Python) el render de cada diseño y el camino completo directo → clip con segmentos, e indica qué falla. El instalador lo ejecuta al terminar.
Si un clip falla, el render ya reintenta solo con recorte fijo y luego con el diseño «Completo» (el clip lleva el campo `fallback` en su `.json`); si ves el error `Parsed_crop_0 … Invalid argument`,
actualiza (`git pull` + reinstalar) y ejecuta `clipper selftest`; si algo sigue en FALLA, envía esa salida.

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
clipper serve                            # motor + panel en http://localhost:7848 (con --host 0.0.0.0 exige contraseña)
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
- Proveedores de texto: **Google Gemini** (clave gratuita en Google AI Studio; la clave va en cabecera), **Anthropic** (Claude) o cualquier API compatible con OpenAI (OpenAI, OpenRouter, Groq, Ollama, LM Studio… con su URL base y modelo).
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
