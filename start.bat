@echo off
chcp 65001 >nul
title Pump Reader
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo ============================================
  echo  Primera vez: instalando todo. Tarda 1-2 min.
  echo ============================================
  py -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r "apps\pump-reader\requirements.txt"
)

echo.
echo  Iniciando Pump Reader...
echo  Abre tu navegador en:  http://localhost:8000
echo  Para apagarlo: cierra esta ventana.
echo.

start "" http://localhost:8000
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --app-dir "apps\pump-reader"
pause
