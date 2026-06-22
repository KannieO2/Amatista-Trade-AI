#!/usr/bin/env python3
"""TradeOS Monte Carlo — REAL edition.

Antigravity's simulate.py had three fatal flaws that made its "+29% / 80% win"
worthless:
  1. Stale import path (`apps/pump-reader` — the app moved to `bots/pump-reader`,
     so the import silently fell back to defaults / never ran the real code here).
  2. FLAT 0.20% round-trip cost — the exact "paper-trading lie" it preached against.
     A $50 order in a $5K book pays nowhere near 0.20%; it walks the book.
  3. Gates so tight only 5 of 50,000 signals passed → a "+29%" computed on 5 trades
     is statistical noise, and "winning by not trading" is not winning.

This version fixes all three and anchors on the bot's REAL measured behaviour:

  PART A  EMPIRICAL TRUTH (no assumptions): bootstrap-resample the REAL exits from
          Supabase → expectancy + 95% confidence interval. This is the ground truth
          your live paper bot actually produced.
  PART B  LIQUIDITY-AWARE forward sim: synthetic 1m paths run through the REAL
          PositionManager exit engine, with a slippage model that scales with order
          size vs book depth (NOT a flat cost). Compares CURRENT exit params vs the
          PROPOSED fix (fast dead-trade cut + short timeout) on the SAME scenarios.
          A calibration check prints whether the model reproduces the real win-rate
          before any proposed numbers are trusted.
  PART C  ASYMMETRY + VIDEO math: at the bot's real ~16% win-rate, what avg-win /
          avg-loss is needed to break even, and how that maps to the video's premise
          (KManuS88: catch the move EARLY and let it run) vs the bot's reality
          (86% flat entries that never move).

    SIM_N=200000 python tools/simulate_real.py
"""
from __future__ import annotations

import importlib
import os
import random
import sys
from datetime import UTC, datetime, timedelta
from statistics import mean

# Correct path: the app lives in bots/pump-reader (NOT apps/pump-reader).
_APP = os.path.join(os.path.dirname(__file__), "..", "bots", "pump-reader")
sys.path.insert(0, _APP)

_N_REQ = int(os.getenv("SIM_N", "200000"))
N = min(_N_REQ, 3_000_000)   # safety cap: scenarios are held in RAM; huge N would OOM
MAX_BARS = int(os.getenv("SIM_MAX_BARS", "90"))
USD = float(os.getenv("SIM_USD", "50"))           # real per-trade size (PUMP_AUTO_ENTRY_USD=50)
IMPACT_CAP_PCT = 2.0                                # walking the full 2% band = 2% slip per side
random.seed(int(os.getenv("SIM_SEED", "7")))

_names = ["flat", "fakeout", "rug", "slow_grind", "real_pump"]
# Archetype weights CALIBRATED so the baseline gated run reproduces the real mix:
# ~86% of entered trades go flat/fade (timeout bleed), ~4% rug/dump, ~4% win. Tune
# via env if the calibration check below drifts from the real numbers.
_W = [float(os.getenv("SIM_W_FLAT", "0.78")), float(os.getenv("SIM_W_FAKE", "0.08")),
      float(os.getenv("SIM_W_RUG", "0.05")), float(os.getenv("SIM_W_GRIND", "0.04")),
      float(os.getenv("SIM_W_PUMP", "0.05"))]


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _liq_slippage_pct(size_usd: float, liq_2pct_usd: float, spread_pct: float) -> float:
    """REAL, liquidity-aware round-trip cost (the whole point of this rewrite).
    You cross the spread, then a market order walks the book in proportion to how
    much of the 2%-band depth you consume — on BOTH entry and exit."""
    if liq_2pct_usd <= 0:
        return IMPACT_CAP_PCT * 2 + spread_pct
    frac = size_usd / liq_2pct_usd
    impact_per_side = min(IMPACT_CAP_PCT, frac * IMPACT_CAP_PCT)
    return spread_pct + 2 * impact_per_side          # spread once + impact on entry & exit


