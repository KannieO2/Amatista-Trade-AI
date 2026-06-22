"""Decision log — auditoría legible de CADA decisión del bot (compra/rechazo/venta).

Cumple el requisito "Logs detallado donde cada decisión esté justificada por los
filtros de seguridad". NO toca estrategia: es un sink aditivo. Escribe una línea
JSONL por decisión en `data/decisions.log`, rotando por tamaño. Fail-safe total:
cualquier error se traga (nunca tumba un trade).

Tres tipos:
  BUY    — entrada ejecutada (con tamaño, slices iceberg, libro).
  REJECT — candidato bloqueado (con el filtro exacto que lo justificó).
  SELL   — salida (con razón y pnl).

El bucket REJECT/SELL se alimenta solo desde `_record_learning_raw` (choke-point
único de todos los skip_*/forensic_block/exit_*); BUY se llama explícito al fill.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .microstructure import MICRO_DB_PATH

logger = logging.getLogger("pump-reader.decision_log")

_DEFAULT = str(Path(MICRO_DB_PATH).resolve().parent / "decisions.log")
PATH = os.getenv("PUMP_DECISION_LOG", _DEFAULT)
ENABLED = os.getenv("PUMP_DECISION_LOG_ENABLED", "true").lower() == "true"
MAX_BYTES = int(os.getenv("PUMP_DECISION_LOG_MAX_BYTES", str(5 * 1024 * 1024)))

_lock = threading.Lock()


def _classify(action: str) -> str | None:
    """Mapea la acción de learning al tipo de decisión humano. None = ignorar
    (ruido como alertas/recolección que no es una decisión de capital)."""
    a = action.lower()
    if a.startswith("skip") or a == "forensic_block":
        return "REJECT"
    if a.startswith("exit_") or a == "trade_closed":
        return "SELL"
    return None


def _rotate_if_big() -> None:
    try:
        if os.path.exists(PATH) and os.path.getsize(PATH) > MAX_BYTES:
            bak = PATH + ".1"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(PATH, bak)
    except Exception:
        pass


def write(kind: str, symbol: str, *, exchange: str = "n/a", reason: str = "",
          **ctx) -> None:
    """Append una decisión. kind ∈ {BUY, REJECT, SELL}. Nunca lanza."""
    if not ENABLED:
        return
    try:
        row = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "kind": kind,
            "symbol": symbol,
            "exchange": exchange,
            "reason": reason,
        }
        for k, v in ctx.items():
            if v is not None:
                row[k] = v
        line = json.dumps(row, ensure_ascii=False, default=str)
        with _lock:
            _rotate_if_big()
            Path(PATH).parent.mkdir(parents=True, exist_ok=True)
            with open(PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        logger.debug("decision_log write failed", exc_info=True)


def from_learning(symbol: str, action: str, detail: str, *,
                  exchange: str = "n/a", pump_score: int | None = None,
                  classification: str | None = None) -> None:
    """Hook desde `_record_learning_raw`. Filtra a REJECT/SELL y escribe."""
    kind = _classify(action)
    if kind is None:
        return
    write(kind, symbol, exchange=exchange, reason=detail,
          filter=action, score=pump_score, cluster=classification)


def tail(n: int = 200) -> list[dict]:
    """Últimas n decisiones (para exponer en el dashboard / API). Read-only."""
    if not os.path.exists(PATH):
        return []
    try:
        with _lock, open(PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-n:]
        out = []
        for ln in lines:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
        out.reverse()
        return out
    except Exception:
        return []
