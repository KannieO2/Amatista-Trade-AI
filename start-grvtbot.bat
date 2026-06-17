@echo off
chcp 65001 >nul
title GRVTBot (real)
cd /d "%~dp0external\GRVTBot"

if not exist "node_modules" (
  echo ============================================
  echo  Primera vez GRVTBot: instalando deps. 1-3 min.
  echo ============================================
  call npm install
)
if not exist "packages\bot\dist\dashboard\server.js" (
  echo  Compilando GRVTBot...
  call npm run build
)
if not exist "master.key" (
  echo  Generando master.key...
  node -e "require('fs').writeFileSync('master.key', require('crypto').randomBytes(32))"
)
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo MOCK_MODE=true>> .env
  echo DRY_RUN=true>> .env
  echo ALLOW_EMBED=1>> .env
  echo BOT_PORT=3848>> .env
  echo MASTER_KEY_PATH=%CD%\master.key>> .env
  echo DASHBOARD_V2_DIST=%CD%\packages\dashboard\dist>> .env
  echo GRVT_TRADING_ACCOUNT_ID=mock-account>> .env
  echo  .env creado en modo MOCK. Para dinero real: pon tus llaves GRVT en external\GRVTBot\.env y quita MOCK_MODE.
)

echo.
echo  GRVTBot en:  http://localhost:3848/dashboard/
echo  (se ve embebido dentro de la pagina principal, seccion Grid Trading)
echo.
node packages\bot\dist\dashboard\server.js
pause
