"""Validation suite for the Phase D analytics math (Module 16).

Feeds synthetic trades with KNOWN, hand-computed answers into the pure metric
helpers and asserts each calculation. No bot, no DB, no network — just the math.

Run:  ../../.venv/Scripts/python.exe tools/validate_analytics.py
"""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics import (  # noqa: E402
    AnalyticsEngine, TradeRecord, classify_regime, drawdown_of, expectancy_of,
    grade_setup, profit_factor_of, quality_score,
)

PASS, FAIL = 0, 0


def check(name: str, got, want, tol: float = 1e-6) -> None:
    global PASS, FAIL
    ok = (got == want) if not isinstance(want, float) else (got is not None and abs(got - want) <= tol)
    if ok:
        PASS += 1
        print(f"  PASS  {name}: {got}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")


def mk(pnl_usd, **kw) -> TradeRecord:
    base = dict(trade_id=f"t{mk.i}", symbol="X/USDT", exchange="binance", pnl_usd=pnl_usd)
    mk.i += 1
    base.update(kw)
    return TradeRecord(**base)


mk.i = 0


def test_expectancy():
    print("Expectancy:")
    # 3 wins (+10,+20,+30) avg 20; 2 losses (-10,-30) avg 20. wr=0.6 lr=0.4
    # expectancy = 0.6*20 - 0.4*20 = 12 - 8 = 4
    trades = [mk(10), mk(20), mk(30), mk(-10), mk(-30)]
    e = expectancy_of(trades)
    check("win_rate", e["win_rate"], 0.6)
    check("loss_rate", e["loss_rate"], 0.4)
    check("avg_win", e["avg_win"], 20.0)
    check("avg_loss", e["avg_loss"], 20.0)
    check("rr", e["rr"], 1.0)
    check("expectancy", e["expectancy"], 4.0)
    check("empty", expectancy_of([])["n"], 0)


def test_profit_factor():
    print("Profit factor:")
    # gross profit 60, gross loss 40 -> PF 1.5
    trades = [mk(10), mk(20), mk(30), mk(-10), mk(-30)]
    pf = profit_factor_of(trades)
    check("gross_profit", pf["gross_profit"], 60.0)
    check("gross_loss", pf["gross_loss"], 40.0)
    check("profit_factor", pf["profit_factor"], 1.5)
    # no losses -> inf
    check("pf_inf", profit_factor_of([mk(5), mk(5)])["profit_factor"], "inf")
    # no trades -> None
    check("pf_none", profit_factor_of([])["profit_factor"], None)


def test_drawdown():
    print("Drawdown:")
    # equity path: +100 -> 100(peak); -50 -> 50 (dd 50); +25 -> 75 (dd 25);
    # -100 -> -25 (dd from peak 100 = 125 max); +10 -> -15 (cur dd 115)
    now = datetime.now(UTC)
    seq = [100, -50, 25, -100, 10]
    trades = [mk(v, exit_timestamp=(now + timedelta(minutes=i)).isoformat())
              for i, v in enumerate(seq)]
    dd = drawdown_of(trades)
    check("max_drawdown", dd["max_drawdown"], 125.0)
    check("current_drawdown", dd["current_drawdown"], 115.0)
    check("net_equity", dd["net_equity"], -15.0)
    check("peak_equity", dd["peak_equity"], 100.0)


def test_grade():
    print("Setup grade:")
    check("below_sample", grade_setup(2.0, 5.0, 0.6, 3), "n/a")
    check("aplus", grade_setup(2.5, 5.0, 0.6, 25), "A+")
    check("broken", grade_setup(0.5, -2.0, 0.2, 25), "F")


def test_regime():
    print("Regime:")
    trend, _ = classify_regime([100, 105, 110, 120])   # +20% -> bull
    check("bull", trend, "bull")
    trend, _ = classify_regime([100, 98, 90, 85])       # -15% -> bear
    check("bear", trend, "bear")
    trend, _ = classify_regime([100, 101, 99, 100])     # ~flat -> sideways
    check("sideways", trend, "sideways")


def test_quality():
    print("Trade quality:")
    rec = TradeRecord(trade_id="q", symbol="X/USDT", exchange="binance",
                      pnl_pct=8.0, mfe_pct=10.0, mae_pct=-1.0, exit_reason="trailing",
                      holding_seconds=600, entry_slippage_pct=0.1, exit_slippage_pct=0.0)
    q = quality_score(rec, "early_entry")
    check("quality_in_range", 0.0 <= q <= 100.0, True)
    check("good_trade_high", q >= 70, True)
    bad = TradeRecord(trade_id="q2", symbol="X/USDT", exchange="binance",
                      pnl_pct=-8.0, mfe_pct=0.5, mae_pct=-8.0, exit_reason="hard_stop",
                      holding_seconds=600)
    qb = quality_score(bad, "late_entry")
    check("bad_trade_low", qb < q, True)


def test_engine_confidence_sizing():
    print("Confidence + sizing simulation:")
    eng = AnalyticsEngine()
    # Strong setup history -> high confidence -> 1.5x multiplier branch.
    for i in range(30):
        eng.trades.append(mk(20 if i % 5 else -10, setup_type="accumulation",
                              market_regime="bull/low_vol", confidence_score=80))
    conf = eng.confidence_for("accumulation", "binance")
    check("confidence_in_range", 0.0 <= conf <= 100.0, True)
    check("mult_high", eng.sizing_multiplier(95), 1.5)
    check("mult_mid", eng.sizing_multiplier(75), 1.0)
    check("mult_low", eng.sizing_multiplier(50), 0.5)
    # New setup with no history -> neutral 60.
    check("neutral_default", eng.confidence_for("brand_new", "binance"), 60.0)


def main():
    print("=" * 60)
    print("PHASE D ANALYTICS — VALIDATION SUITE")
    print("=" * 60)
    for t in (test_expectancy, test_profit_factor, test_drawdown, test_grade,
              test_regime, test_quality, test_engine_confidence_sizing):
        t()
    print("=" * 60)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
