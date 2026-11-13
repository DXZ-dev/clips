#!/usr/bin/env bash
# clipper — instalador para Linux (Debian/Ubuntu probado; Fedora y Arch en "mejor esfuerzo").
#
#   Un solo comando (repositorio público, sin token). Servidor Linux con panel en la red + contraseña:
#     curl -fsSL https://raw.githubusercontent.com/DXZ-dev/clips/HEAD/install.sh | bash -s -- --server
#   Solo para este equipo (panel en http://localhost:7848):
#     curl -fsSL https://raw.githubusercontent.com/DXZ-dev/clips/HEAD/install.sh | bash
#   Desde un clon local:
#     git clone https://github.com/DXZ-dev/clips.git && cd clips && ./install.sh --server
#
#   Es idempotente: volver a ejecutarlo actualiza el código y las dependencias.
set -Eeuo pipefail

# ----------------------------------------------------------------------------- valores por defecto
REPO_URL="${CLIPPER_REPO:-https://github.com/DXZ-dev/clips.git}"
BRANCH="${CLIPPER_BRANCH:-}"          # vacío = rama por defecto del repositorio
INSTALL_DIR="${CLIPPER_DIR:-$HOME/.local/share/clipper}"
BIN_DIR="${CLIPPER_BIN_DIR:-$HOME/.local/bin}"
WHISPER="small"
PORT=""
PYTHON_OVERRIDE=""
GPU="auto"            # auto | yes | no  (libs CUDA para Whisper en GPU NVIDIA)
YES=0; DRY=0; NO_MODELS=0; NO_WHISPER=0; SERVICE=0; UNINSTALL=0; PURGE=0; NO_VERIFY=0; LAN=0; SERVICE_OK=0

usage() {
  cat <<'EOF'
Uso: install.sh [opciones]

  -y, --yes              No preguntar (instala dependencias del sistema con sudo si hace falta)
      --dir DIR          Dónde instalar al ejecutar desde GitHub   (def. ~/.local/share/clipper)
      --branch RAMA      Rama de GitHub a instalar                 (def. la rama por defecto del repo)
      --repo URL         Repositorio git                           (def. https://github.com/DXZ-dev/clips.git)
      --port N           Puerto del panel                          (def. 7848)
      --python RUTA      Intérprete de Python a usar               (def. 3.12 > 3.11 > 3.13)
      --whisper MODELO   Modelo a descargar: tiny|base|small|medium|large-v3   (def. small)
      --no-whisper       No descargar ningún modelo Whisper ahora
      --no-models        No descargar ningún modelo (ni Whisper ni el de rostros)
      --gpu | --cpu      Forzar / descartar las librerías CUDA para Whisper (def. auto-detectar NVIDIA)
      --server           Modo servidor: panel accesible desde tu red con contraseña + servicio que arranca solo
                         (= --lan --service). La contraseña se pide al instalar (o CLIPPER_PASSWORD)
      --lan              Abrir el panel a la red local (pide contraseña)
      --service          Instalar y arrancar un servicio systemd (clipper serve)
      --bin-dir DIR      Dónde crear el comando «clipper»      (def. ~/.local/bin)
      --no-verify        Saltar la verificación final
      --dry-run          Mostrar lo que haría sin ejecutar nada
      --uninstall        Quitar comando, servicio y entorno virtual (conserva tus clips y ajustes)
      --purge            Con --uninstall: borrar también modelos, ajustes, secretos, tokens y cola
  -h, --help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) YES=1 ;;
    --dir) INSTALL_DIR="${2:?}"; shift ;;
    --branch) BRANCH="${2:?}"; shift ;;
    --repo) REPO_URL="${2:?}"; shift ;;
    --port) PORT="${2:?}"; shift ;;
    --python) PYTHON_OVERRIDE="${2:?}"; shift ;;
    --whisper) WHISPER="${2:?}"; shift ;;
    --no-whisper) NO_WHISPER=1 ;;
    --no-models) NO_MODELS=1 ;;
    --gpu) GPU=yes ;;
    --cpu) GPU=no ;;
    --service) SERVICE=1 ;;
    --lan) LAN=1 ;;
    --server) LAN=1; SERVICE=1 ;;
    --bin-dir) BIN_DIR="${2:?}"; shift ;;
    --no-verify) NO_VERIFY=1 ;;
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Opción desconocida: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# ----------------------------------------------------------------------------- utilidades
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else B=""; G=""; Y=""; R=""; N=""; fi
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s✔%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { printf '\n%s✘ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }
trap 'die "Falló la línea $LINENO. Revisa el registro: ${LOG:-/tmp/clipper-install.log}"' ERR

LOG="${TMPDIR:-/tmp}/clipper-install.$(date +%Y%m%d-%H%M%S).log"
: >"$LOG"

# Ejecuta un comando (o solo lo muestra con --dry-run). Salida detallada al registro; imprime un punto cada
# 10 s para que se vea que sigue trabajando (apt/pip pueden tardar minutos sin escribir nada).
run() {
  printf '  $ %s\n' "$*" >>"$LOG"
  if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; return 0; fi
  "$@" >>"$LOG" 2>&1 </dev/null &
  local pid=$! dots=0 rc=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1
    dots=$((dots + 1))
    if [ $((dots % 10)) -eq 0 ] && kill -0 "$pid" 2>/dev/null; then printf '.'; fi
  done
  wait "$pid" || rc=$?
  [ "$dots" -ge 10 ] && printf '\n'
  if [ "$rc" -ne 0 ]; then
    if [ "${RUN_QUIET:-0}" != 1 ]; then
      echo "--- últimas líneas del registro ($LOG) ---" >&2; tail -n 25 "$LOG" >&2
    fi
    return 1
  fi
  return 0
}

# Como run, pero muestra la salida en pantalla (descargas largas / instalaciones de paquetes del sistema).
live() {
  printf '  $ %s\n' "$*" >>"$LOG"
  if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; return 0; fi
  if "$@" 2>&1 </dev/null | tee -a "$LOG"; then return 0; else return 1; fi
}

# Pregunta sí/no (def. sí). Sin terminal o con --yes, asume sí. Lee de /dev/tty para funcionar con «curl | bash».
ask() {
  [ "$YES" = 1 ] && return 0
  if [ -r /dev/tty ] && [ -w /dev/tty ]; then
    local a; printf '%s [S/n] ' "$1" >/dev/tty; read -r a </dev/tty || a=""
    case "${a:-s}" in [sSyY]*) return 0 ;; *) return 1 ;; esac
  fi
  return 0
}

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; else SUDO="__none__"; fi
fi
as_root() {
  if [ "$SUDO" = "__none__" ]; then
    die "Se necesitan permisos de administrador y no hay sudo. Ejecuta como root o instala a mano: $*"
  fi
  if [ -n "$SUDO" ]; then live "$SUDO" "$@"; else live "$@"; fi
}

