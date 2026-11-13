# clipper — instalador para Windows (usa Docker Desktop con WSL2; con GPU NVIDIA si la hay).
#
# En PowerShell:
#   irm https://raw.githubusercontent.com/DXZ-dev/clips/claude/determined-johnson-p5vd5b/install.ps1 | iex
#
# Opciones por variables de entorno (antes de ejecutar):
#   $env:CLIPPER_DIR = "D:\clipper"        carpeta de instalación (def. %USERPROFILE%\clipper)
#   $env:CLIPPER_BRANCH = "main"           rama de GitHub
#   $env:CLIPPER_CPU = "1"                 no usar la GPU aunque exista
#   $env:CLIPPER_NO_START = "1"            solo descargar y construir, sin arrancar
# Es idempotente: volver a ejecutarlo actualiza el código y reconstruye (tus clips y ajustes se conservan).
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Dir    = if ($env:CLIPPER_DIR) { $env:CLIPPER_DIR } else { Join-Path $env:USERPROFILE "clipper" }
$Branch = if ($env:CLIPPER_BRANCH) { $env:CLIPPER_BRANCH } else { "claude/determined-johnson-p5vd5b" }
$Repo   = "DXZ-dev/clips"
$Port   = 7848

function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  !   $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host ""; Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

Write-Host "clipper - instalador para Windows" -ForegroundColor White

# 1) Docker Desktop
Step "Comprobando Docker Desktop"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Warn "Docker no esta instalado."
    Write-Host "  Instalalo con:  winget install -e --id Docker.DockerDesktop"
    Write-Host "  Reinicia el equipo, abre Docker Desktop (acepta WSL2) y vuelve a ejecutar este comando."
    exit 1
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Die "Docker esta instalado pero no esta en marcha. Abre Docker Desktop, espera a que indique 'Engine running' y vuelve a ejecutar." }
Ok ("Docker " + (& docker --version))

# 2) Descargar el codigo (ZIP publico de GitHub; no hace falta git)
Step "Descargando clipper ($Repo, rama $Branch)"
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("clipper_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
$zip = Join-Path $tmp "src.zip"
try {
    Invoke-WebRequest -Uri "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip -UseBasicParsing
} catch {
    Die "No se pudo descargar el codigo. Comprueba la conexion y que la rama '$Branch' exista y el repositorio sea publico."
}
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$src = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
if (-not $src) { Die "El ZIP descargado esta vacio." }
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
# Copia sobre la instalacion existente SIN tocar tus clips (output_clips) ni nada fuera del codigo
Copy-Item -Path (Join-Path $src.FullName "*") -Destination $Dir -Recurse -Force
Remove-Item -Recurse -Force $tmp
Ok "Codigo en $Dir"

# 3) GPU NVIDIA?
$files = @("-f", "docker-compose.yml")
$gpu = $false
if (-not $env:CLIPPER_CPU -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    & nvidia-smi -L *> $null
    if ($LASTEXITCODE -eq 0) { $gpu = $true; $files += @("-f", "docker-compose.gpu.yml") }
}
if ($gpu) { Ok "GPU NVIDIA detectada: Whisper usara CUDA" } else { Ok "Modo CPU (puedes cambiarlo luego en el panel)" }

# 4) Construir y arrancar
Step "Construyendo la imagen (la primera vez tarda varios minutos)"
Push-Location $Dir
try {
    & docker compose @files build
    if ($LASTEXITCODE -ne 0) { Die "Fallo al construir la imagen (revisa el mensaje de arriba)." }
    if ($env:CLIPPER_NO_START) { Ok "Imagen construida. Arranca con: docker compose up -d"; exit 0 }
    Step "Arrancando"
    & docker compose @files up -d
    if ($LASTEXITCODE -ne 0) { Die "No se pudo arrancar el contenedor." }
} finally { Pop-Location }

# 5) Esperar al panel
Step "Esperando al panel (la primera vez descarga el modelo de voz)"
$url = "http://localhost:$Port"
$ready = $false
for ($i = 0; $i -lt 150; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "$url/api/status" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ready) {
    Warn "El panel aun no responde. Mira los registros:  cd `"$Dir`"; docker compose logs -f"
    exit 1
}
Ok "Panel listo"
Start-Process $url

Write-Host ""
Write-Host "clipper instalado: $url" -ForegroundColor Green
Write-Host "  Tus clips:      $Dir\output_clips"
Write-Host "  Ver registros:  cd `"$Dir`"; docker compose logs -f"
Write-Host "  Parar / abrir:  docker compose stop   /   docker compose start"
Write-Host "  Actualizar:     vuelve a ejecutar el comando de instalacion"
