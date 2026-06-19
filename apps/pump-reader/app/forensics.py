"""Trade forensics — autopsia de cada operación (Fases 7+8+9).

Captura el CONTEXTO DE ENTRADA (score, confidence, volume_spike, imbalance,
spread, liquidity, top_book_share, chg 24h, accel) y, al cerrar, el CONTEXTO DE
SALIDA (precio, razón, pnl, duración, resultado). Une ambos en una fila por
trade → base histórica comparable de hard-stops vs ganadores, y estadística por
exchange (Fase 9).

NO toca estrategia/TP/SL/risk. Solo OBSERVA los trades que el bot ya hace en
paper. Escribe en el MISMO archivo SQLite del recorder (WAL permite varias
conexiones). Hooks en main.py, cada uno en try/except: nunca tumba un trade.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from .microstructure import MICRO_DB_PATH

logger = logging.getLogger("pump-reader.forensics")


class ForensicsStore:
    DDL = """
    CREATE TABLE IF NOT EXISTS trade_forensics (
        trade_id      TEXT PRIMARY KEY,
        user_id       TEXT,
        symbol        TEXT,
        exchange      TEXT,
        status        TEXT,        -- 'open' | 'closed'
        outcome       TEXT,        -- 'win' | 'loss' | 'scratch'
        entry_ts_ms   INTEGER,
        exit_ts_ms    INTEGER,
        hold_secs     REAL,
        entry_price   REAL,
        exit_price    REAL,
        pnl           REAL,
        pnl_pct       REAL,
        exit_reason   TEXT,
        score         INTEGER,
        confidence    INTEGER,
        volume_spike  REAL,
        imbalance     REAL,
        spread_pct    REAL,
        liquidity_usd REAL,
        top_book_share REAL,
        chg_24h       REAL,
        accel         REAL,
        classification TEXT,
        cluster       TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_forensics_exchange ON trade_forensics(exchange);
    CREATE INDEX IF NOT EXISTS ix_forensics_outcome  ON trade_forensics(outcome);
    CREATE INDEX IF NOT EXISTS ix_forensics_reason   ON trade_forensics(exit_reason);
    """

    def __init__(self, path: str = MICRO_DB_PATH) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.executescript(self.DDL)
        self._conn.commit()

    @staticmethod
    def _tid(uid: str, exchange: str, symbol: str, entry_ts_ms: int) -> str:
        return f"{uid}:{exchange}:{symbol}:{entry_ts_ms}"

    def record_entry(self, uid: str, candidate, pos, accel: float | None = None) -> None:
        """Inserta la fila al abrir (status='open'). El candidate trae el contexto
        de entrada real; pos trae precio/hora de entrada."""
        entry_ts_ms = int(pos.entry_at.timestamp() * 1000)
        tid = self._tid(uid, pos.exchange, pos.symbol, entry_ts_ms)
        row = (
            tid, uid, pos.symbol, pos.exchange, "open", None,
            entry_ts_ms, None, None, pos.entry_price, None, None, None, None,
            int(getattr(candidate, "pump_score", 0) or 0),
            int(getattr(candidate, "confidence_score", 0) or 0),
            float(getattr(candidate, "volume_spike", 0.0) or 0.0),
            float(getattr(candidate, "orderbook_imbalance", 0.0) or 0.0),
            float(getattr(candidate, "spread_pct", 0.0) or 0.0),
            float(getattr(candidate, "liquidity_usd", 0.0) or 0.0),
            float(getattr(candidate, "top_book_share", 0.0) or 0.0),
            float(getattr(candidate, "price_change_pct_24h", 0.0) or 0.0),
            float(accel) if accel is not None else None,
            getattr(candidate, "classification", "n/a"),
            getattr(candidate, "cluster", "n/a"),
        )
        cols = ("trade_id,user_id,symbol,exchange,status,outcome,entry_ts_ms,exit_ts_ms,"
                "hold_secs,entry_price,exit_price,pnl,pnl_pct,exit_reason,score,confidence,"
                "volume_spike,imbalance,spread_pct,liquidity_usd,top_book_share,chg_24h,accel,"
                "classification,cluster")
        ph = ",".join("?" * 25)
        with self._lock:
            self._conn.execute(f"INSERT OR IGNORE INTO trade_forensics ({cols}) VALUES ({ph})", row)
            self._conn.commit()

    def record_exit(self, uid: str, pos, exit_price: float, pnl: float,
                    pnl_pct: float, reason: str) -> None:
        """Finaliza la fila al cerrar (status='closed') uniendo el contexto de
        salida. Si por algún motivo no existe la fila de entrada, la crea mínima."""
        entry_ts_ms = int(pos.entry_at.timestamp() * 1000)
        exit_ts_ms = int(time.time() * 1000)
        hold = max(0.0, (exit_ts_ms - entry_ts_ms) / 1000)
        outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "scratch")
        tid = self._tid(uid, pos.exchange, pos.symbol, entry_ts_ms)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE trade_forensics SET status='closed', outcome=?, exit_ts_ms=?, "
                "hold_secs=?, exit_price=?, pnl=?, pnl_pct=?, exit_reason=? WHERE trade_id=?",
                (outcome, exit_ts_ms, hold, exit_price, round(pnl, 6), round(pnl_pct, 4), reason, tid),
            )
            if cur.rowcount == 0:
                # No hubo fila de entrada (p.ej. posición restaurada tras reinicio):
                # crea una fila cerrada con lo que se tiene (contexto de entrada nulo).
                self._conn.execute(
                    "INSERT OR IGNORE INTO trade_forensics "
                    "(trade_id,user_id,symbol,exchange,status,outcome,entry_ts_ms,exit_ts_ms,"
                    "hold_secs,entry_price,exit_price,pnl,pnl_pct,exit_reason,score,classification) "
                    "VALUES (?,?,?,?,'closed',?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, uid, pos.symbol, pos.exchange, outcome, entry_ts_ms, exit_ts_ms, hold,
                     pos.entry_price, exit_price, round(pnl, 6), round(pnl_pct, 4), reason,
                     int(getattr(pos, "pump_score", 0) or 0), getattr(pos, "classification", "n/a")),
                )
            self._conn.commit()

    # --- consultas (read-only) ----------------------------------------------
    def _q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in self._conn.execute(sql, args).fetchall()]
            self._conn.row_factory = None
        return rows

    def exchange_stats(self) -> list[dict]:
        """Fase 9: calidad por exchange con ranking. Solo trades cerrados."""
        rows = self._q("""
            SELECT exchange,
                   COUNT(*) AS trades,
                   SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN exit_reason='hard_stop' THEN 1 ELSE 0 END) AS hard_stops,
                   SUM(CASE WHEN exit_reason='dump'      THEN 1 ELSE 0 END) AS dumps,
                   AVG(pnl_pct) AS avg_pnl_pct,
                   SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END) AS gross_profit,
                   SUM(CASE WHEN pnl<0 THEN -pnl ELSE 0 END) AS gross_loss
            FROM trade_forensics WHERE status='closed'
            GROUP BY exchange
        """)
        for r in rows:
            t = r["trades"] or 0
            r["win_rate"] = round((r["wins"] or 0) / t * 100, 1) if t else 0.0
            gl = r["gross_loss"] or 0.0
            r["profit_factor"] = round((r["gross_profit"] or 0.0) / gl, 2) if gl > 0 else None
            r["avg_pnl_pct"] = round(r["avg_pnl_pct"] or 0.0, 3)
        # Ranking: peor primero (más hard_stops y PF más bajo = más problemático).
        rows.sort(key=lambda r: (r["profit_factor"] if r["profit_factor"] is not None else 0,
                                 -(r["hard_stops"] or 0)))
        return rows

    def by_outcome(self, outcome: str, limit: int = 50) -> list[dict]:
        return self._q(
            "SELECT * FROM trade_forensics WHERE status='closed' AND outcome=? "
            "ORDER BY exit_ts_ms DESC LIMIT ?", (outcome, limit))

    def hard_stops(self, limit: int = 50) -> list[dict]:
        return self._q(
            "SELECT * FROM trade_forensics WHERE status='closed' AND exit_reason IN ('hard_stop','dump') "
            "ORDER BY exit_ts_ms DESC LIMIT ?", (limit,))

    def compare_winners_vs_hardstops(self) -> dict:
        """Fase 8: medias de cada feature de entrada, ganadores vs hard-stops."""
        feats = ["score", "confidence", "volume_spike", "imbalance", "spread_pct",
                 "liquidity_usd", "top_book_share", "chg_24h", "accel"]
        out: dict[str, dict] = {}
        for label, cond in (("winners", "outcome='win'"),
                            ("hard_stops", "exit_reason IN ('hard_stop','dump')")):
            sel = ",".join(f"AVG({f}) AS {f}" for f in feats)
            r = self._q(f"SELECT COUNT(*) AS n,{sel} FROM trade_forensics WHERE status='closed' AND {cond}")
            out[label] = r[0] if r else {}
        return out

    def stats(self) -> dict:
        r = self._q("SELECT COUNT(*) AS total, "
                    "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_, "
                    "SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) AS closed_, "
                    "SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS wins, "
                    "SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses "
                    "FROM trade_forensics")
        return r[0] if r else {}

    def all_rows(self) -> list[dict]:
        return self._q("SELECT * FROM trade_forensics ORDER BY entry_ts_ms")

    def optimize_tp_sl(self) -> dict:
        """Analiza las últimas 50 operaciones y sugiere nuevos TP/SL."""
        import statistics
        rows = self._q("SELECT pnl_pct, exit_reason, outcome FROM trade_forensics "
                       "WHERE status='closed' ORDER BY exit_ts_ms DESC LIMIT 50")
        if len(rows) < 20:
            return {"tp": None, "sl": None, "reason": "insufficient data"}

        wins = [r["pnl_pct"] for r in rows if r["outcome"] == "win"]
        losses = [abs(r["pnl_pct"]) for r in rows if r["outcome"] == "loss"]

        if not wins or not losses:
            return {"tp": None, "sl": None, "reason": "no trades"}

        avg_win = statistics.mean(wins)
        avg_loss = statistics.mean(losses)

        new_tp = avg_win * 1.2
        new_sl = avg_loss * 1.2

        new_tp = max(15, min(50, round(new_tp, 1)))
        new_sl = max(1.5, min(5, round(new_sl, 1)))

        return {
            "tp": new_tp,
            "sl": new_sl,
            "avg_win": round(avg_win, 1),
            "avg_loss": round(avg_loss, 1),
            "n_wins": len(wins),
            "n_losses": len(losses),
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
