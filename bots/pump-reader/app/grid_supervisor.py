"""Supervisor del GRVTBot (grid) — UN solo sistema, sin segunda terminal.

Arranca el grid como proceso en SEGUNDO PLANO desde esta app (pump-reader), para
que un solo arranque levante todo y el grid se vea embebido en el dashboard.

REGLAS DE SEGURIDAD (el grid maneja DINERO REAL):
  - NO toca el código del grid. Solo lo lanza.
  - NO lo arranca si ya está corriendo (chequea el puerto) → nunca duplica órdenes.
  - Lo lanza DETACHED: sobrevive a un reinicio/cierre del pump-reader (no se mata).
  - NUNCA lo detiene. El pump-reader jamás cierra el grid.
  - Se puede apagar con PUMP_SUPERVISE_GRID=false.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

GRID_PORT = int(os.getenv("PUMP_GRID_PORT", "3848"))
# bots/pump-reader/app/grid_supervisor.py → parents[2] = bots/ ; grid = bots/grvtbot
GRID_DIR = Path(__file__).resolve().parents[2] / "grvtbot"
GRID_ENTRY = GRID_DIR / "packages" / "bot" / "dist" / "dashboard" / "server.js"


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_grid_running() -> str:
    """Best-effort: arranca el grid en segundo plano si no está ya corriendo.
    Devuelve un estado para loguear. Nunca lanza (no debe romper el arranque)."""
    if os.getenv("PUMP_SUPERVISE_GRID", "true").lower() not in ("1", "true", "yes", "on"):
        return "disabled"
    if _port_open(GRID_PORT):
        return "already-running"          # ya está vivo → NO duplicar
    node = shutil.which("node")
    if not node:
        logger.warning("grid supervisor: 'node' no está en PATH — no se arranca el grid")
        return "no-node"
    if not GRID_ENTRY.exists():
        logger.warning("grid supervisor: build no encontrado (%s) — corre 'npm run build' en el grid", GRID_ENTRY)
        return "no-build"
    # Detached + sin ventana → sobrevive a un reinicio del pump-reader.
    flags = 0
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        flags = DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:
        log_path = GRID_DIR / "grid-supervised.log"
        log_fh = open(log_path, "ab")     # preserva los logs del grid (dinero real)
        subprocess.Popen(
            [node, str(GRID_ENTRY)],
            cwd=str(GRID_DIR),
            stdout=log_fh, stderr=log_fh, stdin=subprocess.DEVNULL,
            creationflags=flags, close_fds=True,
            start_new_session=(os.name != "nt"),   # POSIX: también lo desacopla
        )
        logger.info("grid supervisor: GRVTBot lanzado en 2º plano (:%d, detached) → logs en %s",
                    GRID_PORT, log_path.name)
        return "started"
    except Exception:
        logger.exception("grid supervisor: fallo al lanzar el grid")
        return "error"
