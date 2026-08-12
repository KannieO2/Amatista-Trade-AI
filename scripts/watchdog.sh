#!/usr/bin/env bash
#
# Amatista TradeOS — vigilante de caídas.
#
# NO toca el bot. Corre aparte, mira desde afuera y avisa.
#
# CUBRE DOS FALLAS DISTINTAS, que necesitan mecanismos distintos:
#
#   1) El bot se cae pero el VPS vive
#      -> este script lo detecta y manda Telegram al toque.
#
#   2) El VPS entero se cae (o te suspenden la cuenta)
#      -> ningún script corriendo AHÍ puede avisarte: está muerto.
#         Por eso, cuando todo está sano, este script hace "ping" a un
#         servicio externo. Si los pings dejan de llegar, ese servicio te
#         alerta. Es un interruptor de hombre muerto: el silencio ES la
#         alarma. Sin esto, una caída del VPS pasa inadvertida.
#
# Servicios gratis para el ping: healthchecks.io, cronitor.io, betterstack.
# Creás un check con período 10min + gracia 5min y pegás la URL en HEARTBEAT_URL.
#
# Uso:
#   ./scripts/watchdog.sh
#
# Cron cada 5 minutos (crontab -e):
#   */5 * * * * HEARTBEAT_URL='https://hc-ping.com/xxxx' /ruta/repo/scripts/watchdog.sh >> /var/log/amatista-watchdog.log 2>&1
#
set -uo pipefail   # sin -e: un chequeo que falla NO debe abortar el script

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PUMP_URL="${PUMP_HEALTH_URL:-http://localhost:8000/health}"
GRVT_CONTAINER="${GRVTBOT_CONTAINER:-amatista-grvtbot}"
PUMP_CONTAINER="${PUMP_CONTAINER:-amatista-pump}"
HEARTBEAT_URL="${HEARTBEAT_URL:-}"
STATE_FILE="${WATCHDOG_STATE:-/tmp/.amatista-watchdog-state}"

# Credenciales de Telegram: reusa las del .env raíz, no duplica config.
if [ -f "$REPO_DIR/.env" ]; then
  TG_TOKEN="${TELEGRAM_BOT_TOKEN:-$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"'\r')}"
  TG_CHAT="${TELEGRAM_CHAT_ID:-$(grep -m1 '^TELEGRAM_CHAT_ID=' "$REPO_DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"'\r')}"
else
  TG_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
  TG_CHAT="${TELEGRAM_CHAT_ID:-}"
fi

log() { printf '%s  %s\n' "$(date -u '+%Y-%m-%d %H:%M:%SZ')" "$*"; }

# Escapa una cadena para usarla como valor JSON. El contenido lo generamos
# nosotros (nombres de container, estados de docker), pero se escapa igual.
# Las barras van en variable: escribirlas inline dentro de ${s//…/…} hacía
# que bash se comiera la barra y los saltos salían como una "n" suelta.
json_escape() {
  local s="$1" nl=$'\n' cr=$'\r' tab=$'\t' bs='\'
  s="${s//$bs/$bs$bs}"     # barras primero, o se re-escaparían las de abajo
  s="${s//\"/$bs\"}"
  s="${s//$nl/${bs}n}"
  s="${s//$cr/}"
  s="${s//$tab/${bs}t}"
  printf '%s' "$s"
}