# ----------------------------------------------------------------------------- sistema
detect_os() {
  [ "$(uname -s)" = "Linux" ] || die "Este instalador es solo para Linux."
  OS_ID="unknown"; OS_LIKE=""; OS_NAME="Linux"
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release; OS_ID="${ID:-unknown}"; OS_LIKE="${ID_LIKE:-}"; OS_NAME="${PRETTY_NAME:-$OS_ID}"
  fi
  case " $OS_ID $OS_LIKE " in
    *" debian "*|*" ubuntu "*) PM=apt ;;
    *" fedora "*|*" rhel "*)   PM=dnf ;;
    *" arch "*)                PM=pacman ;;
    *) PM=unknown ;;
  esac
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

apt_missing() { local p; for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || printf '%s\n' "$p"; done; }

install_system_deps() {
  step "Dependencias del sistema ($OS_NAME)"
  local miss=()
  case "$PM" in
    apt)
      local glib=libglib2.0-0
      apt-cache show libglib2.0-0t64 >/dev/null 2>&1 && glib=libglib2.0-0t64   # Debian 13 / Ubuntu 24.04
      mapfile -t miss < <(apt_missing ffmpeg python3-venv python3-pip git curl ca-certificates fonts-dejavu-core libegl1 libgl1 "$glib")
      if [ "${#miss[@]}" -gt 0 ]; then
        ask "Instalar con apt: ${miss[*]}" || die "Sin esas dependencias no se puede continuar."
        as_root apt-get update
        as_root env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get -o DPkg::Lock::Timeout=180 install -y "${miss[@]}"
      fi ;;
    dnf)
      warn "Fedora/RHEL: soporte sin probar. ffmpeg completo (con libx264) requiere RPM Fusion."
      for p in ffmpeg python3 git curl dejavu-sans-fonts mesa-libEGL; do rpm -q "$p" >/dev/null 2>&1 || miss+=("$p"); done
      if [ "${#miss[@]}" -gt 0 ]; then ask "Instalar con dnf: ${miss[*]}" || die "Cancelado."; as_root dnf install -y "${miss[@]}"; fi ;;
    pacman)
      warn "Arch: soporte sin probar."
      for p in ffmpeg python git curl ttf-dejavu libglvnd; do pacman -Qi "$p" >/dev/null 2>&1 || miss+=("$p"); done
      if [ "${#miss[@]}" -gt 0 ]; then ask "Instalar con pacman: ${miss[*]}" || die "Cancelado."; as_root pacman -S --needed --noconfirm "${miss[@]}"; fi ;;
    *) warn "Distribución no reconocida: instala a mano ffmpeg (con libx264), Python ≥3.11 con venv, git y curl." ;;
  esac
  need_cmd ffmpeg || { [ "$DRY" = 1 ] || die "ffmpeg no está instalado."; }
  if [ "$DRY" != 1 ]; then
    # (se captura la salida antes de grep: con pipefail, `grep -q` cerrando el pipe haría fallar a ffmpeg por SIGPIPE)
    local enc ver
    enc="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
    [[ "$enc" == *libx264* ]] || die "Tu ffmpeg no incluye libx264 (necesario para el modo CPU)."
    ver="$(ffmpeg -version 2>/dev/null | awk 'NR==1{print $3}' || true)"
    ok "ffmpeg ${ver:-?} con libx264"
  fi
}

