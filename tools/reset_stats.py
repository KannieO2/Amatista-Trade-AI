#!/usr/bin/env python3
"""Reset de stats del bot para observar la operación desde cero.

Borra (irreversible) las estadísticas de TRADING y de la máquina de estados, para
ver cómo opera el rediseño (Fase 2) sobre una pizarra limpia. Por defecto:

  - SQLite (microstructure.db): trade_forensics, fsm_state, decision_log
  - Supabase (scoped a un user_id): managed_positions, exit_events,
    equity_history, learning_records, account_snapshots

CONSERVA micro_snapshots por defecto (es la observación cruda que la FSM NECESITA
para puntuar; borrarla ciega al sistema que quieres observar). Usa --wipe-micro
para borrarla también.

Multi-tenant: por defecto SOLO toca la cuenta --user (owner). --all-users borra
de TODAS las cuentas (peligroso en DB compartida).

  python tools/reset_stats.py --dry-run            # muestra qué borraría
  python tools/reset_stats.py --yes                # ejecuta (owner, conserva micro)
  python tools/reset_stats.py --yes --wipe-micro   # + borra micro_snapshots
  python tools/reset_stats.py --yes --all-users    # + todas las cuentas
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "pump-reader"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
MICRO_DB = os.getenv("PUMP_MICRO_DB") or os.path.join(
    os.path.dirname(__file__), "..", "apps", "pump-reader", "data", "microstructure.db")

SQLITE_TABLES = ["trade_forensics", "fsm_state", "decision_log"]
SUPA_TABLES = ["managed_positions", "exit_events", "equity_history",
               "learning_records", "account_snapshots"]
# Columna PK presente en cada tabla (para el filtro "todas las filas" sin asumir 'id').
SUPA_ALLROW_COL = {"managed_positions": "key"}  # el resto usa 'id'


def sqlite_counts(con) -> dict:
    out = {}
    for t in SQLITE_TABLES + (["micro_snapshots"]):
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = "(no existe)"
    return out


def wipe_sqlite(con, wipe_micro: bool) -> None:
    tables = list(SQLITE_TABLES) + (["micro_snapshots"] if wipe_micro else [])
    for t in tables:
        try:
            con.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError as e:
            print(f"  skip {t}: {e}")
    con.commit()
    con.execute("VACUUM")
    con.commit()


def wipe_supabase(user: str | None) -> None:
    if not (SUPABASE_URL and SUPABASE_KEY):
        print("  Supabase no configurado — omitido.")
        return
    import httpx
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
         "Prefer": "return=minimal"}
    with httpx.Client(base_url=f"{SUPABASE_URL}/rest/v1", timeout=15.0) as c:
        for t in SUPA_TABLES:
            # PostgREST exige un filtro para DELETE. Scoped por user_id salvo
            # --all-users (filtro 'id not null' = todas las filas).
            col = SUPA_ALLROW_COL.get(t, "id")
            params = {"user_id": f"eq.{user}"} if user else {col: "not.is.null"}
            try:
                r = c.request("DELETE", f"/{t}", headers=h, params=params)
                if r.status_code >= 400:
                    print(f"  {t}: HTTP {r.status_code} {r.text[:120]}")
                else:
                    print(f"  {t}: borrado (scope={'TODOS' if not user else user})")
            except Exception as e:
                print(f"  {t}: error {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="ejecuta de verdad")
    ap.add_argument("--dry-run", action="store_true", help="solo muestra conteos")
    ap.add_argument("--user", default="owner", help="cuenta a resetear (default owner)")
    ap.add_argument("--all-users", action="store_true", help="TODAS las cuentas (peligroso)")
    ap.add_argument("--wipe-micro", action="store_true", help="borra también micro_snapshots")
    a = ap.parse_args()

    con = sqlite3.connect(MICRO_DB)
    print("== ANTES ==", sqlite_counts(con))
    user = None if a.all_users else a.user

    if a.dry_run or not a.yes:
        print("\nDRY-RUN. Borraría:")
        print(f"  SQLite: {SQLITE_TABLES}" + (" + micro_snapshots" if a.wipe_micro else " (micro_snapshots CONSERVADA)"))
        print(f"  Supabase: {SUPA_TABLES}  scope={'TODOS' if a.all_users else a.user}")
        print("\nEjecuta con --yes para aplicar.")
        con.close()
        return

    print("\nBorrando SQLite…")
    wipe_sqlite(con, a.wipe_micro)
    print("Borrando Supabase…")
    wipe_supabase(user)
    print("\n== DESPUÉS ==", sqlite_counts(con))
    con.close()
    print("\nReset hecho. Reinicia el bot para balances/posiciones en memoria a cero.")


if __name__ == "__main__":
    main()
