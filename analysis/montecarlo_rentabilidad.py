"""
Amatista — Simulación Montecarlo de rentabilidad (audit-only, NO toca el bot).

Modela la GEOMETRÍA DE LA ESTRATEGIA (parámetros leídos del código), no el log
contaminado del bot. La incógnita real de un detector de pumps es el WIN-RATE
(calidad de señal): la estructura de salida fija el PAYOFF, no la tasa de acierto.
Por eso:
  - Modo A: barre WR y halla el win-rate de equilibrio (break-even).
  - Modo B: 500k paths por escenario (pesimista/base/optimista) × horizonte
            (3m/6m/1a) -> Sharpe, Sortino, maxDD, risk-of-ruin, CAGR, percentiles.
  - Grid: modelo de carry en rango con cola izquierda por ruptura.

TODO valor de WR/regímen es SUPUESTO ETIQUETADO, no medición. Las salidas dicen
"bajo estos supuestos", nunca "esto es lo que el bot hizo".
"""
from __future__ import annotations
import json, os, math
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RNG = np.random.default_rng(20260625)
OUT = os.path.join(os.path.dirname(__file__), "out")
os.makedirs(OUT, exist_ok=True)
N_PATHS = 500_000

# ---------------------------------------------------------------------------
# 1) PARÁMETROS REALES DE LA ESTRATEGIA (extraídos del código del bot)
# ---------------------------------------------------------------------------
# pump-reader/app/position_manager.py + risk.py (defaults os.getenv):
PUMP = dict(
    hard_stop_pct=8.0,      # PUMP_STOP_LOSS_PCT   -> pérdida tope por trade
    breakeven_pct=4.0,      # PUMP_BREAKEVEN_PCT   -> arma stop a ~empate
    trail_arm_pct=0.8,      # PUMP_TRAIL_ARM_PCT   -> trailing se arma casi en verde
    trail_giveback=8.0,     # PUMP_TRAIL_GIVEBACK_PCT -> devuelve 8% del pico
    flat_timeout_pct=-1.25, # salida plana medida (timeout/vol_fade), de comentario código
    fee_slip_roundtrip=0.40,# taker ~0.1%×2 + slippage ~0.2% (CEX spot mexc/bitget)
    pos_frac=0.20,          # fracción de equity por trade (max_position 25%, uso 20%)
    max_open=4,             # PUMP_MAX_OPEN_TRADES
    daily_loss_halt=8.0,    # PUMP_MAX_DAILY_LOSS_PCT (circuit breaker)
    drawdown_halt=10.0,     # PUMP_MAX_DRAWDOWN_PCT
    trades_per_day=3.0,     # estrategia "predador": pocas entradas alta convicción
)

# grvtbot/packages/bot/src/bot/grid-engine.ts (geometría de grid GRVT perp):
GRID = dict(
    spacing_pct=0.6,        # % por nivel (típico setup en rango)
    fee_roundtrip=0.10,     # maker grid GRVT ~0.05%×2
    num_grids=20,           # niveles -> capital por nivel = 1/num_grids del total
    daily_fills_chop=4.0,   # round-trips/día en mercado lateral (conservador)
    daily_fills_trend=1.5,  # en tendencia se llena poco antes de romper
    breakout_loss_pct=5.0,  # pérdida de inventario cuando rompe el rango (% del equity)
    breakout_prob_day=0.015,# prob diaria de ruptura fuerte fuera de rango (lateral)
    leverage=2.0,
)

HORIZONS = {"3 meses": 91, "6 meses": 182, "1 año": 365}

# VARIABLE PIVOTE: ancho del hard-stop. Default del código = 8% (PUMP_STOP_LOSS_PCT),
# pero los perfiles de cluster (position_manager.py:72-75) lo ajustan a ~0.9-1.3%.
# El veredicto de rentabilidad cambia POR COMPLETO según cuál corra en vivo.
STOP_REGIMES = {"Ajustado (cluster ~1.3%)": 1.3, "Ancho (default 8%)": 8.0}
PRIMARY_STOP = 1.3   # el más probable en vivo (perfiles de cluster activos)

# Escenarios = bandas de régimen (NO pronóstico). Mapean calidad de señal.
SCENARIOS = {
    "Pesimista (mercado choppy / pocas señales)": dict(p_win=0.12, runner_frac=0.20, trades_mult=0.7),
    "Base (mercado normal)":                       dict(p_win=0.20, runner_frac=0.30, trades_mult=1.0),
    "Optimista (alt-season / pumps frecuentes)":   dict(p_win=0.30, runner_frac=0.45, trades_mult=1.4),
}