# ----------------------------------------------------------------------------- código desde GitHub
git_env() {
  # El token va por variables de entorno (no en la URL ni en los argumentos visibles con `ps`).
  local tok="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
  if [ -n "$tok" ]; then
    local gv; gv="$(git --version | awk '{print $3}')"
    if [ "$(printf '%s\n2.31\n' "$gv" | sort -V | head -1)" != "2.31" ]; then
      die "git $gv es demasiado antiguo para usar GITHUB_TOKEN de forma segura (hace falta ≥ 2.31). Actualiza git o clona a mano y ejecuta ./install.sh."
    fi
    local hdr; hdr="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$tok" | base64 -w0)"
    export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="$hdr"
  fi
  [ -t 0 ] || export GIT_TERMINAL_PROMPT=0
  # Si la descarga se atasca (<1 KB/s durante 60 s) falla con un error en vez de esperar sin fin.
  export GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=60
}

fetch_code() {
  step "Código desde GitHub ($REPO_URL${BRANCH:+, rama $BRANCH})"
  need_cmd git || die "git no está instalado."
  git_env
  mkdir -p "$(dirname "$INSTALL_DIR")"
  if [ -d "$INSTALL_DIR/.git" ]; then
    if [ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]; then
      warn "Hay cambios locales en $INSTALL_DIR: no se actualiza el código (se conservan)."
    else
      local br="${BRANCH:-$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD)}"
      live git -C "$INSTALL_DIR" fetch --depth 1 --progress origin "$br" || die "No se pudo leer la rama '$br'. ¿Existe? ¿Falta GITHUB_TOKEN (repo privado)?"
      run git -C "$INSTALL_DIR" checkout -q -B "$br" FETCH_HEAD
      ok "Actualizado a $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
    fi
  elif [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    die "$INSTALL_DIR existe y no es una instalación de clipper (usa --dir otra_ruta)."
  else
    local clone=(clone --depth 1 --progress)
    [ -n "$BRANCH" ] && clone+=(--branch "$BRANCH")
    live git "${clone[@]}" "$REPO_URL" "$INSTALL_DIR" \
      || die "No se pudo clonar. El repositorio es privado: export GITHUB_TOKEN=... (permiso de lectura) y vuelve a ejecutar. Si la rama no existe, usa --branch."
    ok "Clonado en $INSTALL_DIR"
  fi
}

# ----------------------------------------------------------------------------- python / venv
pick_python() {
  PY=""
  local cands=()
  if [ -n "$PYTHON_OVERRIDE" ]; then cands=("$PYTHON_OVERRIDE"); else cands=(python3.12 python3.11 python3 python3.13); fi
  local c v maj min
  for c in "${cands[@]}"; do
    need_cmd "$c" || continue
    v="$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || continue
    maj="${v%%.*}"; min="${v##*.}"
    if [ "$maj" -eq 3 ] && [ "$min" -ge 11 ]; then PY="$(command -v "$c")"; PYV="$v"; break; fi
  done
  [ -n "$PY" ] || die "Hace falta Python 3.11 o superior (recomendado 3.12). Instálalo o usa --python RUTA."
  [ "$PYV" = "3.13" ] && warn "Python 3.13: algunas dependencias de IA pueden no tener paquetes aún; se verifica al final. Recomendado 3.12."
  ok "Python $PYV ($PY)"
}

make_venv() {
  step "Entorno virtual"
  VENV="$APP_DIR/.venv"; VPY="$VENV/bin/python"
  if [ -x "$VPY" ]; then
    local cur; cur="$("$VPY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
    if [ "$cur" != "$PYV" ]; then warn "El entorno actual usa Python $cur; se recrea con $PYV."; run rm -rf "$VENV"; fi
  fi
  if [ ! -x "$VPY" ]; then
    if ! run "$PY" -m venv "$VENV"; then
      if [ "$PM" = apt ]; then
        warn "Falta el paquete venv de Python $PYV; intentando instalarlo."
        as_root apt-get install -y "python${PYV}-venv"
        run rm -rf "$VENV"; run "$PY" -m venv "$VENV"
      else die "No se pudo crear el entorno virtual."; fi
    fi
  fi
  ok "$VENV"
}

has_nvidia() {
  need_cmd nvidia-smi || return 1
  local out; out="$(nvidia-smi -L 2>/dev/null || true)"
  [[ "$out" == *GPU* ]]
}

install_python_deps() {
  step "Dependencias de Python (puede tardar varios minutos)"
  VPIP=("$VPY" -m pip)
  run "${VPIP[@]}" install --upgrade pip wheel
  # torch solo se usa para silero-vad: la versión CPU basta (~200 MB en vez de ~5 GB con CUDA).
  if [ "$DRY" = 1 ] || ! "$VPY" -c 'import torch' >/dev/null 2>&1; then
    if ! RUN_QUIET=1 run "${VPIP[@]}" install torch --index-url https://download.pytorch.org/whl/cpu; then
      warn "No se pudo usar el índice CPU de PyTorch; se usa el de PyPI (más grande)."
      run "${VPIP[@]}" install torch
    fi
  fi
  run "${VPIP[@]}" install -r "$APP_DIR/requirements.txt"
  # YouTube/Twitch/Kick cambian a menudo: estas dos herramientas se actualizan siempre a la última versión
  if ! run "${VPIP[@]}" install --upgrade yt-dlp streamlink; then warn "No se pudieron actualizar yt-dlp/streamlink (se usa la versión instalada)."; fi
  run "${VPIP[@]}" install --no-deps -e "$APP_DIR"
  ok "Paquetes instalados"

  local want_gpu=0
  case "$GPU" in
    yes) want_gpu=1 ;;
    auto) has_nvidia && ask "GPU NVIDIA detectada ($(nvidia-smi -L | head -1 | cut -c1-60)). ¿Instalar librerías CUDA para Whisper (~1 GB)?" && want_gpu=1 ;;
  esac
  if [ "$want_gpu" = 1 ]; then
    run "${VPIP[@]}" install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" && ok "Librerías CUDA 12 / cuDNN 9 para Whisper"
  else
    ok "Modo CPU (puedes cambiarlo en el panel: Auto / GPU / Solo CPU)"
  fi
}