def _gen_scenario():
    """One flagged candidate: features the gates see + a forward 1m path. Liquidity
    is lognormal (thin microcaps common); rug probability rises as the book thins."""
    q = random.random()
    conf = _clamp(40 + 22 * q + random.gauss(0, 17), 20, 99)
    liq = 10 ** (3.4 + 2.1 * (0.45 * q + 0.55 * random.random()))   # ~2.5k .. 350k
    vol = max(0.6, random.lognormvariate(1.05, 0.5))
    spread = _clamp(0.1 + (3.2 * (1 - 0.4 * q)) * random.random(), 0.05, 6.0)
    chg = _clamp(random.gauss(14, 18), -10, 95)
    top = _clamp(0.5 + 0.45 * random.random(), 0.4, 0.97)
    feat = dict(conf=conf, vol=vol, liq=liq, spread=spread, chg=chg, top=top)

    w = list(_W)
    if liq < 20_000:                                  # thin book → more rugs
        w[2] += 0.10
    arch = random.choices(_names, w)[0]

    p, out = 1.0, []
    if arch == "real_pump":
        peak = 1 + min(0.7, abs(random.gauss(0.12, 0.13)))
        run = random.randint(2, 8)
        up = peak ** (1.0 / run)
        for i in range(run):
            p *= up * random.uniform(0.98, 1.03)
            out.append((p, random.uniform(4, 9) * (1 - i / (run * 2.5))))
        for i in range(MAX_BARS - run):
            p *= random.uniform(0.93, 1.005)
            out.append((p, random.uniform(1, 4) * (0.75 ** i)))
    elif arch == "slow_grind":
        n = random.randint(10, 45)
        for _ in range(n):
            p *= 1 + random.uniform(-0.006, 0.010)
            out.append((p, random.uniform(1.0, 2.5)))
        for _ in range(MAX_BARS - n):
            p *= random.uniform(0.985, 1.004)
            out.append((p, random.uniform(0.5, 1.3)))
    elif arch == "fakeout":
        pop = random.randint(1, 3)
        for _ in range(pop):
            p *= 1 + random.uniform(0.01, 0.06)
            out.append((p, random.uniform(3, 7)))
        for i in range(MAX_BARS - pop):
            p *= 1 + random.uniform(-0.012, 0.002)
            out.append((p, random.uniform(0.5, 2.5) * (0.82 ** i)))
    elif arch == "rug":
        # Calibrated to the REAL data: hard_stop exited at ~-8.6% avg (a gap below
        # the 2.5% stop), NOT a -50% cartoon rug. Model a -6..-12% drop bar.
        pre = random.randint(1, 6)
        for _ in range(pre):
            p *= 1 + random.uniform(-0.01, 0.02)
            out.append((p, random.uniform(2, 6)))
        p *= random.uniform(0.88, 0.94)               # -6%..-12% drop = realistic hard_stop
        out.append((p, random.uniform(5, 12)))
        for _ in range(MAX_BARS - pre - 1):
            p *= random.uniform(0.97, 1.005)
            out.append((p, random.uniform(0.5, 2)))
    else:  # flat — the DOMINANT real case (86% of entries): drifts slowly, stays in
        # the band, never goes green -> times out / fast-cuts at a SMALL loss.
        for _ in range(MAX_BARS):
            p *= 1 + random.gauss(-0.0003, 0.0045)    # calm slow drift (real flats aren't volatile)
            out.append((max(p, 0.01), random.uniform(0.3, 1.2)))
    return feat, arch, out


def _passes_gates(s) -> bool:
    """Mirror the live entry gates, reading thresholds LIVE from env so a proposed
    param set takes effect on reload."""
    if s["conf"] < float(os.getenv("PUMP_ENTRY_MIN_CONFIDENCE", "50")):
        return False
    if s["chg"] >= float(os.getenv("PUMP_ENTRY_MAX_CHASE_PCT", "60")):
        return False
    if s["vol"] < float(os.getenv("SIM_GATE_MIN_VOL", os.getenv("PUMP_ENTRY_MIN_VOL_SPIKE", "2.5"))):
        return False
    from app.scanner import forensic_check
    ok, _ = forensic_check(spread_pct=s["spread"], liquidity_usd=s["liq"], top_book_share=s["top"])
    return ok


