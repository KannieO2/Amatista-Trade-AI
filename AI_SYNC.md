# AI_SYNC.md — Diario Compartido de Agentes IA

> **Propósito.** Puente de memoria entre los agentes IA que desarrollan este proyecto
> (Claude Code y Antigravity). No pueden hablar directo; ambos leen/escriben aquí.
> **Protocolo:** al iniciar sesión, LEE este archivo. Al terminar una tarea/sesión,
> AÑADE un registro al final (Fecha · Autor · Archivos · Qué · Por qué). No borres
> historial; solo se agrega. Para hechos vivos del repo, la fuente de verdad es el
> código + `bots/pump-reader/app/`, no este resumen.

---

## 1. Arquitectura base

**Producto:** "Amatista" (TradeOS) — dos bots de trading bajo un mismo dashboard.

- **Pump-Reader** (`bots/pump-reader/`) — detector de *criminal/scam pumps*. **Python /
  FastAPI / uvicorn**, puerto 8000. Modo **paper** por defecto (no opera dinero real).
  Escanea múltiples exchanges (Binance/Bitget/MEXI/etc. vía CCXT + datos públicos),
  detecta acumulación ANTES del pump y registra/alerta. Persistencia canónica en
  **Supabase** (project `nkhufhfkpttfhgpsumur`) + SQLite local (`microstructure.db`,
  tick data, NO se sube; ~428MB).
- **GRVT Grid Bot** (`bots/grvtbot/`) — bot de grid trading. **Node (express + ws) +
  React/Vite SPA**, puerto 3848. Es un fork del upstream KManuS88/GRVTBot. El
  pump-reader lo embebe vía reverse-proxy same-origin bajo `/grid/*`.

**Pila:** Python (pump), Node + React + Vite (grid), Supabase (DB + realtime + RLS),
Docker Compose (ambos contenedores), AWS EC2 (host live), Cloudflare quick tunnel
(dashboard público HTTPS), Telegram (alertas).

**Deploy live:** AWS EC2 t3.small, región sa-east-1, IP `18.231.188.187`. Código se
sube por **tarball+scp** (no git push al deploy). Rebuild: `docker compose up -d
--build pump-reader`. La VM es la **copia autoritativa** del código cuando el local
(OneDrive) falla. Dashboard público vía systemd `amatista-tunnel` (URL trycloudflare
aleatoria, cambia al reiniciar el túnel).

### Motor de detección (el núcleo del pump-reader)
1. **Scanner** (`scanner.py`) — barre exchanges, calcula pump_score, cluster, forensic.
2. **Microstructure** (`microstructure.py`) — graba snapshots tick-a-tick por símbolo.
3. **Scores** (`scores.py`) — 3 ejes ORTOGONALES sobre la ventana de micro:
   `AccumulationScore` (¿compran sin mover precio?), `PersistenceScore` (¿se sostiene?),
   `RugRiskScore` (¿el libro se deteriora?). La decisión es una MATRIZ (entrar sii
   Acc≥A ∧ Pers≥P ∧ Rug≤R), nunca una suma.
4. **Pipeline FSM** (`pipeline.py`) — máquina de estados de OBSERVACIÓN-ANTES-DE-ENTRAR:
   `candidate → watchlist → monitor → confirmation → entry` (+ discard/expired).
   Umbrales: `ACC_MIN=55`, `PERS_MIN=60`, `RUG_MAX=40`, `CONFIRM_TICKS=3` sostenidos.
   Modo `enforcing`: emite intents; `main.py` los ejecuta.
5. **Learning** (`learning.py`) — mide si la alerta salió ANTES del pump (MFE/MAE/lead),
   precision/recall, y `pump_probability` empírica por bucket (Beta-shrunk). 3 buckets:
   Successful / Failed / Dangerous (ver §3).
6. **Position Manager** (`position_manager.py`) — trailing/hard-stop/timeout, perfiles
   de salida por cluster (long_pump = tight/fast, classic = loose/patient).

---

## 2. Cronología histórica (hitos)

- **Génesis:** scanner de gainers/momentum multi-exchange + dashboard. Estructura inicial
  en `apps/`. Detectaba pumps por momentum (tarde) → perdía.
- **Giro estratégico:** el usuario (no técnico) define la directriz central: **detectar
  ANTES del pump (acumulación), NO momentum**. El feed gainers-only era el bloqueador.
