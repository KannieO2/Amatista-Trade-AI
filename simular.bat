@echo off
chcp 65001 >nul
title Simulador Amatista - Monte Carlo REAL
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if "%SIM_N%"=="" set SIM_N=200000

echo ============================================================
echo   SIMULADOR AMATISTA  -  Monte Carlo REAL (no humo)
echo ------------------------------------------------------------
echo   PART A: tu edge REAL medido (exits de Supabase + IC 95%%)
echo   PART B: forward sim liquidity-aware (params actuales vs fix)
echo   PART C: matematica de rentabilidad + comparacion con video
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo  [!] Falta el entorno. Corre primero start.bat en esta carpeta.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" tools\simulate_real.py
echo.
echo ============================================================
echo   Listo. Part A = REAL (confiable). Part B = delta confiable.
echo ============================================================
pause
