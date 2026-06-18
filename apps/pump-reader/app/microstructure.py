"""Microstructure recorder — FASE 1 (solo recolección de datos).

Objetivo único: grabar, cada minuto, la microestructura del orderbook de los
símbolos observados, en una base local persistente (SQLite), para luego poder
RECONSTRUIR los 15/30/60/180 minutos previos a cualquier pump y validar/refutar
la hipótesis: "¿existen señales observables de acumulación antes del pump?".

NO toca scanner, score, entradas, salidas, TP, SL, hard stop ni risk. Es
puramente aditivo: un loop de fondo independiente + un archivo SQLite local.
El bot sigue operando exactamente igual con o sin este módulo.

Fuentes de datos: las MISMAS que ya usa el bot (CCXT público, sin claves) —
OHLCV 1m (precio + volumen) y order book (spread/imbalance/liquidez/profundidad).
Todas las métricas del libro se calculan AQUÍ para no modificar scanner.py.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

import ccxt.async_support as ccxt

logger = logging.getLogger("pump-reader.microstructure")

# --- configuración (todo por variable de entorno, con defaults sensatos) ------
OBSERVE_INTERVAL_SECONDS = int(os.getenv("PUMP_OBSERVE_INTERVAL_SECONDS", "60"))
# Cuánto se sigue observando un símbolo DESPUÉS de la última vez que apareció
# como candidato. 180 min garantiza poder reconstruir 180' antes de un pump.
OBSERVE_RETENTION_MINUTES = int(os.getenv("PUMP_OBSERVE_RETENTION_MINUTES", "180"))
# Tope de símbolos observados por minuto (acota las llamadas a la API).
MAX_OBSERVE_SYMBOLS = int(os.getenv("PUMP_OBSERVE_MAX_SYMBOLS", "80"))
OBSERVE_CONCURRENCY = int(os.getenv("PUMP_OBSERVE_CONCURRENCY", "5"))
# Banda de profundidad alrededor del mid para medir liquidez (igual que scanner).
DEPTH_BAND_PCT = float(os.getenv("PUMP_OBSERVE_DEPTH_BAND_PCT", "0.02"))
ORDERBOOK_LIMIT = int(os.getenv("PUMP_OBSERVE_ORDERBOOK_LIMIT", "50"))
BUFFER_MAXLEN = int(os.getenv("PUMP_OBSERVE_BUFFER_MAXLEN", "200"))
VELOCITY_LOOKBACK = int(os.getenv("PUMP_OBSERVE_VELOCITY_LOOKBACK", "5"))

_DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "data" / "microstructure.db")
MICRO_DB_PATH = os.getenv("PUMP_MICRO_DB", _DEFAULT_DB)


@dataclass
class MicroSnapshot:
    """Una observación de un símbolo en un minuto. Todos los campos pedidos."""
    ts_ms: int            # epoch ms UTC
    symbol: str
    exchange: str
    last_price: float
    volume: float         # volumen base de la última vela 1m cerrada
    volume_delta: float   # volume - volumen previo (de la serie en memoria)
    spread_pct: float
    imbalance: float      # bid_band / (bid_band + ask_band)
    liquidity_usd: float  # bid_depth + ask_depth (dentro de la banda)
    bid_depth: float      # notional bid dentro de la banda
    ask_depth: float      # notional ask dentro de la banda
    top_book_share: float # notional top-3 bids / notional total bids
    velocity: float       # volume / media(volúmenes previos)


@dataclass
class SymbolSeries:
    """Buffer rolling en memoria de un símbolo (para deltas y velocity)."""
    key: str
    buf: deque = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.buf is None:
            self.buf = deque(maxlen=BUFFER_MAXLEN)

    def add(self, s: MicroSnapshot) -> None:
        self.buf.append(s)

    def window(self, n: int) -> list[MicroSnapshot]:
        return list(self.buf)[-n:]

    def last(self) -> MicroSnapshot | None:
        return self.buf[-1] if self.buf else None

    def recent_volumes(self, n: int) -> list[float]:
        return [s.volume for s in list(self.buf)[-n:]]


# ============================ ALMACENAMIENTO ================================
class MicroStore:
    """SQLite append-only. Elegido sobre Parquet/DuckDB porque:
      - sqlite3 es stdlib (cero dependencias nuevas → más robusto en la VM).
      - Un solo archivo, transaccional, append-friendly (1 insert/min/símbolo).
      - WAL permite leer (CLI/análisis) mientras el loop escribe.
      - Export trivial a CSV/Parquet/DuckDB para el análisis offline de Fase 2.
    Parquet no tiene buen append incremental; DuckDB añade dependencia. SQLite
    ingesta; luego se exporta a columnar para analizar.
    """

    DDL = """
    CREATE TABLE IF NOT EXISTS micro_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_ms         INTEGER NOT NULL,
        symbol        TEXT    NOT NULL,
        exchange      TEXT    NOT NULL,
        last_price    REAL,
        volume        REAL,
        volume_delta  REAL,
        spread_pct    REAL,
        imbalance     REAL,
        liquidity_usd REAL,
        bid_depth     REAL,
        ask_depth     REAL,
        top_book_share REAL,
        velocity      REAL
    );
    CREATE INDEX IF NOT EXISTS ix_micro_sym_ts ON micro_snapshots(symbol, exchange, ts_ms);
    CREATE INDEX IF NOT EXISTS ix_micro_ts     ON micro_snapshots(ts_ms);
    """

    COLS = ["ts_ms", "symbol", "exchange", "last_price", "volume", "volume_delta",
            "spread_pct", "imbalance", "liquidity_usd", "bid_depth", "ask_depth",
            "top_book_share", "velocity"]

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

    def insert_batch(self, snaps: list[MicroSnapshot]) -> int:
        if not snaps:
            return 0
        rows = [tuple(getattr(s, c) for c in self.COLS) for s in snaps]
        ph = ",".join("?" * len(self.COLS))
        sql = f"INSERT INTO micro_snapshots ({','.join(self.COLS)}) VALUES ({ph})"
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()
        return len(rows)

    def recent(self, symbol: str, exchange: str, minutes: int) -> list[dict]:
        cutoff = int(time.time() * 1000) - minutes * 60_000
        return self._query(
            "SELECT * FROM micro_snapshots WHERE symbol=? AND exchange=? AND ts_ms>=? ORDER BY ts_ms",
            (symbol.upper(), exchange.lower(), cutoff),
        )

    def reconstruct(self, symbol: str, exchange: str, pump_ts_ms: int,
                    before_minutes: int = 180, after_minutes: int = 30) -> list[dict]:
        lo = pump_ts_ms - before_minutes * 60_000
        hi = pump_ts_ms + after_minutes * 60_000
        return self._query(
            "SELECT * FROM micro_snapshots WHERE symbol=? AND exchange=? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
            (symbol.upper(), exchange.lower(), lo, hi),
        )

    def distinct_symbols(self, since_minutes: int | None = None) -> list[tuple[str, str, int]]:
        sql = "SELECT symbol, exchange, MAX(ts_ms) FROM micro_snapshots"
        args: tuple = ()
        if since_minutes is not None:
            sql += " WHERE ts_ms>=?"
            args = (int(time.time() * 1000) - since_minutes * 60_000,)
        sql += " GROUP BY symbol, exchange"
        with self._lock:
            cur = self._conn.execute(sql, args)
            return [(r[0], r[1], r[2]) for r in cur.fetchall()]

    def stats(self) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*), MIN(ts_ms), MAX(ts_ms), COUNT(DISTINCT symbol||'@'||exchange) FROM micro_snapshots"
            )
            n, lo, hi, syms = cur.fetchone()
        size = Path(self.path).stat().st_size if Path(self.path).exists() else 0
        return {"rows": n or 0, "symbols": syms or 0,
                "first_ts_ms": lo, "last_ts_ms": hi,
                "db_bytes": size, "db_mb": round(size / 1e6, 2), "path": self.path}

    def export_csv(self, out_path: str, since_minutes: int | None = None) -> int:
        sql = f"SELECT {','.join(self.COLS)} FROM micro_snapshots"
        args: tuple = ()
        if since_minutes is not None:
            sql += " WHERE ts_ms>=?"
            args = (int(time.time() * 1000) - since_minutes * 60_000,)
        sql += " ORDER BY ts_ms"
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(self.COLS)
            w.writerows(rows)
        return len(rows)

    def _query(self, sql: str, args: tuple) -> list[dict]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            cur = self._conn.execute(sql, args)
            out = [dict(r) for r in cur.fetchall()]
            self._conn.row_factory = None
        return out

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# ============================ MÉTRICAS DEL LIBRO ============================
def _book_metrics(order_book: dict) -> dict | None:
    """Calcula spread/imbalance/liquidez/profundidad/top_share desde el libro
    público (las mismas semánticas que scanner, pero locales — no toca scanner)."""
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []
    if not bids or not asks:
        return None
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2
    if mid <= 0 or best_ask <= 0:
        return None
    low = mid * (1 - DEPTH_BAND_PCT)
    high = mid * (1 + DEPTH_BAND_PCT)
    bid_depth = sum(pr * am for pr, am in bids if pr >= low)
    ask_depth = sum(pr * am for pr, am in asks if pr <= high)
    total = bid_depth + ask_depth
    imbalance = bid_depth / total if total > 0 else 0.5
    spread_pct = (best_ask - best_bid) / best_ask * 100
    bid_notional = [pr * am for pr, am in bids]
    total_bid = sum(bid_notional)
    top3 = sum(sorted(bid_notional, reverse=True)[:3])
    top_share = top3 / total_bid if total_bid > 0 else 1.0
    return {"spread_pct": round(spread_pct, 4), "imbalance": round(imbalance, 4),
            "liquidity_usd": round(total, 2), "bid_depth": round(bid_depth, 2),
            "ask_depth": round(ask_depth, 2), "top_book_share": round(top_share, 4)}


# ============================ OBSERVADOR ===================================
class MicroObserver:
    """Mantiene la watchlist persistente de observación + el loop de 1 minuto.

    La watchlist se alimenta de los candidatos del scan (note_candidates), pero
    NO los suelta de inmediato: cada símbolo se sigue observando durante
    OBSERVE_RETENTION_MINUTES tras su última aparición, para tener pre-historia.
    """

    def __init__(self, store: MicroStore | None = None) -> None:
        self.store = store or MicroStore()
        self.watch: dict[str, float] = {}            # "exchange:symbol" -> last_seen_ts (epoch s)
        self.series: dict[str, SymbolSeries] = {}    # "exchange:symbol" -> buffer
        self._clients: dict[str, ccxt.Exchange] = {}

    # --- watchlist -----------------------------------------------------------
    def note_candidates(self, items: list[tuple[str, str]]) -> None:
        """Marca símbolos vistos en el último scan. items = [(exchange, symbol)]."""
        now = time.time()
        for exchange, symbol in items:
            self.watch[f"{exchange.lower()}:{symbol.upper()}"] = now

    def warm_start(self) -> None:
        """Tras un reinicio, re-siembra la watchlist con los símbolos observados
        en la ventana de retención (continuidad sin perder pre-historia)."""
        try:
            seen = self.store.distinct_symbols(since_minutes=OBSERVE_RETENTION_MINUTES)
        except Exception:
            logger.exception("warm_start failed")
            return
        now = time.time()
        for symbol, exchange, last_ts_ms in seen:
            self.watch[f"{exchange.lower()}:{symbol.upper()}"] = min(now, last_ts_ms / 1000)

    def _active(self) -> list[tuple[str, str]]:
        cutoff = time.time() - OBSERVE_RETENTION_MINUTES * 60
        active = [(k, ts) for k, ts in self.watch.items() if ts >= cutoff]
        # Limpia los expirados.
        for k in [k for k, ts in self.watch.items() if ts < cutoff]:
            del self.watch[k]
        active.sort(key=lambda x: -x[1])           # los más recientes primero
        out = []
        for key, _ in active[:MAX_OBSERVE_SYMBOLS]:
            exchange, symbol = key.split(":", 1)
            out.append((exchange, symbol))
        return out

    # --- fetching ------------------------------------------------------------
    async def _client(self, exchange_id: str):
        client = self._clients.get(exchange_id)
        if client is None:
            if not hasattr(ccxt, exchange_id):
                return None
            client = getattr(ccxt, exchange_id)({"enableRateLimit": True})
            self._clients[exchange_id] = client
        return client

    async def _snap_symbol(self, exchange: str, symbol: str,
                           sem: asyncio.Semaphore) -> MicroSnapshot | None:
        client = await self._client(exchange)
        if client is None:
            return None
        async with sem:
            try:
                ohlcv = await client.fetch_ohlcv(symbol, timeframe="1m", limit=3)
                ob = await client.fetch_order_book(symbol, limit=ORDERBOOK_LIMIT)
            except Exception:
                return None
        rows = [r for r in ohlcv if r and r[5] is not None]
        if len(rows) < 2:
            return None
        last_price = float(rows[-1][4] or 0.0)     # close de la vela más reciente
        volume = float(rows[-2][5] or 0.0)         # volumen de la última 1m CERRADA
        bm = _book_metrics(ob)
        if bm is None or last_price <= 0:
            return None

        key = f"{exchange}:{symbol}"
        ser = self.series.setdefault(key, SymbolSeries(key=key))
        prev = ser.last()
        volume_delta = round(volume - prev.volume, 6) if prev else 0.0
        prior_vols = ser.recent_volumes(VELOCITY_LOOKBACK)
        base = mean(prior_vols) if prior_vols else 0.0
        velocity = round(volume / base, 4) if base > 0 else 1.0

        snap = MicroSnapshot(
            ts_ms=int(time.time() * 1000), symbol=symbol, exchange=exchange,
            last_price=last_price, volume=volume, volume_delta=volume_delta,
            spread_pct=bm["spread_pct"], imbalance=bm["imbalance"],
            liquidity_usd=bm["liquidity_usd"], bid_depth=bm["bid_depth"],
            ask_depth=bm["ask_depth"], top_book_share=bm["top_book_share"],
            velocity=velocity,
        )
        ser.add(snap)
        return snap

    async def observe_once(self) -> int:
        """Un barrido: toma un MicroSnapshot de cada símbolo activo y persiste el
        lote. Devuelve cuántas filas se grabaron. Nunca lanza (best-effort)."""
        targets = self._active()
        if not targets:
            return 0
        sem = asyncio.Semaphore(OBSERVE_CONCURRENCY)
        results = await asyncio.gather(
            *(self._snap_symbol(e, s, sem) for e, s in targets),
            return_exceptions=True,
        )
        snaps = [r for r in results if isinstance(r, MicroSnapshot)]
        if not snaps:
            return 0
        # Escritura SQLite fuera del event loop.
        return await asyncio.to_thread(self.store.insert_batch, snaps)

    async def close(self) -> None:
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()
        self.store.close()

    def status(self) -> dict:
        return {
            "watching": len(self.watch),
            "active_cap": MAX_OBSERVE_SYMBOLS,
            "interval_s": OBSERVE_INTERVAL_SECONDS,
            "retention_min": OBSERVE_RETENTION_MINUTES,
            **self.store.stats(),
        }


def iso(ts_ms: int | None) -> str | None:
    return datetime.fromtimestamp(ts_ms / 1000, UTC).isoformat() if ts_ms else None