- **Fase 2 (FSM + scores):** se añade la máquina de estados de observación-antes-de-entrar
  y los 3 scores ortogonales (resuelve el defecto "entra en la misma pasada que detecta").
- **Persistencia Supabase + RLS + realtime** (multi-tenant: cada cuenta su propio bot,
  aprendizaje compartido).
- **Reorg `apps/` → `bots/`** (parcial; ver §Pendientes — aún quedan duplicados).
- **Deploy AWS** (2026-06-22): tarball+scp a EC2, Docker Compose, túnel Cloudflare.
- **Endurecimiento de entradas (win-rate):** pisos de liquidez forense, score floor,
  filtro de precio (tesis ¢→$1→$2), volumen mínimo confirmando el arranque, vetos de rug.
- **2026-06-22 (sesión Claude):** gainers eliminado como motor, velocity reutilizado,
  Dangerous→learning, fix grid SPA, fix "no-trades" (ver §3).

---

## 3. Decisiones de diseño recientes (LEER — técnico, para Antigravity)

### 3.1 Por qué se borró el motor "Gainers" / `_velocity_enter`
Gainers era un motor de momentum SEPARADO (book="gainers") que perseguía subidas ya en
curso → entraba tarde, comía stops, solo perdía. El usuario lo confirmó como
"sin utilidad más que perder dinero". Se **eliminó `_velocity_enter`** (su path de
entrada + gates `GAINERS_*`). Se conserva el plumbing `book="gainers"` SOLO para
integridad histórica del P&L de trades viejos (borrarlo corrompería el historial).

### 3.2 Por qué `_velocity_loop` ahora está atado a la FSM (y `VELOCITY_AUTOENTRY=false`)
El motor velocity (`velocity.py`) detecta aceleración de volumen en milisegundos. Antes
solo alimentaba gainers (apagado) → estaba muerto. **Reutilización:** velocity ahora es
el **acelerador de reflejos del ÚNICO motor (FSM)**. Dispara la entrada SOLO si el token
ya está en estado `confirmation` del FSM (o sea, ya pasó Acc/Pers/Rug). El FSM pone la
RIGIDEZ (qué comprar); velocity pone la VELOCIDAD (cuándo, en el milisegundo del break).
Sin token en `confirmation` → no hace nada (cero chase). `VELOCITY_AUTOENTRY`/`MOMENTUM_
AUTOENTRY`/`GAINERS_COIL_AUTOENTRY` quedan en `false`: no se persiguen movimientos sueltos.
Nuevo método `Pipeline.state_of(symbol, exchange)`.

### 3.3 Anti-Top `ENTRY_MAX_RUNUP_PCT = 12%`
Problema real: comprar la CIMA de una rampa ya despegada y comerse el stop (casos tipo
EIGEN/SUI). Gate: si el token ya subió ≥12% sobre su base reciente (intradía, multi-vela),
NO se entra — entra AL arranque, no después. Complementa `ENTRY_MAX_CHASE_PCT=60%`
(techo de subida en 24h).

### 3.4 Fix "no-trades" (2026-06-22) — el más importante
Síntoma: el bot nunca sacaba operaciones de ruptura. Diagnóstico (estado vivo): FSM sano
pero el rechazo #1 era **"Plano, Sin Ruptura" (`skip_fsm_flat`) = 110**. Catch-22: el FSM
confirma ACUMULACIÓN = precio PLANO por definición; el gate de entrada exigía ADEMÁS
+1.5% de precio ya movido (`FSM_MIN_BREAKOUT_PCT`) → doble-bloqueo. El comentario del
propio código dice que la confirmación REAL es el **gate de VOLUMEN** (`FSM_MIN_ENTRY_VOL_
SPIKE=4x`). **Fix:** se waivea `skip_fsm_flat` cuando el volumen ya confirma (≥4x = la
ruptura; el precio la sigue). `_velocity_loop` ahora pasa `accel=t.accel` para que el gate
de volumen vea el break real. Vetos de seguridad SIN cambios.

### 3.5 Dangerous_Signals → aprendizaje (data-integrity §4)
Existía un set persistente que BLOQUEABA tokens scam (concentración de holders, dump
on-chain, MANIPULATION_SUSPECT) pero NUNCA alimentaba el learning → los pesos no aprendían
a evitarlos. Se añadió `LearningLab.record_dangerous()` (clase "dangerous", no cuenta para
precision) y `pump_probability` ahora suma los scams del mismo bucket (cluster+vol) como
cuasi-fallos → un perfil con historial peligroso entra con P más baja. **Nada se borra**;
3 buckets honestos (Successful/Failed/Dangerous) en `/learning/buckets`.

