# Deploy en el VPS — Amatista + Aura juntas

Guía para Contabo Cloud VPS 6 (Ubuntu). Reemplazá `IP-DEL-VPS` por la IP real.

## Cómo quedan repartidas las cosas

Dos stacks de Docker independientes. Cada uno con su carpeta, su `.env` y sus
volúmenes: tocar uno no afecta al otro.

El único punto compartido es **Caddy**, que es el único proceso escuchando en
los puertos 80 y 443 y reparte por dominio:

```
  Internet
     |
  Caddy (80/443)  ── aura.IP.sslip.io ──────> aura_api:8000
     |
     └──────────────  amatista.IP.sslip.io ──> amatista-pump:8000
```

Amatista **deja de publicar el 8000 al exterior**: solo la alcanza Caddy por la
red interna. Además de evitar el choque de puertos, el dashboard pasa a HTTPS —
hoy va por HTTP plano, y con dos personas entrando con usuario y contraseña eso
importa.

### Dominios sin registrar nada

`sslip.io` resuelve cualquier nombre que contenga una IP hacia esa IP, y también
sus subdominios. Con la IP `203.0.113.45`:

- `aura.203-0-113-45.sslip.io`
- `amatista.203-0-113-45.sslip.io`

Las dos apuntan al VPS, y Caddy las separa. Alcanza para que Let's Encrypt
emita los certificados.

## 1. Preparar el servidor

```bash
apt update && apt install -y docker.io docker-compose-plugin git sqlite3 curl
systemctl enable --now docker
```

`sqlite3` no es opcional: sin él el backup no puede usar `.backup` ni verificar
la integridad de la base.

## 2. Red compartida

Caddy vive en el stack de Aura y tiene que llegar a un container del stack de
Amatista. Sin una red común, no se ven.

```bash
docker network create web
```

En **los dos** `docker-compose.yml`, agregar al final:

```yaml
networks:
  web:
    external: true
```

Y sumar `web` a las redes de `caddy` (Aura) y de `pump-reader` (Amatista).
En `pump-reader`, además, **borrar la sección `ports:`** para que deje de
exponerse directo.

## 3. Clonar y levantar Amatista

```bash
git clone https://github.com/KannieO2/Amatista-Trade-AI.git
cd Amatista-Trade-AI
cp .env.example .env   # o subí tu .env
```

**Generar la master.key en el VPS.** No viaja desde tu máquina: la de allá
cifra con otra clave. Hay que crearla acá y volver a cargar las credenciales
de GRVT desde el dashboard.

```bash
mkdir -p bots/grvtbot/data bots/grvtbot/logs/bot bots/pump-reader/data
head -c 32 /dev/urandom > bots/grvtbot/data/master.key
chmod 600 bots/grvtbot/data/master.key

# El container corre como el usuario grvtbot (uid 10000), no como root. Con
# bind mount, las carpetas del host quedan de root y el proceso no puede
# escribir: arranca, tira `SQLITE_CANTOPEN: unable to open database file` y
# entra en bucle de reinicio. En local no pasa porque el override usa un
# volumen de Docker, y ahí Docker ajusta el dueño solo.
chown -R 10000:10000 bots/grvtbot/data bots/grvtbot/logs

docker compose up -d --build
```

> **Neutralizá el override antes de levantar.** `docker-compose.override.yml`
> está versionado y `docker compose` lo aplica solo, pero es exclusivo del
> checkout en OneDrive. En el VPS hay que sacarlo del camino:
>
> ```bash
> mv docker-compose.override.yml docker-compose.override.yml.local-only
> ```

> El `docker-compose.override.yml` del repo es **solo para el checkout local en
> OneDrive** (OneDrive rompe los locks de SQLite). En el VPS no aplica: los datos
> van al bind mount `bots/grvtbot/data`. El script de backup detecta cuál de los
> dos está activo.

## 4. Levantar Aura

```bash
cd ~/aura
# Caddyfile con los dos dominios (ver abajo)
docker compose -f docker-compose.prod.yml up -d
```

### Caddyfile

```caddyfile
{
	email hellopromusic@gmail.com
}

aura.IP-DEL-VPS.sslip.io {
	reverse_proxy api:8000
}

amatista.IP-DEL-VPS.sslip.io {
	reverse_proxy amatista-pump:8000
}
```

Con la IP en guiones: `203.0.113.45` → `203-0-113-45`.

## 5. Backup

```bash
crontab -e
```

```cron
15 3 * * * BACKUP_PASSPHRASE='...' BACKUP_REMOTE='gdrive:amatista' AURA_DB_CONTAINER='aura_db' AURA_DIR='/root/aura' /root/Amatista-Trade-AI/scripts/backup-critical.sh >> /var/log/amatista-backup.log 2>&1
```

- `BACKUP_PASSPHRASE` — **guardala fuera del servidor**. Si está solo acá y
  perdés el servidor, el backup no sirve para nada.
- `BACKUP_REMOTE` — sin esto el backup queda en el mismo VPS, que no te cubre
  una suspensión de cuenta. Necesita `rclone`.
- `AURA_DB_CONTAINER` — activa el `pg_dump` de Aura. Ajustá `AURA_PG_USER` y
  `AURA_PG_DB` si no son `postgres` / `aura`.

Probalo a mano una vez antes de confiar en el cron.

## 6. Watchdog

```cron
*/5 * * * * HEARTBEAT_URL='https://hc-ping.com/tu-uuid' /root/Amatista-Trade-AI/scripts/watchdog.sh >> /var/log/amatista-watchdog.log 2>&1
```

Creá el check gratis en healthchecks.io con período 10 min y gracia 5 min.

El `HEARTBEAT_URL` es lo que detecta que se cayó **el VPS entero**: ningún
proceso corriendo acá puede avisarte de eso, así que mientras todo está sano el
script hace ping y el silencio dispara la alarma del lado de ellos.

## 7. Snapshot

Con todo andando, sacá un snapshot desde el panel de Contabo. Vienen 2
incluidos. Es tu vuelta atrás si algo se rompe después.

## Antes de operar con dinero real

- [ ] Jesús con **su propio usuario**, no el tuyo
- [ ] `DASHBOARD_API_KEY` solo en el `.env` del servidor — es llave maestra:
      quien la tenga ve los canales de todos los bots
- [ ] Unos días en paper antes de pasar a live (el toggle es por bot)
- [ ] Backup probado **restaurando**, no solo generando
- [ ] Watchdog probado parando un container a propósito

## Lo que sigue sin estar cubierto

Si el VPS se cae con posiciones abiertas, **no hay nada que las cierre**. El
stop-loss del bot se evalúa dentro de su ciclo de monitoreo: sin bot corriendo,
no hay stop. La única protección real serían órdenes de disparo del lado de
GRVT, que quedan en sus servidores y se ejecutan aunque el VPS no exista.
Está pendiente.