# ----------------------------------------------------------------------------- lanzador, ajustes, modelos
write_launcher() {
  step "Comando «clipper»"
  LAUNCHER="$BIN_DIR/clipper"
  run mkdir -p "$BIN_DIR"
  if [ "$DRY" = 1 ]; then echo "  [dry-run] escribir $LAUNCHER"; return 0; fi
  cat >"$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Generado por install.sh — no editar (se regenera al reinstalar).
APP="$APP_DIR"
VENV="\$APP/.venv"
# Librerías CUDA instaladas con pip (si existen) para faster-whisper en GPU
for d in "\$VENV"/lib/python*/site-packages/nvidia/cublas/lib "\$VENV"/lib/python*/site-packages/nvidia/cudnn/lib; do
  [ -d "\$d" ] && LD_LIBRARY_PATH="\$d:\${LD_LIBRARY_PATH:-}"
done
export LD_LIBRARY_PATH
exec "\$VENV/bin/python" -m clipper "\$@"
EOF
  chmod +x "$LAUNCHER"
  ok "$LAUNCHER"
  case ":$PATH:" in *":$BIN_DIR:"*) ;; *) warn "$BIN_DIR no está en tu PATH. Añade a ~/.bashrc:  export PATH=\"$BIN_DIR:\$PATH\"" ;; esac
}

put_setting() {   # put_setting SECCION CLAVE VALOR  (VALOR en YAML: 0.0.0.0, 7848, true…)
  [ "$DRY" = 1 ] && { echo "  [dry-run] $1.$2=$3 en settings.local.yaml"; return 0; }
  "$VPY" - "$APP_DIR/settings.local.yaml" "$1" "$2" "$3" <<'PY'
import os, sys, yaml
path, sec, key, val = sys.argv[1:5]
d = (yaml.safe_load(open(path)) if os.path.exists(path) else None) or {}
d.setdefault(sec, {})[key] = yaml.safe_load(val)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    yaml.safe_dump(d, f, allow_unicode=True, sort_keys=False)
PY
}

