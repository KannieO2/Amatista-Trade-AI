@echo off
chcp 65001 >nul
title Pump Reader (Amatista)
REM Lanzador individual del Pump Reader. Normalmente NO se usa solo:
REM el maestro start.bat (en la raiz) levanta este + el GRVTBot juntos.
cd /d "%~dp0"
echo  Pump Reader en: http://localhost:8000
"%~dp0..\..\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
