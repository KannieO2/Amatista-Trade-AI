#!/usr/bin/env bash
#
# Amatista TradeOS — backup de lo irrecuperable.
#
# Respalda SOLO lo que no se puede reconstruir desde git ni desde Supabase:
#   - grid_bot.db      config de bots, fills, roundtrips, historial (SQLite)
#   - master.key       sin esta clave las credenciales GRVT no se descifran
#   - .env             tokens de Telegram, claves de Supabase, DASHBOARD_API_KEY
#
# NO respalda bots/pump-reader/data (~1.1 GB): su fuente de verdad es Supabase.
#
# DÓNDE VIVEN LOS DATOS — esto NO se asume, se le pregunta a Docker.
# En el VPS, docker-compose.yml bind-montea ./bots/grvtbot/data. Pero en el
# checkout local de OneDrive, docker-compose.override.yml lo reemplaza por un
# volumen de Docker (OneDrive rompe los locks de SQLite en modo WAL). Un script
# que asumiera la ruta del disco respaldaría archivos viejos creyendo que son
# los de hoy — peor que no tener backup. Por eso resolvemos el origen real vía
# `docker inspect` y, si es un volumen, sacamos los archivos con `docker cp`.
#
# El archivo va cifrado (AES-256) porque lleva master.key y los .env.
#
# Uso:
#   BACKUP_PASSPHRASE='...' ./scripts/backup-critical.sh
#
# Cron diario a las 03:15 (crontab -e):
#   15 3 * * * BACKUP_PASSPHRASE='...' /ruta/al/repo/scripts/backup-critical.sh >> /var/log/amatista-backup.log 2>&1
#
# RESTAURAR:
#   openssl enc -d -aes-256-cbc -pbkdf2 -in amatista-FECHA.tar.gz.enc \
#     -pass env:BACKUP_PASSPHRASE | tar xzf - -C /destino
#
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/amatista}"
KEEP="${BACKUP_KEEP:-14}"
REMOTE="${BACKUP_REMOTE:-}"          # opcional: destino rclone (ej. "gdrive:amatista")
CONTAINER="${GRVTBOT_CONTAINER:-amatista-grvtbot}"
STALE_HOURS="${BACKUP_STALE_HOURS:-48}"

log()  { printf '%s  %s\n' "$(date -u '+%Y-%m-%d %H:%M:%SZ')" "$*"; }
warn() { log "AVISO: $*"; }
die()  { log "ERROR: $*" >&2; exit 1; }

[ -n "${BACKUP_PASSPHRASE:-}" ] || die "BACKUP_PASSPHRASE no está definida. Sin eso el backup iría en texto plano con tus credenciales adentro."
command -v openssl >/dev/null || die "falta openssl (apt install openssl)"

STAMP="$(date -u '+%Y%m%d-%H%M%SZ')"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$BACKUP_DIR"

# ── 1. Resolver de dónde salen los datos ─────────────────────────────────
DATA_SRC=""       # ruta en el host, si es bind mount
USE_DOCKER_CP=0

if command -v docker >/dev/null && docker inspect "$CONTAINER" >/dev/null 2>&1; then
  mount_type="$(docker inspect "$CONTAINER" \
    --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Type}}{{end}}{{end}}' 2>/dev/null || true)"
  mount_src="$(docker inspect "$CONTAINER" \
    --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"

  case "$mount_type" in
    bind)
      DATA_SRC="$mount_src"
      log "origen: bind mount -> $DATA_SRC"
      ;;
    volume)
      USE_DOCKER_CP=1
      log "origen: volumen Docker ($mount_src) — se extrae con docker cp"
      ;;
    *)
      warn "no pude determinar el mount de /app/data en $CONTAINER"
      ;;
  esac
else
  warn "container '$CONTAINER' no accesible — uso la ruta del repo"
fi

# Fallback: la ruta que usa docker-compose.yml en el VPS.
[ -n "$DATA_SRC" ] || [ "$USE_DOCKER_CP" = "1" ] || DATA_SRC="$REPO_DIR/bots/grvtbot/data"

# ── 2. Extraer los archivos ──────────────────────────────────────────────
# db + wal + shm se copian JUNTOS: el WAL puede tener cientos de KB que aún no
# pasaron al .db principal, y SQLite lo reproduce al abrir. Copiar solo el .db
# perdería esas escrituras.
if [ "$USE_DOCKER_CP" = "1" ]; then
  # Destino RELATIVO a propósito: bajo Git Bash (Windows), docker.exe recibe
  # las rutas absolutas estilo POSIX sin traducir y falla con
  # `invalid output path: directory "C:\tmp" does not exist`. Un `cd` al
  # staging + "." evita el problema y es idéntico en Linux.
  ( cd "$STAGE" && docker cp "$CONTAINER:/app/data/." . ) \
    || die "docker cp desde $CONTAINER falló"
  rm -rf "$STAGE/logs" 2>/dev/null || true
else
  [ -d "$DATA_SRC" ] || die "no existe el directorio de datos: $DATA_SRC"
  for f in grid_bot.db grid_bot.db-wal grid_bot.db-shm master.key; do
    [ -f "$DATA_SRC/$f" ] && cp "$DATA_SRC/$f" "$STAGE/$f"
  done
