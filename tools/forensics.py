#!/usr/bin/env python3
"""CLI de autopsia de trades (Fases 7+8+9). Solo lectura.

  python tools/forensics.py summary           # totales abiertos/cerrados/win/loss
  python tools/forensics.py exchange           # ranking de calidad por exchange
  python tools/forensics.py compare            # ganadores vs hard-stops (medias de entrada)
  python tools/forensics.py hardstops --limit 30
  python tools/forensics.py winners   --limit 30
  python tools/forensics.py export trades.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "pump-reader"))
from app.forensics import ForensicsStore  # noqa: E402


def _print(rows: list[dict]) -> None:
    if not rows:
        print("(sin datos)"); return
    cols = list(rows[0].keys())
    print("\t".join(cols))
    for r in rows:
        print("\t".join(f"{r[c]:.6g}" if isinstance(r[c], float) else str(r[c]) for c in cols))
    print(f"\n{len(rows)} filas")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("PUMP_MICRO_DB"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary")
    sub.add_parser("exchange")
    sub.add_parser("compare")
    ph = sub.add_parser("hardstops"); ph.add_argument("--limit", type=int, default=30)
    pw = sub.add_parser("winners");   pw.add_argument("--limit", type=int, default=30)
    pe = sub.add_parser("export");    pe.add_argument("out")
    a = ap.parse_args()
    st = ForensicsStore(a.db) if a.db else ForensicsStore()

    if a.cmd == "summary":
        for k, v in st.stats().items():
            print(f"{k:10}: {v}")
    elif a.cmd == "exchange":
        _print(st.exchange_stats())
    elif a.cmd == "compare":
        cmp = st.compare_winners_vs_hardstops()
        feats = ["n", "score", "confidence", "volume_spike", "imbalance", "spread_pct",
                 "liquidity_usd", "top_book_share", "chg_24h", "accel"]
        print(f"{'feature':16} {'winners':>14} {'hard_stops':>14}")
        for f in feats:
            w = cmp.get("winners", {}).get(f); h = cmp.get("hard_stops", {}).get(f)
            ws = f"{w:.4g}" if isinstance(w, float) else str(w)
            hs = f"{h:.4g}" if isinstance(h, float) else str(h)
            print(f"{f:16} {ws:>14} {hs:>14}")
    elif a.cmd == "hardstops":
        _print(st.hard_stops(a.limit))
    elif a.cmd == "winners":
        _print(st.by_outcome("win", a.limit))
    elif a.cmd == "export":
        rows = st.all_rows()
        if rows:
            with open(a.out, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader(); w.writerows(rows)
        print(f"exportadas {len(rows)} filas -> {a.out}")
    st.close()


if __name__ == "__main__":
    main()
