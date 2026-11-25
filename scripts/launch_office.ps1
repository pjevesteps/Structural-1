param(
    [switch]$Stop,
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $RepoRoot ".structural_ai_pids.json"
$LogDir = Join-Path $RepoRoot "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Get-ListeningPid([int]$Port) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
    } catch {}
    return $null
}

function Get-LocalLanIp {
    try {
        $ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -notlike "127.*" -and
                $_.IPAddress -notlike "169.254.*" -and
                $_.InterfaceAlias -notlike "*Loopback*" -and
                $_.InterfaceAlias -notlike "*vEthernet*"
            } |
            Sort-Object InterfaceMetric |
            Select-Object -First 1 -ExpandProperty IPAddress
        if ($ip) { return $ip }
    } catch {}
    return "127.0.0.1"
}

function Stop-StructuralOffice {
    Write-Host "Stopping Structural AI Office servers..." -ForegroundColor Cyan
    if (Test-Path $PidFile) {
        try {
            $state = Get-Content $PidFile -Raw | ConvertFrom-Json
            foreach ($p in @($state.backend_pid, $state.frontend_pid)) {
                if ($p) {
                    $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
                    if ($proc) {
                        Write-Host "  Terminating PID $p ($($proc.ProcessName))..." -ForegroundColor Yellow
                        Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
                    }
                }
            }
            # Also clean up child listeners on recorded ports
            foreach ($pt in @($state.backend_port, $state.frontend_port)) {
                if ($pt) {
                    $lpid = Get-ListeningPid ([int]$pt)
                    if ($lpid) {
                        Stop-Process -Id $lpid -Force -ErrorAction SilentlyContinue
                    }
                }
            }
        } catch {
            Write-Warning "Could not parse PID file cleanly: $_"
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    } else {
        foreach ($pt in @($BackendPort, $FrontendPort)) {
            $lpid = Get-ListeningPid $pt
            if ($lpid) {
                $proc = Get-Process -Id $lpid -ErrorAction SilentlyContinue
                if ($proc -and ($proc.ProcessName -match "python|node")) {
                    Write-Host "  Stopping $($proc.ProcessName) (PID $lpid) on port $pt..." -ForegroundColor Yellow
                    Stop-Process -Id $lpid -Force -ErrorAction SilentlyContinue
                }
            }
        }
    }
    Write-Host "Structural AI Office stopped." -ForegroundColor Green
}

if ($Stop) {
    Stop-StructuralOffice
    exit 0
}

function Find-FreePort([int]$PreferredPort) {
    $port = $PreferredPort
    for ($i = 0; $i -lt 10; $i++) {
        $owner = Get-ListeningPid $port
        if (-not $owner) { return $port }
        $proc = Get-Process -Id $owner -ErrorAction SilentlyContinue
        if ($proc -and ($proc.ProcessName -match "python|node") -and (Test-Path $PidFile)) {
            $prev = Get-Content $PidFile -Raw | ConvertFrom-Json
            if ($owner -eq $prev.backend_pid -or $owner -eq $prev.frontend_pid -or $port -eq $prev.backend_port -or $port -eq $prev.frontend_port) {
                Write-Host "Stopping stale Structural AI process (PID $owner) on port $port..." -ForegroundColor Yellow
                Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
                Start-Sleep -Milliseconds 600
                if (-not (Get-ListeningPid $port)) { return $port }
            }
        }
        Write-Host "Port $port is in use by another application (PID $owner); trying fallback port..." -ForegroundColor Yellow
        $port++
    }
    throw "Could not find a free port starting at $PreferredPort."
}

$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Error "Virtual environment not found at $VenvPython. Create .venv and install backend\requirements.txt first."
    exit 1
}

$ActiveBackendPort = Find-FreePort $BackendPort
$ActiveFrontendPort = Find-FreePort $FrontendPort

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  STRUCTURAL AI OFFICE - LOCAL ENGINEERING WORKSPACE" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Starting FastAPI backend on port $ActiveBackendPort..." -ForegroundColor Green

$BackendCmd = "`$Host.UI.RawUI.WindowTitle='Structural AI Backend (:$ActiveBackendPort)'; Set-Location '$RepoRoot'; & '$VenvPython' -m uvicorn backend.app.main:app --reload --host 0.0.0.0 --port $ActiveBackendPort"
$BackendProc = Start-Process -FilePath "powershell.exe" -ArgumentList "-NoExit", "-NoProfile", "-Command", $BackendCmd -WorkingDirectory $RepoRoot -PassThru

Write-Host "Starting Vite frontend on port $ActiveFrontendPort (proxying /api -> 127.0.0.1:$ActiveBackendPort)..." -ForegroundColor Green
$FrontendCmd = "`$Host.UI.RawUI.WindowTitle='Structural AI Frontend (:$ActiveFrontendPort)'; Set-Location '$RepoRoot'; `$env:VITE_API_PROXY_TARGET='http://127.0.0.1:$ActiveBackendPort'; npm --prefix frontend run dev -- --host 0.0.0.0 --port $ActiveFrontendPort"
$FrontendProc = Start-Process -FilePath "powershell.exe" -ArgumentList "-NoExit", "-NoProfile", "-Command", $FrontendCmd -WorkingDirectory $RepoRoot -PassThru

@{
    backend_pid   = $BackendProc.Id
    frontend_pid  = $FrontendProc.Id
    backend_port  = $ActiveBackendPort
    frontend_port = $ActiveFrontendPort
    started_at    = (Get-Date).ToString("o")
} | ConvertTo-Json | Set-Content -Path $PidFile -Encoding UTF8

function Wait-Http200([string]$Url, [int]$TimeoutSeconds = 45) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    return $false
}

Write-Host "Waiting for FastAPI (/docs) and Vite frontend to respond HTTP 200..." -ForegroundColor Yellow
$BackReady = Wait-Http200 "http://127.0.0.1:$ActiveBackendPort/docs" 45
$FrontReady = Wait-Http200 "http://127.0.0.1:$ActiveFrontendPort" 45

$LanIp = Get-LocalLanIp
Write-Host ""
if ($BackReady -and $FrontReady) {
    Write-Host "Both servers are ONLINE!" -ForegroundColor Green
    Start-Process "http://127.0.0.1:$ActiveFrontendPort"
} else {
    Write-Warning "Timed out waiting for one of the servers (Backend=$BackReady, Frontend=$FrontReady). Check the console windows."
}

Write-Host "  Local Workspace : http://127.0.0.1:$ActiveFrontendPort" -ForegroundColor White
Write-Host "  LAN Workspace   : http://${LanIp}:$ActiveFrontendPort" -ForegroundColor Cyan
Write-Host "  FastAPI Docs    : http://127.0.0.1:$ActiveBackendPort/docs" -ForegroundColor Gray
Write-Host ""
Write-Host "To stop both servers cleanly, double-click Stop_Structural_AI.bat" -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Cyan
