#!/usr/bin/env python3
"""Reset TradeOS AI trading state WITHOUT forgetting what it learned.

What it does (non-destructive — nothing is deleted):
  - Marks every OPEN managed position as closed in Supabase, so on the next
    start the bot rebuilds a clean slate (0 open) and the 4-position cap is fresh.

What it KEEPS (the "learning"):
  - learning_records  (every alert/entry/exit outcome the loop learned from)
  - alerts            (signal history → precision/recall, MFE/MAE)
  - candidates        (last scan snapshots)
  - token_market / allocation

Run on the server (venv has httpx + dotenv):
  ~/tradeos/apps/pump-reader/.venv/bin/python ~/tradeos/deploy/reset_state.py
Then restart the service so the in-memory state is rebuilt clean:
  sudo systemctl restart pumpreader
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# repo-root .env (deploy/ -> repo root)
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

import os

URL = os.getenv("SUPABASE_URL", "").rstrip("/")
KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

if not URL or not KEY:
    print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY missing in .env — nothing to reset.")
    sys.exit(1)

H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
REST = f"{URL}/rest/v1"


def count(table: str, query: str = "") -> int:
    try:
        r = httpx.get(f"{REST}/{table}?{query}", headers={**H, "Prefer": "count=exact",
                      "Range": "0-0"}, timeout=15)
        cr = r.headers.get("content-range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception as exc:
        print(f"  (count {table} failed: {exc})")
        return -1


def main() -> int:
    open_before = count("managed_positions", "closed=eq.false")
    learned = count("learning_records")
    alerts = count("alerts")
    print(f"Antes: {open_before} posiciones abiertas")
    print(f"Aprendizaje preservado: {learned} learning_records · {alerts} alerts")

    if open_before == 0:
        print("Nada que cerrar — estado ya limpio.")
    else:
        r = httpx.patch(
            f"{REST}/managed_positions?closed=eq.false",
            headers={**H, "Prefer": "return=minimal"},
            json={"closed": True},
            timeout=30,
        )
        if r.status_code not in (200, 204):
            print(f"ERROR cerrando posiciones: {r.status_code} {r.text[:200]}")
            return 1
        open_after = count("managed_positions", "closed=eq.false")
        print(f"Después: {open_after} posiciones abiertas (cerradas {open_before - open_after})")

    print("\nReset OK. Reinicia el servicio:  sudo systemctl restart pumpreader")
    print("(El aprendizaje NO se tocó.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
