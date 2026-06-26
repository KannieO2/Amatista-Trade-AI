"""Recomputa SOLO la sección grid (modelo corregido) y regenera c5_grid.png +
parchea results.json, sin re-correr el pump (35 min)."""
import json, os
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from montecarlo_rentabilidad import mc_grid, HORIZONS, OUT, GRID

def fmt(x): return f"{x*100:.1f}%"

r = json.load(open(f"{OUT}/results.json", encoding="utf-8"))
r["params"]["GRID"] = GRID
r["grid"] = {}
for reg in ("chop", "trend"):
    r["grid"][reg] = {hn: mc_grid(days, regime=reg) for hn, days in HORIZONS.items()}
    m = r["grid"][reg]["1 año"]
    print(f"grid {reg:6} 1a med={fmt(m['median_total_ret']):>8} "
          f"Sharpe={m['median_sharpe']:.2f} maxDD={fmt(m['median_maxdd'])} "
          f"P(+)={fmt(m['prob_profit'])} ruin={fmt(m['prob_ruin_50'])}")

# regen c5_grid
PUR = "#a05cf2"
plt.rcParams.update({"figure.dpi": 130, "font.size": 10, "axes.grid": True, "grid.alpha": .25})
fig, ax = plt.subplots(figsize=(8, 4.2))
regs = ["chop", "trend"]
med = [r["grid"][g]["1 año"]["median_total_ret"]*100 for g in regs]
p5 = [r["grid"][g]["1 año"]["p5_total"]*100 for g in regs]
p95 = [r["grid"][g]["1 año"]["p95_total"]*100 for g in regs]
x = np.arange(2)
ax.bar(x, med, 0.5, color=[ "#4ea872", "#e8556a"])
ax.errorbar(x, med, yerr=[np.array(med)-np.array(p5), np.array(p95)-np.array(med)],
            fmt="none", ecolor="#888", capsize=6)
ax.axhline(0, color="#888", lw=1)
for xi, v in zip(x, med):
    ax.annotate(f"{v:+.1f}%", (xi, v), ha="center", va="bottom" if v >= 0 else "top")
ax.set_xticks(x); ax.set_xticklabels(["Lateral (chop)", "Tendencia (trend)"])
ax.set_ylabel("Retorno total 1 año (%)")
ax.set_title("Grid GRVT · Retorno por régimen — 1 año (P5–P95)")
fig.tight_layout(); fig.savefig(f"{OUT}/c5_grid.png"); plt.close()

json.dump(r, open(f"{OUT}/results.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2, default=str)
print("OK -> grid parcheado + c5 regenerado")