# ---------------------------------------------------------------------------
# 2) MODELO DE RETORNO POR TRADE (pump) — vectorizado sobre N paths
# ---------------------------------------------------------------------------
def pump_trade_returns(n, p_win, runner_frac, hard_stop=None):
    """Devuelve retorno% por trade (sobre el notional del trade, antes de pos_frac).
    Mezcla: WIN (pico capturado por trailing) / SCRATCH (plano) / LOSS (hard stop).
    hard_stop = ancho del stop en %; es la variable pivote de la rentabilidad."""
    if hard_stop is None:
        hard_stop = PUMP["hard_stop_pct"]
    u = RNG.random(n)
    out = np.empty(n)
    fee = PUMP["fee_slip_roundtrip"]

    win = u < p_win
    # de los NO-win, ~40% son scratch plano, resto hard-stop
    rest = ~win
    scratch = rest & (RNG.random(n) < 0.40)
    loss = rest & ~scratch

    # WIN: pico = mezcla pop pequeño / runner cola gorda; capturo (1-giveback)
    nw = win.sum()
    if nw:
        is_runner = RNG.random(nw) < runner_frac
        peak = np.where(
            is_runner,
            RNG.lognormal(math.log(14.0), 0.70, nw),   # runner: mediana ~14%, cola a >60%
            RNG.lognormal(math.log(2.2), 0.50, nw),    # pop: mediana ~2.2%
        )
        captured = peak * (1 - PUMP["trail_giveback"] / 100.0) * 0.90
        out[win] = captured - fee

    # SCRATCH: plano medido ~ -1.25%
    ns = scratch.sum()
    if ns:
        out[scratch] = RNG.normal(PUMP["flat_timeout_pct"], 0.6, ns) - fee

    # LOSS: banda hard-stop (escala con el ancho del stop + algo de slippage)
    nl = loss.sum()
    if nl:
        sd = max(0.30, hard_stop * 0.20)
        out[loss] = np.clip(RNG.normal(-hard_stop, sd, nl), -hard_stop * 1.8, -0.1) - fee
    return out

def pump_expectancy(p_win, runner_frac, hard_stop=None, n=400_000):
    r = pump_trade_returns(n, p_win, runner_frac, hard_stop)
    return r.mean(), r.std()

# ---------------------------------------------------------------------------
# 3) MODO A — WIN-RATE DE EQUILIBRIO (break-even)
# ---------------------------------------------------------------------------
def breakeven_sweep(hard_stop=None):
    wrs = np.linspace(0.05, 0.50, 46)
    exp_base, exp_pess, exp_opt = [], [], []
    for wr in wrs:
        exp_base.append(pump_expectancy(wr, 0.30, hard_stop)[0])
        exp_pess.append(pump_expectancy(wr, 0.20, hard_stop)[0])
        exp_opt.append(pump_expectancy(wr, 0.45, hard_stop)[0])
    exp_base, exp_pess, exp_opt = map(np.array, (exp_base, exp_pess, exp_opt))
    def be_wr(exp):
        s = np.where(np.diff(np.sign(exp)) != 0)[0]
        if len(s) == 0:
            return None
        i = s[0]
        # interpolación lineal del cruce por 0
        x0, x1, y0, y1 = wrs[i], wrs[i+1], exp[i], exp[i+1]
        return float(x0 - y0 * (x1 - x0) / (y1 - y0))
    return wrs, exp_pess, exp_base, exp_opt, {
        "pesimista": be_wr(exp_pess), "base": be_wr(exp_base), "optimista": be_wr(exp_opt)}

def breakeven_vs_stop():
    """Gráfico clave: win-rate de equilibrio EN FUNCIÓN del ancho del stop.
    Muestra que con stop ajustado basta ~20% WR; con stop ancho exige ~45%."""
    stops = np.linspace(0.8, 8.0, 25)
    wrs = np.linspace(0.03, 0.60, 58)
    be = []
    for s in stops:
        exp = np.array([pump_expectancy(w, 0.30, s, n=120_000)[0] for w in wrs])
        sgn = np.where(np.diff(np.sign(exp)) != 0)[0]
        if len(sgn):
            i = sgn[0]; x0, x1, y0, y1 = wrs[i], wrs[i+1], exp[i], exp[i+1]
            be.append(float(x0 - y0 * (x1 - x0) / (y1 - y0)))
        else:
            be.append(None)
    return stops.tolist(), be