setup_lan() {
  [ "$LAN" = 1 ] || return 0
  step "Acceso desde la red (con contraseña)"
  put_setting server host 0.0.0.0
  ok "El panel escuchará en toda la red local (puerto ${PORT:-7848})"
  if [ "$DRY" = 1 ]; then echo "  [dry-run] clipper set-password"; return 0; fi
  local pw="${CLIPPER_PASSWORD:-}"
  if [ -z "$pw" ] && grep -q "server.password_hash" "$APP_DIR/secrets.local.yaml" 2>/dev/null; then
    ok "Ya hay una contraseña configurada (se conserva; cámbiala con: clipper set-password)"; return 0
  fi
  if [ -z "$pw" ]; then
    if [ -r /dev/tty ] && [ -w /dev/tty ]; then
      local p1="" p2=""
      while :; do
        printf '  Elige una contraseña para el panel (mín. 8 caracteres): ' >/dev/tty; read -rs p1 </dev/tty; printf '\n' >/dev/tty
        printf '  Repítela: ' >/dev/tty; read -rs p2 </dev/tty; printf '\n' >/dev/tty
        if [ "${#p1}" -lt 8 ]; then printf '  ! Demasiado corta.\n' >/dev/tty
        elif [ "$p1" != "$p2" ]; then printf '  ! No coinciden.\n' >/dev/tty
        else pw="$p1"; break; fi
      done
    else
      die "Sin terminal para pedir la contraseña. Ejecuta con CLIPPER_PASSWORD='tu-contraseña' antes del comando."
    fi
  fi
  printf '%s\n' "$pw" | "$LAUNCHER" set-password --stdin >>"$LOG" 2>&1 || die "No se pudo guardar la contraseña (¿mínimo 8 caracteres?)."
  ok "Contraseña guardada"
}

set_port() {
  [ -n "$PORT" ] || return 0
  case "$PORT" in ''|*[!0-9]*) die "--port debe ser un número" ;; esac
  { [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ]; } || die "--port debe estar entre 1024 y 65535"
  [ "$DRY" = 1 ] && { echo "  [dry-run] puerto $PORT en settings.local.yaml"; return 0; }
  "$VPY" - "$APP_DIR/settings.local.yaml" "$PORT" <<'PY'
import os, sys, yaml
path, port = sys.argv[1], int(sys.argv[2])
d = (yaml.safe_load(open(path)) if os.path.exists(path) else None) or {}
d.setdefault("server", {})["port"] = port
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    yaml.safe_dump(d, f, allow_unicode=True, sort_keys=False)
PY
  ok "Puerto del panel: $PORT"
}

download_models() {
  [ "$NO_MODELS" = 1 ] && { warn "Modelos omitidos (--no-models): descárgalos luego con scripts/download_models.sh y «clipper download-model small»."; return 0; }
  step "Modelos (única fase que necesita red tras instalar; después todo funciona sin conexión)"
  mkdir -p "$APP_DIR/models"
  if [ ! -s "$APP_DIR/models/blaze_face_short_range.tflite" ]; then
    run bash "$APP_DIR/scripts/download_models.sh" && ok "Detector de rostros (MediaPipe)"
  else ok "Detector de rostros ya descargado"; fi
  if [ "$NO_WHISPER" = 1 ]; then warn "Whisper omitido (--no-whisper)."; return 0; fi
  case "$WHISPER" in *[!A-Za-z0-9._-]*|"") die "Nombre de modelo inválido: $WHISPER" ;; esac
  if [ -d "$APP_DIR/models/whisper-$WHISPER" ]; then ok "Whisper '$WHISPER' ya descargado"
  else
    if run "$LAUNCHER" download-model "$WHISPER"; then ok "Whisper '$WHISPER'"
    else warn "No se pudo descargar Whisper ahora. Reintenta: clipper download-model $WHISPER"; fi
  fi
}

current_port() {
  if [ -n "$PORT" ]; then echo "$PORT"; return; fi
  "$VPY" -c 'from clipper.config import load_config; print(load_config().server.port)' 2>/dev/null || echo 7848
}

# ¿Es un proceso de clipper (de esta o de otra instalación anterior)?
is_clipper_proc() {
  case "$1" in *"$APP_DIR"*|*"-m clipper"*|*"/clipper serve"*|*"clipper/.venv"*) return 0 ;; esac
  return 1
}

# PIDs que escuchan en el puerto $1. Lee /proc con Python (no depende de ss/lsof/fuser, que pueden no estar instalados).
port_pids() {
  "${VPY:-python3}" - "$1" <<'PY' 2>/dev/null || true
import os, sys
port = int(sys.argv[1])
inodes = set()
for f in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        rows = open(f).read().splitlines()[1:]
    except OSError:
        continue
    for r in rows:
        c = r.split()
        if len(c) > 9 and c[3] == "0A" and int(c[1].rsplit(":", 1)[1], 16) == port:
            inodes.add(c[9])
found = set()
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                t = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if t.startswith("socket:[") and t[8:-1] in inodes:
                found.add(int(pid))
    except OSError:
        continue
print("\n".join(str(p) for p in sorted(found)))
PY
}
proc_cmd() { tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null || true; }