### 3.6 Auto-tune de alertas
`_optimization_loop` (cada hora) ajusta solo `ALERT_MIN_PROBABILITY` según la precisión
medida (precisión baja → sube el piso; alta + lead corto → baja), banda [0.25,0.70],
persiste y se restaura al arranque.

### 3.7 Reglas inquebrantables de validación (capital sobre cantidad)
- **Forensic hard floor:** `FORENSIC_HARD_MIN_LIQUIDITY_USD = $15.000` (libros más
  finos = rug-trap; LAYER ganó a $17k → piso a $15k).
- **Techo de liquidez del libro:** `ENTRY_MAX_LIQUIDITY_USD = $150.000` (big-caps no
  pumpean). **Techo de market cap:** `$50M` (filtro microcap real).
- **Tesis criminal-pump:** `PREPUMP_MAX_PRICE = $1` (¢→$1→$2; un token caro tiene poco
  espacio multi-x). `PREPUMP_MIN_SCORE = 50` (no entra basura sub-50).
- **Vetos de rug (marcan Dangerous + bloquean):** holders concentrados (top1>25% / top10>70%),
  dump on-chain (buy_ratio bajo con flujo), MANIPULATION_SUSPECT (libro flaco+concentrado).
- **On-chain:** nunca es gate duro en CEX (honeypot/GoPlus solo aplica a DEX); se usa como
  confirmación de presión compradora (heat) o veto de dump, no como bloqueo universal.
- **Risk guard:** kill-switch global auto (rate-limit storm + caída de volumen 60%);
  `max_open_trades`, position size por riesgo fijo, sin permiso de Withdrawal en API keys.
- **Iceberg:** si la entrada supera ~2% de la profundidad del libro, se parte en 3.

---

## 4. Pendientes actuales

- **[PRINCIPAL] El bot no sacaba operaciones de ruptura.** Causa raíz encontrada y
  parchada (§3.4). Verificar en vivo que ahora SÍ entran trades cuando un token en
  `confirmation` rompe con ≥4x volumen. Si siguen sin salir, revisar si el problema es
  que pocos tokens llegan a `confirmation` (umbrales Acc/Pers/Rug o ventana de micro).
- **[GRVT login bypass]** El SPA del grid carga (fix de asset-base en `grvt_proxy.py`:
  reescribe `/dashboard/` → `/grid/dashboard/`). El SSO mintea token (`/grid-sso` con
  `GRVT_BACKEND_HOST`). Falta: que el grid TRADEE necesita **API keys GRVT reales** en
  `bots/grvtbot/.env` (las actuales son placeholders). Tarea: pulir el bypass de login
  para que el usuario nunca vea el login del grid (SSO transparente, ya casi).
- **Reorg `apps/` → `bots/` incompleto:** `apps/` aún tiene duplicados viejos +
  `apps/dashboard/` (Next.js no migrado). No borrar `apps/` sin confirmar que nada vivo
  depende de él.
- **Recolor del grid** al tema del pump-reader (paleta) — propuesto, no priorizado.

---

## 5. Registro de cambios (append-only)

### 2026-06-22 · Autor: Claude (Opus 4.8)
- **Archivos:** `bots/pump-reader/app/main.py`, `learning.py`, `pipeline.py`,
  `grvt_proxy.py`; `AI_SYNC.md` (nuevo).
- **Qué:** (1) Borrado gainers como motor + velocity reutilizado como acelerador de
  ruptura del FSM (§3.2). (2) Dangerous→learning con penalización de probabilidad (§3.5).
  (3) Fix SPA del grid: reescritura de asset-base en el proxy (§Pendientes). (4) Fix
  "no-trades": waive `skip_fsm_flat` cuando volumen≥4x confirma (§3.4). (5) Auto-tune de
  `ALERT_MIN_PROBABILITY` por precisión (§3.6).
- **Por qué:** el usuario reportó cero operaciones de ruptura + sensación de "piezas
  sueltas". El sistema ahora sigue UN motor (FSM) con velocity como timing, y el
  aprendizaje cierra el lazo (evita scams, ajusta alertas). Capital sobre cantidad.
- **Incidente:** durante un error de límite de sesión se borraron casi todos los `.py`
  locales de `bots/pump-reader/app/`; restaurados desde la VM (fuente de verdad).
  Recomendado: commitear a git para no depender de la VM/OneDrive.
