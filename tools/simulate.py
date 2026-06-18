#!/usr/bin/env python3
"""Monte Carlo of the REAL TradeOS strategy.

We cannot download a million real tokens (no such API, rate limits), so we
generate a million *synthetic-but-calibrated* microcap scenarios — pumps that run
then dump, fakeouts, chop, rugs, slow grinds — and run the bot's ACTUAL code over
them: the same entry gates (confidence / momentum / volume / forensic) and the
same exit logic (PositionManager: TP1, trailing, dump, hard-stop, break-even,
volume-aware time-stop). Each round trip pays COST_PCT (fees + slippage).

The absolute expectancy depends on the scenario assumptions (archetype weights +
pump-size distribution, printed below — tune via env). The RELATIVE diagnosis —
which exit reason bleeds, whether the gates add edge, how a parameter change moves
EV — is robust and is what we use to find what's wrong.

    SIM_N=1000000 python tools/simulate.py
"""
from __future__ import annotations

import os
import random
import sys
import time
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "pump-reader"))

from app.position_manager import PositionManager  # noqa: E402
from app.scanner import forensic_check  # noqa: E402

# Mirror the live entry gates (import so the sim stays in sync with production).
try:
    from app.main import ENTRY_MAX_CHASE_PCT, ENTRY_MIN_CONFIDENCE, ENTRY_MIN_VOL_SPIKE
except Exception:  # pragma: no cover - fallback to current defaults
    ENTRY_MIN_CONFIDENCE, ENTRY_MAX_CHASE_PCT, ENTRY_MIN_VOL_SPIKE = 50.0, 60.0, 2.5

N = int(os.getenv("SIM_N", "1000000"))
COST_PCT = float(os.getenv("SIM_COST_PCT", "0.20"))   # round-trip fees + slippage, %
MAX_BARS = int(os.getenv("SIM_MAX_BARS", "90"))        # 1m bars simulated per trade
USD = 100.0
random.seed(int(os.getenv("SIM_SEED", "7")))

_names = ["fakeout", "chop", "rug", "slow_grind", "real_pump"]


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _gen_scenario() -> tuple[dict, str, list[tuple[float, float]]]:
    """One flagged candidate. KEY REALISM: at entry you cannot tell a real pump
    from a fakeout — the features overlap heavily. A hidden 'quality' q tilts the
    OUTCOME only weakly, and bleeds into the features only weakly + buried in
    noise, so the gates have realistic (modest) skill, not oracle vision. Pump
    magnitudes are small because the bot enters mid-move (near a local high)."""
    q = random.random()  # latent, unobservable at entry

    # --- features the gates see: weak q signal, heavy noise (entry ambiguity) ---
    conf = _clamp(40 + 22 * q + random.gauss(0, 17), 20, 99)
    # liquidity lognormal ~5k..500k; higher-quality setups skew deeper, with noise
    liq = 10 ** (3.7 + 2.0 * (0.45 * q + 0.55 * random.random()))
    vol = max(0.6, random.lognormvariate(1.05, 0.5))          # ~2-6x, ~independent
    spread = _clamp(0.1 + (3.2 * (1 - 0.4 * q)) * random.random(), 0.05, 6.0)
    chg = _clamp(random.gauss(18, 22), -10, 95)               # 24h move at flag time
    top = _clamp(0.5 + 0.45 * random.random(), 0.4, 0.97)
    feat = dict(conf=conf, vol=vol, liq=liq, spread=spread, chg=chg, top=top)

    # --- outcome archetype. REALISM: a rug/dump is a THIN-BOOK event, so its
    # probability is tied to liquidity (deep books rarely rug). This is what gives
    # the liquidity/forensic gate genuine protective power. ---
    if liq < 20_000:
        p_rug = 0.22
    elif liq < 50_000:
        p_rug = 0.12
    elif liq < 150_000:
        p_rug = 0.05
    else:
        p_rug = 0.015
    p_pump = 0.08 + 0.18 * q
    p_grind = 0.09 + 0.08 * q
    p_fake = 0.38 - 0.08 * q
    p_chop = max(0.0, 1 - (p_pump + p_grind + p_rug + p_fake))
    arch = random.choices(_names, [p_fake, p_chop, p_rug, p_grind, p_pump])[0]

    # --- forward path from entry=1.0. Entry is mid-move → modest residual upside ---
    p, out = 1.0, []
    if arch == "real_pump":
        # residual peak from entry: mostly +3..25%, fat tail to ~+70%
        peak = 1 + min(0.7, abs(random.gauss(0.12, 0.13)))
        run = random.randint(2, 8)
        up = peak ** (1.0 / run)
        for i in range(run):
            p *= up * random.uniform(0.98, 1.03)
            out.append((p, random.uniform(4, 9) * (1 - i / (run * 2.5))))
        for i in range(MAX_BARS - run):                       # give most of it back
            p *= random.uniform(0.93, 1.005)
            out.append((p, random.uniform(1, 4) * (0.75 ** i)))
    elif arch == "slow_grind":
        n = random.randint(10, 45)
        for _ in range(n):
            p *= 1 + random.uniform(-0.006, 0.010)            # weak net up, choppy
            out.append((p, random.uniform(1.0, 2.5)))
        for _ in range(MAX_BARS - n):
            p *= random.uniform(0.985, 1.004)
            out.append((p, random.uniform(0.5, 1.3)))
    elif arch == "fakeout":
        pop = random.randint(1, 3)
        for _ in range(pop):
            p *= 1 + random.uniform(0.01, 0.08)               # small pop
            out.append((p, random.uniform(3, 7)))
        for i in range(MAX_BARS - pop):
            p *= 1 + random.uniform(-0.015, 0.002)            # fades under entry
            out.append((p, random.uniform(0.5, 2.5) * (0.82 ** i)))
    elif arch == "rug":
        pre = random.randint(1, 6)
        for _ in range(pre):
            p *= 1 + random.uniform(-0.01, 0.02)
            out.append((p, random.uniform(2, 6)))
        p *= random.uniform(0.25, 0.65)                       # the rug
        out.append((p, random.uniform(8, 20)))
        for _ in range(MAX_BARS - pre - 1):
            p *= random.uniform(0.96, 1.01)
            out.append((p, random.uniform(0.5, 2)))
    else:  # chop
        for _ in range(MAX_BARS):
            p *= 1 + random.gauss(0, 0.012)
            out.append((max(p, 0.01), random.uniform(0.6, 2.0)))
    return feat, arch, out