stop_pid() {   # SIGTERM, y SIGKILL si en 5 s no ha salido
  local pid="$1" n=0
  kill "$pid" 2>/dev/null || return 0
  while kill -0 "$pid" 2>/dev/null && [ "$n" -lt 10 ]; do sleep 0.5; n=$((n + 1)); done
  if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid" 2>/dev/null || true; fi
}

confirm_tty() {   # sí/no explícito (por defecto NO); nunca se asume con --yes
  [ "$YES" = 1 ] && return 1
  [ -r /dev/tty ] && [ -w /dev/tty ] || return 1
  local a; printf '%s [s/N] ' "$1" >/dev/tty; read -r a </dev/tty || a=""
  case "${a:-n}" in [sSyY]*) return 0 ;; *) return 1 ;; esac
}

# Deja libre el puerto del panel antes de arrancar el servicio:
#  - Servicios y procesos de clipper anteriores (aunque sean de otra carpeta): se paran solos.
#  - Un programa AJENO que ocupe el puerto no se mata a ciegas: se pregunta (por defecto no) y, si no, se usa otro puerto libre.
free_port() {
  [ "$DRY" = 1 ] && return 0
  local port pid cmd old rest p
  port="$(current_port)"
  if need_cmd systemctl; then
    if [ -f /etc/systemd/system/clipper.service ]; then
      systemctl stop clipper.service >>"$LOG" 2>&1 || true     # parado, para que no reaparezca mientras se instala
    fi
    old="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/clipper.service"
    if [ -f "$old" ]; then
      systemctl --user disable --now clipper.service >>"$LOG" 2>&1 || true
      rm -f "$old"; systemctl --user daemon-reload >>"$LOG" 2>&1 || true
      warn "Se retiró un servicio de usuario antiguo de clipper (solo escuchaba en localhost)."
    fi
  fi
  for pid in $(port_pids "$port"); do
    cmd="$(proc_cmd "$pid")"
    if is_clipper_proc "$cmd"; then
      stop_pid "$pid"; warn "Se detuvo una instancia anterior de clipper (PID $pid) que ocupaba el puerto $port."
    fi
  done
  rest="$(port_pids "$port")"
  [ -n "$rest" ] || return 0
  pid="$(echo "$rest" | head -n 1)"; cmd="$(proc_cmd "$pid")"
  if confirm_tty "El puerto $port lo ocupa otro programa (PID $pid: ${cmd:-desconocido}). ¿Detenerlo?"; then
    stop_pid "$pid"; ok "Programa detenido (PID $pid)"
    return 0
  fi
  for p in $(seq $((port + 1)) $((port + 30))); do
    if [ -z "$(port_pids "$p")" ]; then
      warn "El puerto $port está ocupado por otro programa (PID $pid: ${cmd:-desconocido}): se usará el $p."
      PORT="$p"; put_setting server port "$p"
      return 0
    fi
  done
  die "No se encontró ningún puerto libre entre $port y $((port + 30))."
}

install_service() {
  [ "$SERVICE" = 1 ] || return 0
  step "Servicio systemd"
  need_cmd systemctl || { warn "systemd no disponible: servicio omitido."; return 0; }
  [ "$DRY" = 1 ] && { echo "  [dry-run] instalar clipper.service"; return 0; }
  free_port
  if [ "$(id -u)" -eq 0 ]; then
    cat >/etc/systemd/system/clipper.service <<EOF
[Unit]
Description=clipper (clips verticales desde directos)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$LAUNCHER serve
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    if systemctl daemon-reload >>"$LOG" 2>&1 && systemctl enable clipper.service >>"$LOG" 2>&1 && systemctl restart clipper.service >>"$LOG" 2>&1; then
      SERVICE_OK=1; ok "Servicio del sistema activo y con arranque automático: systemctl status clipper"
    else
      warn "No se pudo activar el servicio (ver $LOG). Archivo en /etc/systemd/system/clipper.service"
    fi
    return 0
  fi
  local dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$dir"
  cat >"$dir/clipper.service" <<EOF
[Unit]
Description=clipper (clips verticales desde directos)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$LAUNCHER serve
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  if systemctl --user daemon-reload >>"$LOG" 2>&1 && systemctl --user enable clipper.service >>"$LOG" 2>&1 && systemctl --user restart clipper.service >>"$LOG" 2>&1; then
    SERVICE_OK=1; ok "Servicio activo: systemctl --user status clipper"
    warn "Para que arranque con el equipo sin abrir sesión: sudo loginctl enable-linger $USER"
  else
    warn "No se pudo activar el servicio (¿sin sesión systemd de usuario?). Archivo creado en $dir/clipper.service"
  fi
}