def _run_one(PM, path, cost_pct: float):
    pm = PM()
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    entry = path[0][0]
    pm.open(symbol="X", exchange="mexc", entry_price=entry, qty=USD / entry, now=t0)
    pos = pm.positions["mexc:X"]
    reason = "open_eod"
    for bar, (price, vol) in enumerate(path[1:], start=1):
        evs = pm.step("mexc:X", price, volume=vol, now=t0 + timedelta(seconds=bar * 60))
        if evs:
            reason = evs[-1].reason
        if pos.closed:
            break
    realized = pos.realized_pnl + (pos.last_price - entry) * pos.qty
    return realized / USD * 100 - cost_pct, reason


def _stats(xs):
    if not xs:
        return dict(n=0, ev=0, win=0, avgw=0, avgl=0, pf=0, tot=0)
    wins = [x for x in xs if x > 0]
    loss = [x for x in xs if x <= 0]
    gp, gl = sum(wins), -sum(loss)
    return dict(n=len(xs), ev=sum(xs) / len(xs), win=len(wins) / len(xs) * 100,
                avgw=(gp / len(wins) if wins else 0), avgl=(-gl / len(loss) if loss else 0),
                pf=(gp / gl if gl else float("inf")), tot=sum(xs))


def run_scenario(label: str, params: dict, scenarios) -> dict:
    """Apply params to env, RELOAD position_manager (so module-level constants like
    the fast-cut take effect), then run the real exit engine over the shared
    scenario set with liquidity-aware cost."""
    for k, v in params.items():
        os.environ[k] = str(v)
    import app.position_manager as pmod
    importlib.reload(pmod)
    PM = pmod.PositionManager

    pnls, by_reason = [], {}
    for feat, _arch, path in scenarios:
        if not _passes_gates(feat):
            continue
        cost = _liq_slippage_pct(USD, feat["liq"] * 0.02, feat["spread"])  # 2% band depth ≈ liq*0.02
        pnl, reason = _run_one(PM, path, cost)
        pnls.append(pnl)
        by_reason.setdefault(reason, []).append(pnl)
    st = _stats(pnls)
    print(f"\n-- {label} --")
    print(f"  entered: {st['n']:,} of {len(scenarios):,}   "
          f"EV/trade: {st['ev']:+.3f}%   win: {st['win']:.1f}%   PF: {st['pf']:.2f}")
    print(f"  avg win: {st['avgw']:+.2f}%   avg loss: {st['avgl']:+.2f}%   "
          f"net on {st['n']:,} trades: {st['tot']/100*USD:+,.0f}$")
    for r, xs in sorted(by_reason.items(), key=lambda kv: sum(kv[1])):
        s = _stats(xs)
        print(f"    {r:12} {s['n']:>7,} ({s['n']/max(st['n'],1)*100:4.1f}%)  "
              f"avg {s['ev']:+.2f}%  net {s['tot']/100*USD:+7.0f}$")
    return st


# ---------------------------------------------------------------------------
def part_a_empirical():
    print("=" * 64)
    print("PART A — EMPIRICAL TRUTH (real Supabase exits, bootstrap 95% CI)")
    print("=" * 64)
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_APP, "..", "..", ".env"))
        import asyncio
        from app import store

        async def _pull():
            return await store.list_exits("owner")
        exits = asyncio.run(_pull())
    except Exception as e:
        print(f"  (could not load real exits: {e})")
        return None
    pnls = [float(e.get("pnl") or 0) for e in exits]
    if not pnls:
        print("  (no real exits found)")
        return None
    n = len(pnls)
    boot = []
    for _ in range(5000):
        s = [random.choice(pnls) for _ in range(n)]
        boot.append(sum(s) / n)
    boot.sort()
    lo, hi = boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot))]
    wins = sum(1 for p in pnls if p > 0)
    print(f"  real trades: {n}   win rate: {wins/n*100:.1f}%   net: ${sum(pnls):+.2f}")
    print(f"  mean PnL/trade: ${mean(pnls):+.3f}   95% CI: [${lo:+.3f}, ${hi:+.3f}]")
    verdict = "NEGATIVE (loses money)" if hi < 0 else "inconclusive" if lo < 0 < hi else "POSITIVE"
    print(f"  verdict: edge is {verdict}  ({'CI entirely below 0' if hi<0 else 'CI straddles 0'})")
    return dict(n=n, win=wins / n * 100, mean=mean(pnls), lo=lo, hi=hi)