# ---------------------------------------------------------------------------
# 4) MODO B — MONTECARLO DE EQUITY (500k paths)
# ---------------------------------------------------------------------------
def mc_equity(p_win, runner_frac, days, n_paths=N_PATHS, trades_mult=1.0,
              record_curve=False, hard_stop=None):
    """Compone equity trade a trade. Aplica pos_frac + circuit breaker diario.
    Devuelve métricas + (opcional) percentiles de curva."""
    tpd = PUMP["trades_per_day"] * trades_mult
    n_trades = max(1, int(round(tpd * days)))
    eq = np.ones(n_paths)             # equity normalizada (1.0 = capital inicial)
    peak = np.ones(n_paths)
    max_dd = np.zeros(n_paths)
    pos = PUMP["pos_frac"]
    dl_halt = PUMP["daily_loss_halt"] / 100.0
    dd_halt = PUMP["drawdown_halt"] / 100.0
    tpd_i = max(1, int(round(tpd)))

    # Sharpe/Sortino vía acumuladores incrementales (sin guardar 365×N arrays):
    sum_d = np.zeros(n_paths); sumsq_d = np.zeros(n_paths)
    sum_down = np.zeros(n_paths); n_days = 0
    day_acc = np.zeros(n_paths); day_start_eq = eq.copy()

    curve_idx, curve = [], []
    snap_every = max(1, n_trades // 60)

    for t in range(n_trades):
        r = pump_trade_returns(n_paths, p_win, runner_frac, hard_stop) / 100.0
        # circuit breaker: si la pérdida del día ya superó el límite, no opera
        dd = (peak - eq) / peak
        halted = (day_acc <= -dl_halt) | (dd >= dd_halt)
        applied = np.where(halted, 0.0, r * pos)
        eq = np.maximum(eq * (1 + applied), 1e-6)
        peak = np.maximum(peak, eq)
        max_dd = np.maximum(max_dd, (peak - eq) / peak)
        # cierre de día (aprox: tpd trades por día) -> acumula stats diarios
        if (t + 1) % tpd_i == 0:
            day_ret = eq / day_start_eq - 1.0
            sum_d += day_ret; sumsq_d += day_ret * day_ret
            sum_down += np.where(day_ret < 0, day_ret * day_ret, 0.0)
            n_days += 1
            day_start_eq = eq.copy(); day_acc[:] = 0.0
        else:
            day_acc = eq / day_start_eq - 1.0
        if record_curve and (t % snap_every == 0 or t == n_trades - 1):
            curve_idx.append(t / tpd)  # día
            curve.append(np.percentile(eq, [5, 25, 50, 75, 95]))

    total_ret = eq - 1.0
    if n_days > 0:
        mu_d = sum_d / n_days
        var_d = np.maximum(sumsq_d / n_days - mu_d * mu_d, 0.0)
        sd_d = np.sqrt(var_d) + 1e-9
        dsd = np.sqrt(sum_down / n_days) + 1e-9
        sharpe = (mu_d / sd_d) * math.sqrt(365)
        sortino = (mu_d / dsd) * math.sqrt(365)
    else:
        sharpe = sortino = np.zeros(n_paths)
    years = days / 365.0
    cagr = np.sign(eq) * (np.abs(eq) ** (1 / years)) - 1.0

    m = dict(
        days=days, n_trades=n_trades,
        median_total_ret=float(np.median(total_ret)),
        mean_total_ret=float(np.mean(total_ret)),
        p5_total=float(np.percentile(total_ret, 5)),
        p25_total=float(np.percentile(total_ret, 25)),
        p75_total=float(np.percentile(total_ret, 75)),
        p95_total=float(np.percentile(total_ret, 95)),
        prob_profit=float((total_ret > 0).mean()),
        prob_ruin_50=float((eq < 0.5).mean()),     # risk-of-ruin: perder >50%
        prob_loss_20=float((total_ret < -0.20).mean()),
        median_maxdd=float(np.median(max_dd)),
        p95_maxdd=float(np.percentile(max_dd, 95)),
        median_cagr=float(np.median(cagr)),
        median_sharpe=float(np.median(sharpe)),
        median_sortino=float(np.median(sortino)),
    )
    if record_curve:
        m["curve_days"] = curve_idx
        m["curve_pcts"] = np.array(curve).tolist()
        m["sample_total_ret"] = total_ret[:50000].tolist()
    return m

# ---------------------------------------------------------------------------
# 5) GRID — modelo de carry en rango con cola por ruptura (1 año)
# ---------------------------------------------------------------------------
def mc_grid(days, n_paths=200_000, regime="chop"):
    fills = GRID["daily_fills_chop"] if regime == "chop" else GRID["daily_fills_trend"]
    # ganancia por fill SOBRE EL EQUITY TOTAL = spacing neto × apalancamiento / nº niveles
    # (cada nivel usa solo 1/num_grids del capital). Sin /num_grids el compounding explota.
    per_fill = (GRID["spacing_pct"] - GRID["fee_roundtrip"]) / 100.0 * GRID["leverage"] / GRID["num_grids"]
    bp = GRID["breakout_prob_day"] * (1.0 if regime == "chop" else 3.0)
    eq = np.ones(n_paths); peak = np.ones(n_paths); max_dd = np.zeros(n_paths)
    daily = np.empty((days, n_paths))
    for d in range(days):
        f = RNG.poisson(fills, n_paths)
        gain = f * per_fill
        broke = RNG.random(n_paths) < bp
        loss = np.where(broke, GRID["breakout_loss_pct"] / 100.0 * GRID["leverage"], 0.0)
        dr = gain - loss
        eq = np.maximum(eq * (1 + dr), 1e-6)
        peak = np.maximum(peak, eq)
        max_dd = np.maximum(max_dd, (peak - eq) / peak)
        daily[d] = dr
    total = eq - 1.0
    mu_d = daily.mean(axis=0); sd_d = daily.std(axis=0) + 1e-9
    downside = np.where(daily < 0, daily, 0.0)
    dsd = np.sqrt((downside**2).mean(axis=0)) + 1e-9
    return dict(
        regime=regime, days=days,
        median_total_ret=float(np.median(total)), mean_total_ret=float(np.mean(total)),
        p5_total=float(np.percentile(total, 5)), p95_total=float(np.percentile(total, 95)),
        prob_profit=float((total > 0).mean()), prob_ruin_50=float((eq < 0.5).mean()),
        median_maxdd=float(np.median(max_dd)), p95_maxdd=float(np.percentile(max_dd, 95)),
        median_sharpe=float(np.median(mu_d / sd_d * math.sqrt(365))),
        median_sortino=float(np.median(mu_d / dsd * math.sqrt(365))),
    )

# ---------------------------------------------------------------------------
# 6) EJECUCIÓN + CHARTS
# ---------------------------------------------------------------------------
def fmt_pct(x): return f"{x*100:.1f}%"

PRIMARY_LABEL = "Ajustado (cluster ~1.3%)"
WIDE_LABEL = "Ancho (default 8%)"

def main():
    results = {"pump": {}, "grid": {}, "params": {"PUMP": PUMP, "GRID": GRID,
               "SCENARIOS": SCENARIOS, "STOP_REGIMES": STOP_REGIMES,
               "PRIMARY_STOP": PRIMARY_STOP, "N_PATHS": N_PATHS}}

    print("== Modo A: break-even win-rate (stop primario %.1f%%) ==" % PRIMARY_STOP)
    wrs, ep, eb, eo, be = breakeven_sweep(PRIMARY_STOP)
    results["breakeven"] = {"wrs": wrs.tolist(), "exp_pess": ep.tolist(),
                            "exp_base": eb.tolist(), "exp_opt": eo.tolist(),
                            "be_wr": be, "stop": PRIMARY_STOP}
    print("  break-even WR:", {k: (round(v*100,1) if v else None) for k,v in be.items()})

    print("== Modo A2: break-even WR vs ancho de stop (variable pivote) ==")
    stops_x, be_x = breakeven_vs_stop()
    results["breakeven_vs_stop"] = {"stops": stops_x, "be_wr": be_x}

    print("== Modo B: MC pump 500k (ambos regímenes de stop) ==")
    # PRIMARY: 3 escenarios × 3 horizontes completos. WIDE: 3 escenarios a 1 año.
    for stop_label, stop_pct in STOP_REGIMES.items():
        results["pump"][stop_label] = {}
        horizons = HORIZONS if stop_label == PRIMARY_LABEL else {"1 año": 365}
        for sc, p in SCENARIOS.items():
            results["pump"][stop_label][sc] = {}
            for hn, days in horizons.items():
                rec = (stop_label == PRIMARY_LABEL and hn == "1 año" and sc.startswith("Base"))
                m = mc_equity(p["p_win"], p["runner_frac"], days, trades_mult=p["trades_mult"],
                              record_curve=rec, hard_stop=stop_pct)
                results["pump"][stop_label][sc][hn] = m
                print(f"  [{stop_label[:18]:18}] {sc[:20]:20} {hn:8} "
                      f"med={fmt_pct(m['median_total_ret']):>7} P(+)={fmt_pct(m['prob_profit']):>6} "
                      f"Sharpe={m['median_sharpe']:.2f} ruin={fmt_pct(m['prob_ruin_50'])}")

    print("== Grid MC ==")
    for reg in ("chop", "trend"):
        results["grid"][reg] = {hn: mc_grid(days, regime=reg) for hn, days in HORIZONS.items()}
        m = results["grid"][reg]["1 año"]
        print(f"  grid {reg:6} 1a med={fmt_pct(m['median_total_ret']):>7} "
              f"Sharpe={m['median_sharpe']:.2f} ruin={fmt_pct(m['prob_ruin_50'])}")

    # ---- CHARTS ----
    plt.rcParams.update({"figure.dpi": 130, "font.size": 10, "axes.grid": True,
                         "grid.alpha": 0.25})
    PUR = "#a05cf2"
    PRI = results["pump"][PRIMARY_LABEL]

    # C1: break-even WR (stop primario)
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.axhline(0, color="#888", lw=1)
    ax.plot(wrs*100, eb*100, color=PUR, lw=2.4, label="Base")
    ax.plot(wrs*100, ep*100, color="#e8556a", lw=1.6, ls="--", label="Pesimista")
    ax.plot(wrs*100, eo*100, color="#4ea872", lw=1.6, ls="--", label="Optimista")
    if be["base"]:
        ax.axvline(be["base"]*100, color=PUR, ls=":", lw=1.4)
        ax.annotate(f"break-even ≈ {be['base']*100:.1f}%", (be["base"]*100, 0),
                    xytext=(be["base"]*100+3, max(eb.max()*100*0.4, 0.5)), color=PUR)
    ax.set_xlabel("Win-rate (%)"); ax.set_ylabel("Expectancy por trade (%)")
    ax.set_title(f"Pump · Esperanza por trade vs win-rate (stop {PRIMARY_STOP}%)")
    ax.legend(); fig.tight_layout(); fig.savefig(f"{OUT}/c1_breakeven.png"); plt.close()

    # C6: break-even WR vs ancho de stop (LA variable pivote)
    sx = np.array(stops_x); byv = np.array([np.nan if v is None else v*100 for v in be_x])
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.plot(sx, byv, color=PUR, lw=2.6, marker="o", ms=3)
    ax.axvline(1.3, color="#4ea872", ls="--", lw=1.4, label="Cluster ~1.3%")
    ax.axvline(8.0, color="#e8556a", ls="--", lw=1.4, label="Default 8%")
    ax.set_xlabel("Ancho del hard-stop (%)"); ax.set_ylabel("Win-rate de equilibrio (%)")
    ax.set_title("Pump · Win-rate necesario para NO perder, según ancho del stop")
    ax.legend(); fig.tight_layout(); fig.savefig(f"{OUT}/c6_stopwidth.png"); plt.close()

    # C7: flip de rentabilidad — stop ajustado vs ancho (base, 1 año)
    fig, ax = plt.subplots(figsize=(8, 4.2))
    labels = [PRIMARY_LABEL, WIDE_LABEL]
    med = [results["pump"][l]["Base (mercado normal)"]["1 año"]["median_total_ret"]*100 for l in labels]
    x = np.arange(2)
    cols = ["#4ea872", "#e8556a"]
    ax.bar(x, med, 0.5, color=cols)
    ax.axhline(0, color="#888", lw=1)
    for xi, v in zip(x, med):
        ax.annotate(f"{v:+.1f}%", (xi, v), ha="center",
                    va="bottom" if v >= 0 else "top")
    ax.set_xticks(x); ax.set_xticklabels(["Stop ajustado ~1.3%", "Stop ancho 8%"])
    ax.set_ylabel("Retorno mediano 1 año (%)")
    ax.set_title("Pump · El stop decide: retorno mediano según ancho del stop (Base, 1 año)")
    fig.tight_layout(); fig.savefig(f"{OUT}/c7_flip.png"); plt.close()

    # C2: equity fan chart (primario, base 1 año)
    base1 = PRI["Base (mercado normal)"]["1 año"]
    if "curve_pcts" in base1:
        cd = np.array(base1["curve_days"]); cp = np.array(base1["curve_pcts"])
        fig, ax = plt.subplots(figsize=(8, 4.2))
        ax.fill_between(cd, cp[:,0], cp[:,4], color=PUR, alpha=.12, label="P5–P95")
        ax.fill_between(cd, cp[:,1], cp[:,3], color=PUR, alpha=.25, label="P25–P75")
        ax.plot(cd, cp[:,2], color=PUR, lw=2.2, label="Mediana")
        ax.axhline(1.0, color="#888", lw=1, ls="--")
        ax.set_xlabel("Días"); ax.set_ylabel("Equity (×capital)")
        ax.set_title("Pump · Abanico de equity 500k paths — escenario Base, 1 año")
        ax.legend(); fig.tight_layout(); fig.savefig(f"{OUT}/c2_fan.png"); plt.close()

    # C3: distribución de retorno total (base 1 año)
    if "sample_total_ret" in base1:
        s = np.array(base1["sample_total_ret"])*100
        fig, ax = plt.subplots(figsize=(8, 4.2))
        ax.hist(np.clip(s, -100, 300), bins=120, color=PUR, alpha=.8)
        ax.axvline(0, color="#e8556a", lw=1.4, ls="--")
        ax.axvline(np.median(s), color="#fff", lw=1.4)
        ax.set_xlabel("Retorno total a 1 año (%)"); ax.set_ylabel("Frecuencia")
        ax.set_title("Pump · Distribución de resultado — Base, 1 año (cola derecha = runners)")
        fig.tight_layout(); fig.savefig(f"{OUT}/c3_dist.png"); plt.close()

    # C4: Sharpe + P(profit) por escenario (1 año)
    scs = list(SCENARIOS.keys())
    shp = [PRI[s]["1 año"]["median_sharpe"] for s in scs]
    pp = [PRI[s]["1 año"]["prob_profit"]*100 for s in scs]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    x = np.arange(len(scs))
    ax.bar(x-0.2, shp, 0.4, color=PUR, label="Sharpe (mediana)")
    ax2 = ax.twinx(); ax2.bar(x+0.2, pp, 0.4, color="#4ea872", label="P(profit) %")
    ax.set_xticks(x); ax.set_xticklabels([s.split(" (")[0] for s in scs], fontsize=8)
    ax.set_ylabel("Sharpe"); ax2.set_ylabel("P(profit) %")
    ax.set_title(f"Pump · Sharpe y prob. de ganar por escenario — 1 año (stop {PRIMARY_STOP}%)")
    fig.tight_layout(); fig.savefig(f"{OUT}/c4_scenarios.png"); plt.close()

    # C5: grid chop vs trend (1 año)
    fig, ax = plt.subplots(figsize=(8, 4.2))
    regs = ["chop", "trend"]
    med = [results["grid"][r]["1 año"]["median_total_ret"]*100 for r in regs]
    p5 = [results["grid"][r]["1 año"]["p5_total"]*100 for r in regs]
    p95 = [results["grid"][r]["1 año"]["p95_total"]*100 for r in regs]
    x = np.arange(2)
    ax.bar(x, med, 0.5, color=PUR, label="Mediana")
    ax.errorbar(x, med, yerr=[np.array(med)-np.array(p5), np.array(p95)-np.array(med)],
                fmt="none", ecolor="#888", capsize=6, label="P5–P95")
    ax.axhline(0, color="#e8556a", lw=1, ls="--")
    ax.set_xticks(x); ax.set_xticklabels(["Lateral (chop)", "Tendencia (trend)"])
    ax.set_ylabel("Retorno total 1 año (%)")
    ax.set_title("Grid GRVT · Retorno por régimen — 1 año")
    ax.legend(); fig.tight_layout(); fig.savefig(f"{OUT}/c5_grid.png"); plt.close()

    with open(f"{OUT}/results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nOK -> charts + results.json en {OUT}")

if __name__ == "__main__":
    main()
