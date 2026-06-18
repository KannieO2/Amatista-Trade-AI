#!/usr/bin/env python3
"""CLI para consultar / exportar / reconstruir la DB de microestructura (FASE 1).

Solo LECTURA (export incluido). No toca el bot. Ejemplos:

  python tools/micro.py stats
  python tools/micro.py symbols --since 180
  python tools/micro.py recent PHA/USDT mexc --minutes 60
  python tools/micro.py reconstruct PHA/USDT mexc "2026-06-18T03:10:00+00:00" --before 180 --after 30
  python tools/micro.py reconstruct PHA/USDT mexc 1750000000000 --before 60
  python tools/micro.py export out.csv --since 1440
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "pump-reader"))
from app.microstructure import MicroStore, iso  # noqa: E402

COLS = ["ts_ms", "symbol", "exchange", "last_price", "volume", "volume_delta",
        "spread_pct", "imbalance", "liquidity_usd", "bid_depth", "ask_depth",
        "top_book_share", "velocity"]


def _to_ms(s: str) -> int:
    """Acepta epoch ms, epoch s, o ISO-8601."""
    s = s.strip()
    if s.isdigit():
        v = int(s)
        return v if v > 10_000_000_000 else v * 1000   # heurística s vs ms
    dt = datetime.fromisoformat(s)
    return int(dt.timestamp() * 1000)


def _print_rows(rows: list[dict]) -> None:
    if not rows:
        print("(sin datos)")
        return
    print("\t".join(["time(UTC)"] + COLS[1:]))
    for r in rows:
        line = [iso(r["ts_ms"])] + [f'{r[c]:.6g}' if isinstance(r[c], float) else str(r[c]) for c in COLS[1:]]
        print("\t".join(line))
    print(f"\n{len(rows)} filas")


def main() -> None:
    ap = argparse.ArgumentParser(description="Consulta de microestructura (FASE 1)")
    ap.add_argument("--db", default=os.getenv("PUMP_MICRO_DB"), help="ruta a la DB (def: la del bot)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats")

    ps = sub.add_parser("symbols"); ps.add_argument("--since", type=int, default=None, help="minutos")

    pr = sub.add_parser("recent")
    pr.add_argument("symbol"); pr.add_argument("exchange"); pr.add_argument("--minutes", type=int, default=60)

    prc = sub.add_parser("reconstruct")
    prc.add_argument("symbol"); prc.add_argument("exchange")
    prc.add_argument("pump_time", help="epoch ms/s o ISO-8601 del pump")
    prc.add_argument("--before", type=int, default=180); prc.add_argument("--after", type=int, default=30)

    pe = sub.add_parser("export")
    pe.add_argument("out"); pe.add_argument("--since", type=int, default=None, help="minutos")

    a = ap.parse_args()
    store = MicroStore(a.db) if a.db else MicroStore()

    if a.cmd == "stats":
        s = store.stats()
        print(f"filas        : {s['rows']:,}")
        print(f"símbolos     : {s['symbols']}")
        print(f"primer ts    : {iso(s['first_ts_ms'])}")
        print(f"último ts    : {iso(s['last_ts_ms'])}")
        print(f"tamaño DB    : {s['db_mb']} MB")
        print(f"ruta         : {s['path']}")
    elif a.cmd == "symbols":
        for sym, ex, last in sorted(store.distinct_symbols(a.since), key=lambda x: -x[2]):
            print(f"{sym:18} {ex:9} último {iso(last)}")
    elif a.cmd == "recent":
        _print_rows(store.recent(a.symbol, a.exchange, a.minutes))
    elif a.cmd == "reconstruct":
        rows = store.reconstruct(a.symbol, a.exchange, _to_ms(a.pump_time), a.before, a.after)
        _print_rows(rows)
    elif a.cmd == "export":
        n = store.export_csv(a.out, a.since)
        print(f"exportadas {n:,} filas -> {a.out}")
    store.close()


if __name__ == "__main__":
    main()