# Se manda como JSON DESDE UN ARCHIVO, no como formulario ni como argumento.
# Con --data-urlencode, y también pasando el JSON inline, Telegram rechazaba
# los acentos y emojis con `400: text must be encoded in UTF-8`: el texto se
# reconvierte según el codepage antes de salir. Escrito a archivo y enviado
# con --data-binary @archivo, los bytes viajan intactos.
notify() {
  local msg="$1"
  if [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT" ]; then
    log "AVISO: sin credenciales de Telegram — mensaje no enviado: $msg"
    return
  fi
  local tmp
  tmp="$(mktemp)"
  printf '{"chat_id":"%s","parse_mode":"HTML","text":"%s"}' \
    "$(json_escape "$TG_CHAT")" "$(json_escape "$msg")" > "$tmp"
  if curl -fsS -m 15 -X POST \
      "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
      -H 'Content-Type: application/json; charset=utf-8' \
      --data-binary @"$tmp" >/dev/null 2>&1; then
    log "telegram enviado"
  else
    log "AVISO: no se pudo enviar el telegram"
  fi
  rm -f "$tmp"
}

PROBLEMS=()

# ── 1. Containers arriba ─────────────────────────────────────────────────
for c in "$GRVT_CONTAINER" "$PUMP_CONTAINER"; do
  state="$(docker inspect "$c" --format '{{.State.Status}}' 2>/dev/null)"
  if [ "$state" != "running" ]; then
    PROBLEMS+=("container <code>$c</code> no está corriendo (estado: ${state:-inexistente})")
    continue
  fi
  # Si el container declara healthcheck, respetarlo. 'unhealthy' significa
  # que el proceso vive pero no responde — se cuelga sin morir, que es el
  # caso que un simple "¿está el proceso?" no detecta.
  health="$(docker inspect "$c" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' 2>/dev/null)"
  if [ -n "$health" ] && [ "$health" != "healthy" ]; then
    PROBLEMS+=("container <code>$c</code> está <b>$health</b>")
  fi
done

# ── 2. El grid responde de verdad ────────────────────────────────────────
# Se consulta DENTRO del container: el puerto 3848 no se publica al host y
# el proxy /grid/* del pump-reader exige autenticación.
if docker inspect "$GRVT_CONTAINER" >/dev/null 2>&1; then
  gh="$(docker exec "$GRVT_CONTAINER" sh -c \
        "curl -fs -m 10 http://127.0.0.1:3848/api/health" 2>/dev/null)"
  if [ -z "$gh" ]; then
    PROBLEMS+=("el grid bot no responde en <code>/api/health</code>")
  else
    case "$gh" in
      *'"status":"ok"'*) : ;;
      *) PROBLEMS+=("el grid bot reporta estado degradado") ;;
    esac
  fi
fi

# ── 3. El pump-reader responde ───────────────────────────────────────────
code="$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$PUMP_URL" 2>/dev/null)"
[ "$code" = "200" ] || PROBLEMS+=("pump-reader no responde en <code>$PUMP_URL</code> (HTTP ${code:-sin respuesta})")

# ── 4. Alertar / recuperar ───────────────────────────────────────────────
# Sólo se avisa en el CAMBIO de estado: sano→caído y caído→sano. Sin esto,
# un cron cada 5 minutos mandaría 288 mensajes por día durante una caída y
# terminarías silenciando el canal justo cuando importa.
prev="$(cat "$STATE_FILE" 2>/dev/null || echo ok)"

if [ ${#PROBLEMS[@]} -gt 0 ]; then
  log "CAÍDO: ${#PROBLEMS[@]} problema(s)"
  for p in "${PROBLEMS[@]}"; do log "  - $p"; done

  if [ "$prev" != "down" ]; then
    # Saltos de línea reales: --data-urlencode de curl los codifica solo.
    # Construirlos como literales %0A y luego reinterpretarlos daba un
    # cuerpo mal formado y Telegram respondía 400.
    body=$'🔴 <b>Amatista caído</b>\n'
    for p in "${PROBLEMS[@]}"; do body+=$'\n• '"$p"; done
    body+=$'\n\n<i>'"$(date -u '+%Y-%m-%d %H:%M:%SZ')"$'</i>'
    notify "$body"
  else
    log "ya estaba caído — no repito el aviso"
  fi
  echo down > "$STATE_FILE"

  # NO se hace ping: el silencio hace saltar la alarma externa también.
  exit 1
fi

log "OK — containers arriba, grid y pump respondiendo"

if [ "$prev" = "down" ]; then
  notify "$(printf '✅ <b>Amatista recuperado</b>\n\nTodo respondiendo de nuevo.\n\n<i>%s</i>' "$(date -u '+%Y-%m-%d %H:%M:%SZ')")"
fi
echo ok > "$STATE_FILE"

# ── 5. Latido al servicio externo ────────────────────────────────────────
if [ -n "$HEARTBEAT_URL" ]; then
  curl -fsS -m 15 "$HEARTBEAT_URL" >/dev/null \
    && log "latido enviado" \
    || log "AVISO: falló el latido a $HEARTBEAT_URL"
else
  log "AVISO: HEARTBEAT_URL vacío — una caída del VPS entero NO se va a detectar."
fi