# Espera a que el panel responda de verdad (no basta con que systemd diga "activo").
wait_ready() {
  [ "$SERVICE_OK" = 1 ] || return 0
  [ "$DRY" = 1 ] && return 0
  step "Esperando a que el panel responda"
  local port code
  port="${PORT:-$("$VPY" -c 'from clipper.config import load_config; print(load_config().server.port)' 2>/dev/null || echo 7848)}"
  for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$port/login" 2>/dev/null || true)"
    case "$code" in 200|301|302|307|401) ok "El panel responde en el puerto $port"; return 0 ;; esac
    sleep 2
  done
  warn "El servicio está activo pero el panel NO responde en el puerto $port tras 2 minutos."
  warn "Mira los motivos con:  journalctl -u clipper -n 60 --no-pager"
  ss -ltnp 2>/dev/null | grep -E "[:.]${port}[[:space:]]" >&2 || true
  systemctl status clipper --no-pager -l 2>&1 | tail -n 15 >&2 || true
  journalctl -u clipper -n 25 --no-pager 2>&1 | tail -n 25 >&2 || true
}

# ----------------------------------------------------------------------------- verificación
verify() {
  [ "$NO_VERIFY" = 1 ] && return 0
  [ "$DRY" = 1 ] && return 0
  step "Verificación"
  "$VPY" - >>"$LOG" 2>&1 <<'PY' || die "Fallo al importar las dependencias (ver registro)."
import cv2, numpy, yaml, pydantic, fastapi, uvicorn, httpx, faster_whisper, silero_vad, mediapipe, clipper.engine  # noqa
PY
  ok "Dependencias importan correctamente"
  "$LAUNCHER" check | tr -d '\n ' | sed 's/^/  /'; echo
  local tmp; tmp="$(mktemp -d)"
  ffmpeg -v error -y -f lavfi -i "testsrc2=s=1280x720:r=25:d=3" -f lavfi -i "sine=f=300:d=3" -c:v libx264 -preset ultrafast -c:a aac -shortest "$tmp/in.mp4" >>"$LOG" 2>&1
  if "$LAUNCHER" reframe "$tmp/in.mp4" -o "$tmp/out.mp4" >>"$LOG" 2>&1 \
     && [ "$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 "$tmp/out.mp4")" = "1080,1920" ]; then
    ok "Prueba de render 9:16 correcta (1080x1920)"
  else
    tail -n 20 "$LOG" >&2; die "La prueba de render falló."
  fi
  rm -rf "$tmp"
  # Autodiagnóstico: render de cada diseño y camino completo directo -> clip con SU ffmpeg
  local st; st="$("$LAUNCHER" selftest 2>&1 || true)"
  printf '%s\n' "$st" >>"$LOG"
  if printf '%s' "$st" | grep -q "todo correcto"; then ok "Autodiagnóstico (clipper selftest): todo correcto"
  else
    warn "El autodiagnóstico encontró problemas. Resultado:"
    printf '%s\n' "$st" | grep -E "FALLA|AVISO|Resultado" | sed 's/^/      /' >&2
    warn "Envía la salida completa de:  clipper selftest"
  fi
}

