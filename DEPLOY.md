# Desplegar Amatista TradeOS 24/7 — APP COMPLETA (los dos bots)

Monta en una VM Linux **toda la app**:
- **pump-reader** (Python :8000) — dashboard + detector ScamPump (gainers + pre-pump).
- **grvtbot** (Node :3848) — bot de grid GRVT (el tab "Grid Bot", embebido en el dashboard).

Un solo `docker-compose.yml` (en la raíz del repo) levanta los dos en una red privada.
Solo el pump-reader publica puerto, atado a `127.0.0.1` → se ve por **túnel SSH** (el
password no viaja en claro). El grid se ve DENTRO del dashboard (tab Grid Bot).

> Corre en **PAPER** (pump) y el grid solo opera si le pones GRVT API keys. Sin dinero
> real hasta tener edge probado con semanas de data.

---

## 0. Antes de empezar (en tu PC)
Los cambios de esta sesión están SOLO en tu PC. Para que la VM tenga la última versión,
**sube el código a GitHub primero**:
```bash
cd "ruta/local/Trading IA"
git add -A && git commit -m "deploy: app completa (pump + grvtbot)"
git push
```
> Los `.env` y `master.key` NO se suben (están en `.gitignore`) — van aparte por scp (paso 4).
> (Si prefieres, dime "commitea y pushea" y lo hago yo.)

---

## 1. Crear la VM — AWS EC2
1. console.aws.amazon.com → **EC2 → Launch instance**. Name: `amatista`.
2. AMI: **Ubuntu Server 22.04 LTS**. Tipo: **t3.small** (2 GB, ~$15/mes). Dos bots +
   build de Node pesan; la `t2.micro` (1 GB free) necesita swap (paso 3b) y va justa.
3. **Key pair**: crear → descarga el **.pem** y guárdalo.
4. **Security group**: solo **SSH (22)** desde **My IP**. NO abras 8000/3848.
5. **Storage**: 30 GB gp3. → **Launch**.
6. Apunta la **IP pública** (opcional: Elastic IP para que no cambie).

**Permisos del .pem en Windows** (si `ssh` se queja):
- Git Bash:  `chmod 400 tu.pem`
- PowerShell: `icacls tu.pem /inheritance:r /grant:r "$($env:USERNAME):R"`

---

## 2. Entrar a la VM
```bash
ssh -i RUTA/tu.pem ubuntu@IP_PUBLICA
```

## 3. Instalar Docker (una vez)
```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER
exit            # cierra sesión para que aplique el grupo docker
```
Vuelve a entrar con el mismo `ssh ...`.

**3b. Swap (SOLO t2.micro 1 GB):**
```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 4. Traer el código + los secretos
```bash
# Código (en la VM) — clona el repo (ya pusheado en el paso 0):
git clone https://github.com/KannieO2/Amatista-Trade-AI.git amatista
cd amatista
```
Sube los **3 archivos de secretos** desde TU PC (otra terminal). Van cifrados por SSH:
```bash
# 1) .env del pump (está en la RAÍZ del repo local)
scp -i RUTA/tu.pem "ruta/local/Trading IA/.env"             ubuntu@IP:~/amatista/.env
# 2) .env del grvtbot (GRVT API keys)
scp -i RUTA/tu.pem "ruta/local/.../bots/grvtbot/.env"       ubuntu@IP:~/amatista/bots/grvtbot/.env
# 3) llave maestra del grvtbot
scp -i RUTA/tu.pem "ruta/local/.../bots/grvtbot/master.key" ubuntu@IP:~/amatista/bots/grvtbot/master.key
```
> Si el grvtbot aún no tiene GRVT keys, arranca igual pero el grid queda inactivo
> hasta que pongas las keys en `bots/grvtbot/.env`. El pump funciona sin eso.

## 5. Arrancar TODO (en la VM, desde `~/amatista`)
```bash
docker compose up -d --build      # construye y levanta los dos bots
docker compose ps                 # ambos 'running'
curl -s localhost:8000/health     # -> {"status":"ok",...}
```

---

## 6. Ver el dashboard (túnel SSH)
Desde TU PC:
```bash
ssh -i RUTA/tu.pem -L 8000:localhost:8000 ubuntu@IP_PUBLICA
```
Deja esa terminal abierta → navegador en **http://localhost:8000**
(login `APP_USERNAME`/`APP_PASSWORD` del .env). El tab **Grid Bot** embebe el grvtbot
(mismo origen, vía el proxy `/grid/*` → contenedor grvtbot). Un solo sitio, dos bots.

## 7. Operación
```bash
docker compose logs -f --tail=100 pump-reader   # logs del pump
docker compose logs -f --tail=100 grvtbot       # logs del grid
docker compose restart                          # reiniciar ambos
docker compose down                             # parar
# actualizar tras cambios (en tu PC: git push; en la VM):
cd ~/amatista && git pull && docker compose up -d --build
```

---

## Seguridad
- **Paper** por defecto en el pump; el grid solo opera con GRVT keys que TÚ pongas.
- Real algún día: API keys **sin permiso de retiro** + **IP whitelist** a la IP de la VM
  (en el exchange/GRVT, no en el código).
- Secretos (`.env` × 2, `master.key`) solo en la VM y tu PC. Jamás en git.
- Nada expuesto a internet (puertos en `127.0.0.1` + túnel SSH). Para HTTPS público con
  dominio (Caddy/DuckDNS) — pídelo y lo añado.
