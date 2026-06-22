"""Edge Lab — backtest APARTE (no toca el bot). Busca el filtro de ENTRADA que
vuelve rentable la población REAL de trades ya ejecutados (trade_forensics).

Honestidad: solo filtra por features conocidas EN LA ENTRADA (volume_spike,
liquidity, spread, imbalance, chg_24h, score, top_book_share). NO usa exit_reason
ni pnl para filtrar (eso sería trampa con lookahead). El P&L del subconjunto es la
suma de los pnl REALES de los trades que pasarían el filtro. Sin re-simular salidas.

Uso: python sims/edge_lab.py
"""
from __future__ import annotations
import sqlite3, statistics as st, itertools, os

DB = os.path.join(os.path.dirname(__file__), "..", "data", "microstructure.db")


def load() -> list[dict]:
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM trade_forensics WHERE status='closed' AND pnl IS NOT NULL")]
    c.close()
    return rows


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "pnl": 0.0, "wr": 0.0, "pf": 0.0, "exp": 0.0}
    pnl = sum(r["pnl"] for r in rows)
    wins = [r["pnl"] for r in rows if r["pnl"] > 0]
    losses = [r["pnl"] for r in rows if r["pnl"] < 0]
    gp = sum(wins); gl = abs(sum(losses))
    return {"n": n, "pnl": round(pnl, 2), "wr": len(wins) / n,
            "pf": (gp / gl) if gl > 0 else float("inf"),
            "exp": round(pnl / n, 3)}


def fline(label, s):
    return (f"{label:30} n={s['n']:4} pnl=${s['pnl']:9.2f} "
            f"wr={s['wr']:5.0%} pf={s['pf']:.2f} exp=${s['exp']:.3f}")


def passes(r, vol, score, spread, chg_max, liq_lo, liq_hi, top_max, imb_lo):
    return (((r["volume_spike"] or 0) >= vol)
            and ((r["score"] or 0) >= score)
            and ((r["spread_pct"] or 99) <= spread)
            and ((r["chg_24h"] or 0) <= chg_max)
            and (liq_lo <= (r["liquidity_usd"] or 0) <= liq_hi)
            and ((r["top_book_share"] or 1) <= top_max)
            and ((r["imbalance"] or 0) >= imb_lo))


def grid_search(R: list[dict], min_trades: int) -> tuple[dict, dict]:
    VOL   = [0, 2, 3, 4, 6, 8]
    SCORE = [0, 50, 60, 65, 70]
    SPRED = [0.25, 0.5, 0.99]
    CHG   = [12, 25, 50, 999]
    LIQHI = [3e5, 1e6, 1e9]
    TOP   = [0.6, 0.7, 1.0]
    IMB   = [0.0, 0.6, 0.65]
    best, best_params = None, None
    for vol, sc, sp, chg, lhi, top, imb in itertools.product(VOL, SCORE, SPRED, CHG, LIQHI, TOP, IMB):
        sub = [r for r in R if passes(r, vol, sc, sp, chg, 1.5e4, lhi, top, imb)]
        if len(sub) < min_trades:
            continue
        s = stats(sub)
        # objetivo: P&L total (rentabilidad absoluta), desempate por expectancy
        key = (s["pnl"], s["exp"])
        if best is None or key > (best["pnl"], best["exp"]):
            best, best_params = s, dict(vol=vol, score=sc, spread=sp, chg_max=chg,
                                        liq_hi=lhi, top_max=top, imb_lo=imb)
    return best, best_params


def main():
    R = load()
    print("=" * 78)
    print("EDGE LAB — backtest sobre trades REALES (trade_forensics)")
    print("=" * 78)
    base = stats(R)
    print(fline("BASELINE (todos los trades)", base))
    print()

    # --- single-feature sweeps (para entender el edge) ---
    print("-- volume_spike >= X --")
    for x in (2, 3, 4, 6, 8):
        print(fline(f"  vol>={x}", stats([r for r in R if (r["volume_spike"] or 0) >= x])))
    print("-- score >= X --")
    for x in (50, 60, 65, 70):
        print(fline(f"  score>={x}", stats([r for r in R if (r["score"] or 0) >= x])))
    print("-- chg_24h <= X (anti-entrar-tarde) --")
    for x in (12, 25, 50):
        print(fline(f"  chg<={x}", stats([r for r in R if (r["chg_24h"] or 0) <= x])))
    print("-- vol>=6 AND score>=65 --")
    print(fline("  combo", stats([r for r in R
                if (r["volume_spike"] or 0) >= 6 and (r["score"] or 0) >= 65])))
    print()

    # --- grid search para el mejor ruleset rentable ---
    for mt in (30, 20, 12):
        best, p = grid_search(R, min_trades=mt)
        if best:
            print(f"MEJOR RULESET (>= {mt} trades):")
            print(f"  filtro: vol>={p['vol']} score>={p['score']} spread<={p['spread']} "
                  f"chg<={p['chg_max']} liq<=${p['liq_hi']:.0f} top<={p['top_max']} imb>={p['imb_lo']}")
            print(" ", fline("  resultado", best))
            kept = best["n"]; print(f"  conserva {kept}/{len(R)} trades ({kept/len(R):.0%}), "
                                    f"elimina {len(R)-kept}")
            print()


if __name__ == "__main__":
    main()