# ----------------------------------------------------------------------------- desinstalar
uninstall() {
  step "Desinstalando clipper de $APP_DIR"
  local l="$BIN_DIR/clipper"
  if [ -f "$l" ] && grep -q "APP=\"$APP_DIR\"" "$l" 2>/dev/null; then run rm -f "$l"; ok "Comando eliminado"; fi
  if need_cmd systemctl && [ -f /etc/systemd/system/clipper.service ]; then
    systemctl disable --now clipper.service >>"$LOG" 2>&1 || true
    run rm -f /etc/systemd/system/clipper.service; ok "Servicio del sistema eliminado"
  fi
  if need_cmd systemctl && [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/clipper.service" ]; then
    systemctl --user disable --now clipper.service >>"$LOG" 2>&1 || true
    run rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/clipper.service"; ok "Servicio eliminado"
  fi
  run rm -rf "$APP_DIR/.venv"; ok "Entorno virtual eliminado"
  if [ "$PURGE" = 1 ]; then
    run rm -rf "$APP_DIR/models" "$APP_DIR/data"
    run rm -f "$APP_DIR/settings.local.yaml" "$APP_DIR/secrets.local.yaml" "$APP_DIR/tokens.local.json"
    ok "Modelos, ajustes, secretos, tokens y cola de publicación eliminados"
  fi
  ok "Se conservan tus clips (output_clips/) y el código en $APP_DIR"
}

summary() {
  local port; port="${PORT:-$("$VPY" -c 'from clipper.config import load_config; print(load_config().server.port)' 2>/dev/null || echo 7848)}"
  local url="http://localhost:$port" ip
  if [ "$LAN" = 1 ]; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    url="http://${ip:-IP_DEL_SERVIDOR}:$port"
  fi
  local start="clipper serve"; [ "$SERVICE_OK" = 1 ] && start="ya está en marcha (systemctl status clipper)"
  cat <<EOF

${G}${B}✔ clipper instalado${N}   (registro: $LOG)

  Panel (abre esto en tu navegador) :  $url
  Arrancar el panel :  $start
  Sin web (terminal):  clipper run
  Ajustes           :  panel → ⚙️ Ajustes   (o edita $APP_DIR/config.yaml)
  Actualizar        :  vuelve a ejecutar el instalador
  Desinstalar       :  bash $APP_DIR/install.sh --uninstall [--purge]
EOF
}

# ----------------------------------------------------------------------------- flujo principal
# Todo el flujo va dentro de main(): así bash lee el script ENTERO antes de ejecutar nada (importante con «curl | bash»,
# si no, curl puede dar «Failure writing output» cuando el script salta a su segunda fase).
main() {
  detect_os

  # ¿Estamos dentro de un clon (ejecutado como archivo) o por «curl | bash»?
  SELF="${BASH_SOURCE[0]:-}"
  if [ -n "$SELF" ] && [ -f "$SELF" ] && [ -f "$(dirname "$SELF")/clipper/__init__.py" ]; then
    APP_DIR="$(cd "$(dirname "$SELF")" && pwd)"
    FROM_CLONE=1
  else
    APP_DIR="$INSTALL_DIR"
    FROM_CLONE=0
  fi

  if [ "$UNINSTALL" = 1 ]; then uninstall; exit 0; fi

  printf '%sclipper — instalador%s  (%s)\n' "$B" "$N" "$OS_NAME"
  printf '  Registro detallado: %s   (puedes seguirlo con: tail -f %s)\n' "$LOG" "$LOG"
  [ "$DRY" = 1 ] && warn "Modo simulación: no se cambia nada."

  if [ "$FROM_CLONE" = 0 ]; then
    # Hace falta git (y curl) antes de poder clonar
    if ! need_cmd git; then
      step "Instalando git"
      case "$PM" in
        apt) as_root apt-get update; as_root env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y git ca-certificates ;;
        dnf) as_root dnf install -y git ;;
        pacman) as_root pacman -S --needed --noconfirm git ;;
        *) die "Instala git y vuelve a ejecutar." ;;
      esac
    fi
    fetch_code
    # Continúa con el instalador del repo descargado (misma versión que el código)
    if [ "$DRY" != 1 ]; then
      args=(--dir "$INSTALL_DIR" --repo "$REPO_URL" --whisper "$WHISPER" --bin-dir "$BIN_DIR")
      [ -n "$BRANCH" ] && args+=(--branch "$BRANCH")
      [ "$GPU" = yes ] && args+=(--gpu)
      [ "$GPU" = no ] && args+=(--cpu)
      [ -n "$PORT" ] && args+=(--port "$PORT")
      [ -n "$PYTHON_OVERRIDE" ] && args+=(--python "$PYTHON_OVERRIDE")
      [ "$YES" = 1 ] && args+=(--yes)
      [ "$NO_MODELS" = 1 ] && args+=(--no-models)
      [ "$NO_WHISPER" = 1 ] && args+=(--no-whisper)
      [ "$SERVICE" = 1 ] && args+=(--service)
      [ "$LAN" = 1 ] && args+=(--lan)
      [ "$NO_VERIFY" = 1 ] && args+=(--no-verify)
      exec bash "$INSTALL_DIR/install.sh" "${args[@]}" </dev/null
    fi
    exit 0
  fi

  step "Comprobaciones previas"
  avail_kb="$(df -Pk "$APP_DIR" | awk 'NR==2{print $4}')"
  [ "${avail_kb:-0}" -ge 4194304 ] || warn "Poco espacio libre ($((avail_kb / 1024)) MB). Se recomiendan ≥ 4 GB (modelos + dependencias + clips)."
  [ "$(id -u)" -eq 0 ] && warn "Ejecutando como root: el entorno y el comando quedarán a nombre de root (mejor usa un usuario normal)."
  ok "Instalación en: $APP_DIR"

  install_system_deps
  pick_python
  make_venv
  install_python_deps
  write_launcher
  set_port
  setup_lan
  download_models
  install_service
  verify
  wait_ready
  [ "$DRY" = 1 ] || summary
}

main "$@"