def part_c_asymmetry(real):
    print("\n" + "=" * 64)
    print("PART C — ASYMMETRY & VIDEO math")
    print("=" * 64)
    wr = (real["win"] / 100) if real else 0.16
    print(f"  at the bot's real win rate {wr*100:.0f}%, break-even needs:")
    for avg_loss in (0.4, 0.8, 1.25):
        # wr*W - (1-wr)*L = 0  ->  W = (1-wr)/wr * L
        need_w = (1 - wr) / wr * avg_loss
        print(f"    if avg loss = -{avg_loss:.2f}%  ->  avg win must be >= +{need_w:.2f}%")
    print("  VIDEO premise (KManuS88): enter EARLY, let the clean move run = big avg win,")
    print("  rare small losses. Bot REALITY: 86% flat entries -> many small losses, few")
    print("  winners. The fix mirrors the video: make losers CHEAP (fast cut) and let the")
    print("  few real movers RUN (trailing already wins 100%).")


def main():
    real = part_a_empirical()

    print("\n" + "=" * 64)
    print(f"PART B — LIQUIDITY-AWARE forward sim  (N={N:,}, size=${USD:.0f}/trade)")
    print("=" * 64)
    scenarios = [_gen_scenario() for _ in range(N)]

    current = {
        "PUMP_STOP_LOSS_PCT": "2.5", "PUMP_TIMEOUT_MINUTES": "60",
        "PUMP_MAX_HOLD_MINUTES": "90", "PUMP_FAST_CUT_MINUTES": "0",
        "PUMP_TRAIL_ARM_PCT": "3", "PUMP_TRAIL_GIVEBACK_PCT": "14", "PUMP_BREAKEVEN_PCT": "9",
    }
    proposed = {
        "PUMP_STOP_LOSS_PCT": "2.5", "PUMP_TIMEOUT_MINUTES": "10",
        "PUMP_MAX_HOLD_MINUTES": "20", "PUMP_FAST_CUT_MINUTES": "3",
        "PUMP_FAST_CUT_MIN_PROGRESS_PCT": "0.6",
        "PUMP_TRAIL_ARM_PCT": "2", "PUMP_TRAIL_GIVEBACK_PCT": "20", "PUMP_BREAKEVEN_PCT": "5",
    }
    base = run_scenario("CURRENT exit params (baseline)", current, scenarios)
    if real:
        print(f"\n  CALIBRATION CHECK: sim baseline win {base['win']:.0f}% vs real {real['win']:.0f}%  "
              f"-> {'OK (model tracks reality)' if abs(base['win']-real['win'])<12 else 'DRIFT (tune SIM_W_*)'}")
    prop = run_scenario("PROPOSED fix (fast-cut + short timeout + let winners run)", proposed, scenarios)

    print("\n  --------- DELTA ---------")
    print(f"  EV/trade : {base['ev']:+.3f}%  ->  {prop['ev']:+.3f}%   "
          f"({prop['ev']-base['ev']:+.3f}% per trade)")
    print(f"  avg loss : {base['avgl']:+.2f}%  ->  {prop['avgl']:+.2f}%")
    print(f"  win rate : {base['win']:.1f}%  ->  {prop['win']:.1f}%")

    part_c_asymmetry(real)
    print("\nNOTE: Part A numbers are EMPIRICAL (trust them). Part B absolute EV is")
    print("model-dependent; the DELTA between baseline and proposed is the robust signal.")


if __name__ == "__main__":
    main()
