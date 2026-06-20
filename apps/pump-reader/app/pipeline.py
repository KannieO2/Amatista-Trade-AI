"""FASE 2 §6 + observabilidad (fases 4/5/6) — máquina de estados + Decision Log.

Pipeline de OBSERVACIÓN-ANTES-DE-ENTRAR. Resuelve el defecto estructural del bot
actual (entra en la misma pasada que detecta). Cada símbolo recorre:

    CANDIDATE → WATCHLIST → MONITOR → CONFIRMATION → ENTRY
                                   ↘ DISCARD / EXPIRED

La FSM lee EXCLUSIVAMENTE las ventanas de micro_snapshots (Fase 1) y las puntúa
con scores.py (Fase 2). Cada transición y cada evaluación se escribe en
decision_log (append-only) — auditable, reproducible, base del dashboard.

Modos (PUMP_FSM_MODE):
  - "shadow"    (default): observa y registra lo que HARÍA. No toca las entradas
                 reales. Sirve para ver cómo opera el rediseño sin riesgo.
  - "enforcing": tick() devuelve intents de entrada; main.py los ejecuta por el
                 motor de ejecución actual (PositionManager/Risk SIN cambios).

Escribe en el MISMO archivo SQLite del recorder (WAL). Nunca lanza al caller.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .microstructure import MICRO_DB_PATH, MicroStore
from .scores import ScoreSet, evaluate

logger = logging.getLogger("pump-reader.pipeline")

# --- configuración (calibrable por env) ---------------------------------------
FSM_MODE = os.getenv("PUMP_FSM_MODE", "enforcing").lower()     # shadow | enforcing
WINDOW_MIN = int(os.getenv("PUMP_FSM_WINDOW_MIN", "30"))       # ventana de scoring (min)
MIN_ROWS = int(os.getenv("PUMP_FSM_MIN_ROWS", "8"))           # filas para WATCHLIST→MONITOR
ACC_MIN = int(os.getenv("PUMP_FSM_ACC_MIN", "55"))           # umbral AccumulationScore
PERS_MIN = int(os.getenv("PUMP_FSM_PERS_MIN", "60"))         # umbral PersistenceScore
RUG_MAX = int(os.getenv("PUMP_FSM_RUG_MAX", "40"))           # techo RugRiskScore
CONFIRM_TICKS = int(os.getenv("PUMP_FSM_CONFIRM_TICKS", "3")) # ticks sostenidos → ENTRY
EXPIRE_MIN = int(os.getenv("PUMP_FSM_EXPIRE_MIN", "120"))    # descarta si no confirma
# Tras cuántos min se BORRA de la tabla de estado un token terminal (expired/
# discard). NO toca decision_log (el historial/aprendizaje se conserva): solo
# limpia el board para que los NO-candidatos no se acumulen, y permite que un
# token re-entre al embudo si más tarde empieza a acumular de verdad.
PRUNE_TERMINAL_MIN = int(os.getenv("PUMP_FSM_PRUNE_MIN", "60"))

STATES = ("candidate", "watchlist", "monitor", "confirmation", "entry", "discard", "expired")


@dataclass
class EntryIntent:
    symbol: str
    exchange: str
    scores: ScoreSet
    at: float = field(default_factory=time.time)


class Pipeline:
    DDL = """
    CREATE TABLE IF NOT EXISTS fsm_state (
        symbol        TEXT NOT NULL,
        exchange      TEXT NOT NULL,
        state         TEXT NOT NULL,
        since_ts_ms   INTEGER,
        last_eval_ts_ms INTEGER,
        confirm_count INTEGER DEFAULT 0,
        acc           INTEGER DEFAULT 0,
        pers          INTEGER DEFAULT 0,
        rug           INTEGER DEFAULT 0,
        seq           REAL    DEFAULT 0,
        updated_at    INTEGER,
        PRIMARY KEY (symbol, exchange)
    );
    CREATE TABLE IF NOT EXISTS decision_log (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_ms     INTEGER NOT NULL,
        symbol    TEXT, exchange TEXT,
        from_state TEXT, to_state TEXT,
        action    TEXT,
        acc INTEGER, pers INTEGER, rug INTEGER, seq REAL,
        detail    TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_decision_ts  ON decision_log(ts_ms);
    CREATE INDEX IF NOT EXISTS ix_decision_sym ON decision_log(symbol, exchange);
    """

    def __init__(self, path: str = MICRO_DB_PATH, store: MicroStore | None = None) -> None:
        self.path = path
        self.mode = FSM_MODE
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.executescript(self.DDL)
        self._conn.commit()
        # MicroStore propio para LECTURA de ventanas (misma DB, WAL).
        self._reader = store or MicroStore(path)

    @staticmethod
    def _key(symbol: str, exchange: str) -> tuple[str, str]:
        return symbol.upper(), exchange.lower()

    # --- escritura de estado / log ------------------------------------------
    def _log(self, symbol: str, exchange: str, frm: str, to: str, action: str,
             s: ScoreSet | None = None, detail: dict | None = None) -> None:
        acc = s.accumulation if s else None
        pers = s.persistence if s else None
        rug = s.rug_risk if s else None
        seq = s.sequence_bonus if s else None
        det = json.dumps(detail or (s.components if s else {}), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO decision_log (ts_ms,symbol,exchange,from_state,to_state,action,"
                "acc,pers,rug,seq,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time() * 1000), symbol, exchange, frm, to, action, acc, pers, rug, seq, det),
            )
            self._conn.commit()

    def _get(self, symbol: str, exchange: str) -> dict | None:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            cur = self._conn.execute(
                "SELECT * FROM fsm_state WHERE symbol=? AND exchange=?", (symbol, exchange))
            row = cur.fetchone()
            self._conn.row_factory = None
        return dict(row) if row else None

    def _put(self, symbol: str, exchange: str, state: str, *, since: int | None = None,
             confirm_count: int = 0, s: ScoreSet | None = None) -> None:
        now = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                "INSERT INTO fsm_state (symbol,exchange,state,since_ts_ms,last_eval_ts_ms,"
                "confirm_count,acc,pers,rug,seq,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,exchange) DO UPDATE SET state=excluded.state,"
                "last_eval_ts_ms=excluded.last_eval_ts_ms,confirm_count=excluded.confirm_count,"
                "acc=excluded.acc,pers=excluded.pers,rug=excluded.rug,seq=excluded.seq,"
                "updated_at=excluded.updated_at,"
                "since_ts_ms=COALESCE(?,fsm_state.since_ts_ms)",
                (symbol, exchange, state, since or now, now, confirm_count,
                 s.accumulation if s else 0, s.persistence if s else 0,
                 s.rug_risk if s else 0, s.sequence_bonus if s else 0.0, now,
                 since),
            )
            self._conn.commit()

    # --- API ----------------------------------------------------------------
    def note_candidate(self, symbol: str, exchange: str) -> None:
        """El scan vio este símbolo. Lo admite a la FSM si es nuevo O si había
        terminado (expired/discard) — un token que vuelve a aparecer y empieza a
        acumular merece re-entrar al embudo, no quedar bloqueado para siempre."""
        symbol, exchange = self._key(symbol, exchange)
        cur = self._get(symbol, exchange)
        if cur is None or cur["state"] in ("expired", "discard"):
            self._put(symbol, exchange, "watchlist", since=int(time.time() * 1000), confirm_count=0)
            self._log(symbol, exchange, cur["state"] if cur else "candidate", "watchlist", "admit")

    def tick(self) -> list[EntryIntent]:
        """Un barrido de la FSM sobre todos los símbolos no terminales. Devuelve
        los intents de entrada (en enforcing main.py los ejecuta). Best-effort."""
        intents: list[EntryIntent] = []
        # Limpia los NO-candidatos añejos antes de barrer (mantiene el board limpio).
        self.prune_terminal()
        try:
            rows = self._all_active()
        except Exception:
            logger.exception("pipeline.tick read failed")
            return intents
        now = int(time.time() * 1000)
        for st in rows:
            symbol, exchange = st["symbol"], st["exchange"]
            state = st["state"]
            try:
                # caducidad por inactividad sin confirmar
                since = st["since_ts_ms"] or now
                if state in ("watchlist", "monitor", "confirmation") and \
                        (now - since) > EXPIRE_MIN * 60_000:
                    self._put(symbol, exchange, "expired")
                    self._log(symbol, exchange, state, "expired", "timeout")
                    continue

                W = self._reader.recent(symbol, exchange, WINDOW_MIN)
                if len(W) < MIN_ROWS:
                    continue  # sigue en watchlist hasta tener ventana

                s = evaluate(W)
                if state == "watchlist":
                    self._put(symbol, exchange, "monitor", s=s)
                    self._log(symbol, exchange, "watchlist", "monitor", "window_ready", s)
                    state = "monitor"

                passes = (s.accumulation >= ACC_MIN and s.persistence >= PERS_MIN
                          and s.rug_risk <= RUG_MAX)

                if state == "monitor":
                    if passes:
                        self._put(symbol, exchange, "confirmation", confirm_count=1, s=s)
                        self._log(symbol, exchange, "monitor", "confirmation", "scores_cross", s)
                    else:
                        self._put(symbol, exchange, "monitor", s=s)  # refresca scores
                        self._log(symbol, exchange, "monitor", "monitor", "evaluate", s)
                    continue

                if state == "confirmation":
                    if not passes:
                        # se rompió la señal antes de sostenerse
                        self._put(symbol, exchange, "monitor", s=s)
                        self._log(symbol, exchange, "confirmation", "monitor", "lost_signal", s)
                        continue
                    cc = (st["confirm_count"] or 0) + 1
                    if cc >= CONFIRM_TICKS:
                        if self.mode == "enforcing":
                            # READY: emite el intent pero NO marca 'entry' todavía —
                            # main.py ejecuta la compra y llama mark_entered SOLO si
                            # hubo fill real (así 'entry' = comprado de verdad, no un
                            # estado falso cuando el forensic/risk lo bloquea).
                            self._put(symbol, exchange, "confirmation", confirm_count=cc, s=s)
                            self._log(symbol, exchange, "confirmation", "confirmation", "ready_to_enter", s)
                        else:
                            self._put(symbol, exchange, "entry", confirm_count=cc, s=s)
                            self._log(symbol, exchange, "confirmation", "entry", "confirmed_shadow", s)
                        intents.append(EntryIntent(symbol, exchange, s))
                    else:
                        self._put(symbol, exchange, "confirmation", confirm_count=cc, s=s)
                        self._log(symbol, exchange, "confirmation", "confirmation", "sustaining", s,
                                  detail={"confirm_count": cc, "need": CONFIRM_TICKS})
                    continue
            except Exception:
                logger.exception("pipeline.tick symbol %s:%s", exchange, symbol)
        return intents

    def mark_entered(self, symbol: str, exchange: str) -> None:
        """main.py confirma que ejecutó la entrada (enforcing). Marca entry SIN
        borrar los scores que la llevaron ahí (antes _put con s=None los ponía en
        0 → la fila entry mostraba acc=0, ilegible)."""
        symbol, exchange = self._key(symbol, exchange)
        now = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                "UPDATE fsm_state SET state='entry', updated_at=? WHERE symbol=? AND exchange=?",
                (now, symbol, exchange))
            self._conn.commit()

    def mark_closed(self, symbol: str, exchange: str) -> None:
        """main.py: the position for this token just CLOSED. Move it OUT of 'entry'
        to a terminal state. Without this, 'entry' stuck forever and every closed
        trade kept piling a stale 'buy' on the board (17 shown, 1 actually open).
        Terminal => leaves the board, gets pruned, and can re-admit later if the
        token sets up again (note_candidate re-admits expired/discard)."""
        symbol, exchange = self._key(symbol, exchange)
        now = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                "UPDATE fsm_state SET state='expired', updated_at=? "
                "WHERE symbol=? AND exchange=? AND state='entry'",
                (now, symbol, exchange))
            self._conn.commit()
        self._log(symbol, exchange, "entry", "expired", "trade_closed")

    def _all_active(self) -> list[dict]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            cur = self._conn.execute(
                "SELECT * FROM fsm_state WHERE state IN ('watchlist','monitor','confirmation')")
            out = [dict(r) for r in cur.fetchall()]
            self._conn.row_factory = None
        return out

    # --- lectura para dashboard / API ---------------------------------------
    def state_counts(self) -> dict:
        with self._lock:
            cur = self._conn.execute("SELECT state, COUNT(*) FROM fsm_state GROUP BY state")
            return {r[0]: r[1] for r in cur.fetchall()}

    def board(self, limit: int = 60) -> list[dict]:
        """Solo CANDIDATOS VIVOS. Los terminales (expired/discard) no son
        candidatos → fuera del board (su historial sigue en decision_log)."""
        return self._q("SELECT symbol,exchange,state,acc,pers,rug,seq,confirm_count,"
                       "since_ts_ms,last_eval_ts_ms FROM fsm_state "
                       "WHERE state NOT IN ('expired','discard') "
                       "ORDER BY (acc+pers) DESC, rug ASC LIMIT ?", (limit,))

    def prune_terminal(self, older_than_min: int = PRUNE_TERMINAL_MIN) -> int:
        """Borra de fsm_state los NO-candidatos (expired/discard) ya añejos. NO
        toca decision_log (historial/aprendizaje intacto). Best-effort."""
        cutoff = int(time.time() * 1000) - older_than_min * 60_000
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM fsm_state WHERE state IN ('expired','discard') AND updated_at < ?",
                    (cutoff,))
                self._conn.commit()
                return cur.rowcount
        except Exception:
            logger.exception("prune_terminal failed")
            return 0

    def recent_decisions(self, limit: int = 80) -> list[dict]:
        return self._q("SELECT ts_ms,symbol,exchange,from_state,to_state,action,acc,pers,rug,seq "
                       "FROM decision_log ORDER BY id DESC LIMIT ?", (limit,))

    def status(self) -> dict:
        return {"mode": self.mode, "window_min": WINDOW_MIN, "thresholds":
                {"acc_min": ACC_MIN, "pers_min": PERS_MIN, "rug_max": RUG_MAX,
                 "confirm_ticks": CONFIRM_TICKS}, "states": self.state_counts()}

    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            out = [dict(r) for r in self._conn.execute(sql, args).fetchall()]
            self._conn.row_factory = None
        return out

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
        try:
            self._reader.close()
        except Exception:
            pass