def _passes_gates(s: dict, accel: float | None = None) -> bool:
    if s["conf"] < ENTRY_MIN_CONFIDENCE:
        return False
    if s["chg"] >= ENTRY_MAX_CHASE_PCT:
        return False
    if accel is None and s["vol"] < ENTRY_MIN_VOL_SPIKE:
        return False
    ok, _ = forensic_check(spread_pct=s["spread"], liquidity_usd=s["liq"], top_book_share=s["top"])
    return ok


def _simulate_trade(path: list[tuple[float, float]]) -> tuple[float, str]:
    """Run the REAL exit engine over a path. Returns (net_pnl_pct, exit_reason)."""
    pm = PositionManager()
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
    # Mark-to-market anything still open at the horizon.
    realized = pos.realized_pnl + (pos.last_price - entry) * pos.qty
    pnl_pct = realized / USD * 100 - COST_PCT
    return pnl_pct, reason


def main() -> None:
    t = time.time()
    signals = entered = 0
    pnls: list[float] = []
    by_reason: dict[str, list[float]] = {}
    by_arch_entered: dict[str, int] = {a: 0 for a in _names}
    take_all_pnls: list[float] = []   # ungated comparison
    for i in range(N):
        s, arch, path = _gen_scenario()
        signals += 1
        pnl, reason = _simulate_trade(path)   # deterministic given the path
        take_all_pnls.append(pnl)             # ungated: take every flagged signal
        if _passes_gates(s):                  # gated: only what the bot would take
            entered += 1
            by_arch_entered[arch] += 1
            pnls.append(pnl)
            by_reason.setdefault(reason, []).append(pnl)
        if (i + 1) % 200_000 == 0:
            print(f"  …{i+1:,}/{N:,} ({time.time()-t:.0f}s)", flush=True)

    def stats(xs: list[float]) -> dict:
        if not xs:
            return dict(n=0, ev=0, win=0, avgw=0, avgl=0, pf=0, tot=0)
        wins = [x for x in xs if x > 0]
        loss = [x for x in xs if x <= 0]
        gp, gl = sum(wins), -sum(loss)
        return dict(n=len(xs), ev=sum(xs)/len(xs), win=len(wins)/len(xs)*100,
                    avgw=(gp/len(wins) if wins else 0), avgl=(-gl/len(loss) if loss else 0),
                    pf=(gp/gl if gl else float("inf")), tot=sum(xs))

    g = stats(pnls)
    a = stats(take_all_pnls)
    print("\n================ TradeOS Monte Carlo ================")
    print(f"signals: {signals:,}   cost/trade: {COST_PCT:.2f}%   bars: {MAX_BARS}   seed fixed")
    print(f"gates: conf>={ENTRY_MIN_CONFIDENCE:.0f}  chase<{ENTRY_MAX_CHASE_PCT:.0f}%  vol>={ENTRY_MIN_VOL_SPIKE}x  +forensic")
    print(f"\n-- GATED (what the bot actually trades) --")
    print(f"  entered: {entered:,} ({entered/signals*100:.1f}% of signals)")
    print(f"  expectancy/trade: {g['ev']:+.3f}%   win rate: {g['win']:.1f}%")
    print(f"  avg win: {g['avgw']:+.2f}%   avg loss: {g['avgl']:+.2f}%   profit factor: {g['pf']:.2f}")
    print(f"  total return on deployed capital: {g['tot']:+.0f}% (sum of {g['n']:,} trades @ $100)")
    print(f"  net $: {g['tot']/100*USD:+,.0f} over {g['n']:,} trades")
    print(f"\n-- UNGATED (take every flagged signal) --")
    print(f"  expectancy/trade: {a['ev']:+.3f}%   win rate: {a['win']:.1f}%   profit factor: {a['pf']:.2f}")
    print(f"  -> gates add {g['ev']-a['ev']:+.3f}% expectancy per trade")
    print(f"\n-- exit reason breakdown (gated) --")
    for r, xs in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        st = stats(xs)
        print(f"  {r:12} {st['n']:>8,} ({st['n']/g['n']*100:4.1f}%)  avg {st['ev']:+.2f}%  win {st['win']:4.1f}%")
    print(f"\n-- entries by archetype (gated) --")
    for ar in _names:
        c = by_arch_entered[ar]
        print(f"  {ar:12} {c:>8,} ({c/max(entered,1)*100:4.1f}% of entries)")
    print(f"\n{time.time()-t:.0f}s total")


if __name__ == "__main__":
    main()
