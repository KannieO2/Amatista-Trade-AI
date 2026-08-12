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
# El archivo va cifrado (AES-256) porque lleva master.key y los .env. Nunca
# subas esto a ningún lado sin cifrar.
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
REMOTE="${BACKUP_REMOTE:-}"   # opcional: destino rclone (ej. "gdrive:amatista")

log() { printf '%s  %s\n' "$(date -u '+%Y-%m-%d %H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[ -n "${BACKUP_PASSPHRASE:-}" ] || die "BACKUP_PASSPHRASE no está definida. Sin eso el backup iría en texto plano con tus credenciales adentro."

command -v openssl >/dev/null || die "falta openssl (apt install openssl)"

STAMP="$(date -u '+%Y%m%d-%H%M%SZ')"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$BACKUP_DIR"

# ── 1. SQLite ────────────────────────────────────────────────────────────
# `cp grid_bot.db` NO alcanza: el WAL puede tener cientos de KB de escrituras
# que todavía no pasaron al .db principal, y una copia cruda queda incompleta
# o corrupta. `.backup` toma una foto consistente con la base EN USO, sin
# parar el bot.
DB_SRC="$REPO_DIR/bots/grvtbot/data/grid_bot.db"
if [ -f "$DB_SRC" ]; then
  if command -v sqlite3 >/dev/null; then
    sqlite3 "$DB_SRC" ".backup '$STAGE/grid_bot.db'" \
      || die "sqlite3 .backup falló sobre $DB_SRC"
    # Verificar que la copia sirve ANTES de darla por buena.
    integrity="$(sqlite3 "$STAGE/grid_bot.db" 'PRAGMA integrity_check;')"
    [ "$integrity" = "ok" ] || die "la copia de grid_bot.db no pasa integrity_check: $integrity"
    log "grid_bot.db respaldado y verificado (integrity_check ok)"
  else
    # Sin sqlite3: copiar db+wal+shm juntos. Menos seguro que .backup pero
    # recuperable, porque SQLite reproduce el WAL al abrir.
    log "AVISO: sqlite3 no instalado (apt install sqlite3). Copiando db+wal+shm en crudo."
    cp "$DB_SRC" "$STAGE/grid_bot.db"
    [ -f "$DB_SRC-wal" ] && cp "$DB_SRC-wal" "$STAGE/grid_bot.db-wal"
    [ -f "$DB_SRC-shm" ] && cp "$DB_SRC-shm" "$STAGE/grid_bot.db-shm"
  fi
else
  log "AVISO: no existe $DB_SRC — ¿ruta equivocada?"
fi

# ── 2. master.key ────────────────────────────────────────────────────────
# Lo más crítico del backup. Sin esto las credenciales GRVT guardadas quedan
# inservibles y hay que generar API keys nuevas para CADA usuario.
MK="$REPO_DIR/bots/grvtbot/data/master.key"
if [ -f "$MK" ]; then
  cp "$MK" "$STAGE/master.key"
  log "master.key respaldada"
else
  log "AVISO: no se encontró master.key en $MK"
fi

# ── 3. .env ──────────────────────────────────────────────────────────────
for env_rel in ".env" "bots/grvtbot/.env" "bots/pump-reader/.env"; do
  src="$REPO_DIR/$env_rel"
  if [ -f "$src" ]; then
    dest="$STAGE/env--$(echo "$env_rel" | tr '/' '_')"
    cp "$src" "$dest"
    log "respaldado $env_rel"
  fi
done

# ── 4. Empaquetar + cifrar ───────────────────────────────────────────────
OUT="$BACKUP_DIR/amatista-$STAMP.tar.gz.enc"
tar czf - -C "$STAGE" . \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -pass env:BACKUP_PASSPHRASE -out "$OUT" \
  || die "falló el cifrado"
chmod 600 "$OUT"

# Verificar que el archivo se puede descifrar y abrir. Un backup que no se
# puede restaurar no es un backup.
openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_PASSPHRASE -in "$OUT" \
  | tar tzf - >/dev/null \
  || die "el archivo generado NO se puede descifrar/leer — backup inválido"

log "backup ok: $OUT ($(du -h "$OUT" | cut -f1))"

# ── 5. Rotación ──────────────────────────────────────────────────────────
ls -1t "$BACKUP_DIR"/amatista-*.tar.gz.enc 2>/dev/null \
  | tail -n +$((KEEP + 1)) \
  | while read -r old; do rm -f "$old" && log "rotado: $(basename "$old")"; done

# ── 6. Copia FUERA del VPS (lo que de verdad importa) ────────────────────
# Un backup guardado en el mismo servidor no te salva si perdés el servidor
# o te suspenden la cuenta — que es el escenario más reportado en Contabo.
if [ -n "$REMOTE" ]; then
  if command -v rclone >/dev/null; then
    rclone copy "$OUT" "$REMOTE" && log "copiado a $REMOTE"
  else
    log "AVISO: BACKUP_REMOTE definido pero rclone no está instalado"
  fi
else
  log "AVISO: BACKUP_REMOTE vacío — el backup queda SOLO en este servidor."
fi