fi

[ -f "$STAGE/grid_bot.db" ] || die "no se obtuvo grid_bot.db — el backup estaría vacío"

if [ -f "$STAGE/master.key" ]; then
  log "master.key incluida ($(wc -c < "$STAGE/master.key") bytes)"
else
  warn "NO se encontró master.key. Si el bot ya guardó credenciales GRVT, sin esta clave NO se pueden descifrar."
fi

# ── 3. Chequeo de frescura ───────────────────────────────────────────────
# Un backup de archivos viejos es peor que ninguno: da falsa tranquilidad.
# Comparamos contra el más reciente entre db y wal (el wal se toca en cada
# escritura; el .db puede quedar quieto un buen rato en modo WAL).
newest=0
for f in "$STAGE/grid_bot.db" "$STAGE/grid_bot.db-wal"; do
  [ -f "$f" ] || continue
  m="$(date -r "$f" +%s 2>/dev/null || echo 0)"
  [ "$m" -gt "$newest" ] && newest="$m"
done
if [ "$newest" -gt 0 ]; then
  age_h=$(( ( $(date +%s) - newest ) / 3600 ))
  if [ "$age_h" -gt "$STALE_HOURS" ]; then
    warn "los datos tienen ${age_h}h sin cambios (umbral ${STALE_HOURS}h). ¿El bot está corriendo? ¿Es el origen correcto?"
  else
    log "datos frescos (última escritura hace ${age_h}h)"
  fi
fi

# ── 4. Verificar que la base sirve ───────────────────────────────────────
# Sin sqlite3 CLI usamos Python, que casi siempre está. Si no hay ninguno,
# seguimos pero avisando: mejor un backup sin verificar que ningún backup.
#
# Efecto secundario buscado: abrir y cerrar limpiamente una base en modo WAL
# hace checkpoint — el -wal se vuelca dentro del .db y queda un archivo único
# y autocontenido. Si NO hay con qué verificar, el -wal y el -shm siguen en el
# staging y entran igual al tar; SQLite reproduce el WAL al abrir. Los dos
# caminos restauran bien, por eso el tar copia el directorio entero en vez de
# una lista fija de archivos.
if command -v sqlite3 >/dev/null; then
  r="$(sqlite3 "$STAGE/grid_bot.db" 'PRAGMA integrity_check;' 2>&1 || true)"
  [ "$r" = "ok" ] || die "grid_bot.db no pasa integrity_check: $r"
  log "integrity_check ok (sqlite3)"
elif command -v python3 >/dev/null || command -v python >/dev/null; then
  PY="$(command -v python3 || command -v python)"
  "$PY" - "$STAGE/grid_bot.db" <<'PYEOF' || die "grid_bot.db no pasa integrity_check"
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
res = con.execute("PRAGMA integrity_check").fetchone()[0]
n = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
print(f"integrity_check {res} ({n} tablas)")
sys.exit(0 if res == "ok" else 1)
PYEOF
else
  warn "sin sqlite3 ni python: no se pudo verificar la integridad de la base"
fi

# ── 5. .env ──────────────────────────────────────────────────────────────
for env_rel in ".env" "bots/grvtbot/.env" "bots/pump-reader/.env"; do
  src="$REPO_DIR/$env_rel"
  if [ -f "$src" ]; then
    cp "$src" "$STAGE/env--$(echo "$env_rel" | tr '/' '_')"
    log "respaldado $env_rel"
  fi
done

# ── 6. Empaquetar + cifrar ───────────────────────────────────────────────
OUT="$BACKUP_DIR/amatista-$STAMP.tar.gz.enc"
tar czf - -C "$STAGE" . \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASSPHRASE -out "$OUT" \
  || die "falló el cifrado"
chmod 600 "$OUT"

# Un backup que no se puede restaurar no es un backup: descifrar y listar.
openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE -in "$OUT" \
  | tar tzf - >/dev/null \
  || die "el archivo generado NO se puede descifrar/leer — backup inválido"

log "backup ok: $OUT ($(du -h "$OUT" | cut -f1))"

# ── 7. Rotación ──────────────────────────────────────────────────────────
ls -1t "$BACKUP_DIR"/amatista-*.tar.gz.enc 2>/dev/null \
  | tail -n +$((KEEP + 1)) \
  | while read -r old; do rm -f "$old" && log "rotado: $(basename "$old")"; done

# ── 8. Copia FUERA del VPS ───────────────────────────────────────────────
# Un backup guardado en el mismo servidor no te salva si perdés el servidor o
# te suspenden la cuenta — el escenario más reportado en Contabo.
if [ -n "$REMOTE" ]; then
  if command -v rclone >/dev/null; then
    rclone copy "$OUT" "$REMOTE" && log "copiado a $REMOTE"
  else
    warn "BACKUP_REMOTE definido pero rclone no está instalado"
  fi
else
  warn "BACKUP_REMOTE vacío — el backup queda SOLO en este servidor."
fi
