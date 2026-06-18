# run_bot.ps1 — Lanzador durable del bot TradeOS (recolección local 24/7).
# Úsalo en TU PROPIA ventana de PowerShell (déjala abierta). Si el bot se cae,
# este script lo relanza solo. Ctrl+C para detener del todo.
#
#   powershell -ExecutionPolicy Bypass -File tools\run_bot.ps1
#
# No toca estrategia/TP/SL/risk. Solo arranca el mismo bot (paper + recorder).

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path $py)) { Write-Host "No existe $py" -ForegroundColor Red; exit 1 }

function Stop-Stale {
    $c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    foreach ($x in $c) {
        try { Stop-Process -Id $x.OwningProcess -Force -ErrorAction SilentlyContinue } catch {}
    }
    Start-Sleep -Milliseconds 600
}

Write-Host "TradeOS · recolector local. Reinicio automatico ON. Ctrl+C para salir." -ForegroundColor Cyan
$n = 0
while ($true) {
    $n++
    Stop-Stale
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$ts] arranque #$n -> http://127.0.0.1:8000  (log: bot.log)" -ForegroundColor Green
    # Foreground: si el proceso termina (crash/cierre), el while lo relanza.
    & $py -m uvicorn app.main:app --app-dir "apps\pump-reader" --host 127.0.0.1 --port 8000 *>> "bot.log"
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$ts] el bot termino (codigo $LASTEXITCODE). Relanzando en 5s..." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
