"""FASE 2 — motor de scores temporales (detección de PREPARACIÓN, no momentum).

Cada score es una FUNCIÓN PURA de una ventana W = lista ordenada (vieja→nueva) de
filas de micro_snapshots (dicts con las columnas de MicroStore). No toca red, ni
estado, ni la estrategia: solo mira la película de un símbolo y devuelve enteros
0-100 + sus componentes (para el Decision Log y el dashboard).

Spec: TradeOS_Fase2_Especificacion.docx
  - AccumulationScore  : ¿alguien COMPRA sin mover el precio? (absorción)
  - PersistenceScore   : ¿la señal se SOSTIENE en el tiempo? (anti-ruido)
  - RugRiskScore       : ¿el libro se DETERIORA? (eje ortogonal al alza)
  - sequence_bonus     : ¿la rampa ACELERA? (la forma > el nivel)

REGLA del audit: PumpScore (alza) y RugRiskScore (colapso) son ejes ortogonales.
La decisión es una MATRIZ (ENTRAR sii Acc≥A y Pers≥P y Rug≤R), nunca una suma.

Los umbrales de normalización son CONSTANTES PROVISIONALES a calibrar con los
percentiles empíricos de micro_snapshots (Fase 1). Están todos arriba, nombrados.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from statistics import mean

EPS = 1e-9

# --- constantes de normalización (a calibrar con datos de Fase 1) -------------
FLAT_PRICE_BAND = float(os.getenv("PUMP_F2_FLAT_PRICE_BAND", "0.02"))   # |Δprecio| "plano"
HEALTHY_IMB_LO = float(os.getenv("PUMP_F2_IMB_LO", "0.60"))            # imbalance comprador sano
HEALTHY_IMB_HI = float(os.getenv("PUMP_F2_IMB_HI", "0.85"))           # por encima = blow-off
PERSIST_IMB_MIN = float(os.getenv("PUMP_F2_PERSIST_IMB", "0.65"))     # imbalance "sostenido"
CONCENTRATION_HI = float(os.getenv("PUMP_F2_CONCENTRATION", "0.80"))  # top_book_share alto
MOVE_NO_SUPPORT_PCT = float(os.getenv("PUMP_F2_MOVE_PCT", "0.01"))    # |Δprecio| de "empujón"
MIN_WINDOW = int(os.getenv("PUMP_F2_MIN_WINDOW", "5"))                # filas mínimas para puntuar

# pesos (suman 100 dentro de cada score) — del spec, calibrables
W_ACC = (30, 20, 20, 15, 15)   # vol↑+plano, bid↑, absorción, spread↓, imbalance sano
W_RUG = (25, 20, 20, 25, 10)   # liq↓, spread↑, concentración, mov sin soporte, retirada bid


# ============================ helpers numéricos ============================
def _col(W: list[dict], name: str) -> list[float]:
    return [float(r.get(name) or 0.0) for r in W]


def _rel_trend(xs: list[float]) -> float:
    """Tendencia neta relativa en [-1, 1]. >0 sube, <0 baja. Independiente de
    unidades: (último - primero) / (media de |xs|)."""
    if len(xs) < 2:
        return 0.0
    scale = mean([abs(x) for x in xs]) + EPS
    return _clamp((xs[-1] - xs[0]) / scale, -1.0, 1.0)


def _mono_up(xs: list[float]) -> float:
    """Grado de monotonía creciente en [0,1] = pasos crecientes / (n-1)."""
    if len(xs) < 2:
        return 0.0
    inc = sum(1 for a, b in zip(xs, xs[1:]) if b > a)
    return inc / (len(xs) - 1)


def _frac(xs: list[float], pred) -> float:
    return (sum(1 for x in xs if pred(x)) / len(xs)) if xs else 0.0


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _absorption_series(W: list[dict]) -> list[float]:
    """Por intervalo: 1.0 si el precio cae (o queda) pero el bid_depth NO cede
    (la venta se absorbe), si no 0.0. Firma de acumulación."""
    out: list[float] = []
    for a, b in zip(W, W[1:]):
        dprice = float(b.get("last_price") or 0) - float(a.get("last_price") or 0)
        dbid = float(b.get("bid_depth") or 0) - float(a.get("bid_depth") or 0)
        out.append(1.0 if (dprice <= 0 and dbid >= 0) else 0.0)
    return out


# ============================ scores ============================
@dataclass
class ScoreSet:
    accumulation: int
    persistence: int
    rug_risk: int
    sequence_bonus: float
    n: int
    components: dict           # subscores transparentes para log/dashboard

    def as_dict(self) -> dict:
        return asdict(self)


def accumulation_score(W: list[dict]) -> tuple[int, dict]:
    """¿Compran sin mover el precio? Spec §2. Pesos 30/20/20/15/15."""
    if len(W) < MIN_WINDOW:
        return 0, {}
    price = _col(W, "last_price")
    price_move = abs(price[-1] - price[0]) / (abs(price[0]) + EPS)
    flat = _clamp(1.0 - price_move / FLAT_PRICE_BAND)        # 1 plano … 0 se movió >banda

    f_vol_flat = _clamp(_rel_trend(_col(W, "volume"))) * flat        # vol↑ con precio plano
    f_bid = _clamp(_rel_trend(_col(W, "bid_depth")))                 # profundidad bid↑
    f_abs = mean(_absorption_series(W)) if len(W) > 1 else 0.0       # absorción de ventas
    f_spread = _clamp(-_rel_trend(_col(W, "spread_pct")))            # spread comprimiéndose
    imb = mean(_col(W, "imbalance"))
    # Sesgo comprador sano con RAMPA suave (antes binario 1/0: un imb 0.86 — apenas
    # sobre 0.85 — caía a 0 de golpe, un cliff que descartaba un casi-sano por un pelo).
    # Crédito pleno dentro de [LO,HI]; rampa lineal 0.50→LO subiendo y HI→0.95 bajando
    # hacia blow-off. Continuo y monótono a cada lado del rango sano.
    if HEALTHY_IMB_LO <= imb <= HEALTHY_IMB_HI:
        f_imb = 1.0
    elif imb < HEALTHY_IMB_LO:
        f_imb = _clamp((imb - 0.50) / (HEALTHY_IMB_LO - 0.50))
    else:                                                            # imb > HI = blow-off
        f_imb = _clamp((0.95 - imb) / (0.95 - HEALTHY_IMB_HI))

    parts = (f_vol_flat, f_bid, f_abs, f_spread, f_imb)
    score = sum(w * f for w, f in zip(W_ACC, parts))
    comp = {"vol_flat": round(f_vol_flat, 3), "bid_up": round(f_bid, 3),
            "absorption": round(f_abs, 3), "spread_compress": round(f_spread, 3),
            "imbalance_healthy": round(f_imb, 3), "mean_imbalance": round(imb, 3)}
    return int(_clamp(score, 0, 100)), comp


def persistence_score(W: list[dict]) -> tuple[int, dict]:
    """¿La señal se sostiene? Spec §3. 25 cada componente."""
    if len(W) < MIN_WINDOW:
        return 0, {}
    g_imb = _frac(_col(W, "imbalance"), lambda x: x > PERSIST_IMB_MIN)
    g_vol = _mono_up(_col(W, "volume"))
    g_liq = _mono_up(_col(W, "liquidity_usd"))
    g_abs = mean(_absorption_series(W)) if len(W) > 1 else 0.0
    score = 25 * g_imb + 25 * g_vol + 25 * g_liq + 25 * g_abs
    comp = {"imb_sustained": round(g_imb, 3), "vol_monotonic": round(g_vol, 3),
            "liq_monotonic": round(g_liq, 3), "abs_sustained": round(g_abs, 3)}
    return int(_clamp(score, 0, 100)), comp


def rug_risk_score(W: list[dict]) -> tuple[int, dict]:
    """¿El libro se deteriora? Spec §4. Pesos 25/20/20/25/10. Eje ORTOGONAL."""
    if len(W) < MIN_WINDOW:
        return 0, {}
    f_liq = _clamp(-_rel_trend(_col(W, "liquidity_usd")))    # liquidez desapareciendo
    f_spread = _clamp(_rel_trend(_col(W, "spread_pct")))     # spread ensanchando
    conc = mean(_col(W, "top_book_share"))
    f_conc = _clamp((conc - CONCENTRATION_HI) / (1 - CONCENTRATION_HI)) if conc > CONCENTRATION_HI else 0.0

    # movimiento sin soporte: fracción de barras con |Δprecio| alto y profundidad
    # por debajo de la mediana de la ventana (precio empujado sin libro real).
    price = _col(W, "last_price")
    bid = _col(W, "bid_depth")
    med_bid = sorted(bid)[len(bid) // 2]
    trap = 0
    # Por intervalo: precio se MUEVE fuerte (|Δprice| > umbral) pero la profundidad
    # bid del bar al que se movió está por DEBAJO de la mediana de la ventana =
    # precio empujado sin libro real. (pa,ba)=bar i, (pb,bb)=bar i+1.
    for (pa, ba), (pb, bb) in zip(zip(price, bid), zip(price[1:], bid[1:])):
        dmove = abs(pb - pa) / (abs(pa) + EPS)
        if dmove > MOVE_NO_SUPPORT_PCT and bb < med_bid:
            trap += 1
    f_trap = trap / max(1, len(W) - 1)

    # retirada súbita de bid: peor caída relativa de un intervalo a otro.
    f_pull = 0.0
    for a, b in zip(bid, bid[1:]):
        if a > 0:
            f_pull = max(f_pull, _clamp((a - b) / a))

    parts = (f_liq, f_spread, f_conc, f_trap, f_pull)
    score = sum(w * f for w, f in zip(W_RUG, parts))
    comp = {"liq_vanishing": round(f_liq, 3), "spread_widening": round(f_spread, 3),
            "concentration": round(f_conc, 3), "move_no_support": round(f_trap, 3),
            "bid_pull": round(f_pull, 3), "mean_top_share": round(conc, 3)}
    return int(_clamp(score, 0, 100)), comp


def sequence_bonus(xs: list[float]) -> float:
    """Spec §5: la FORMA importa. Premia rampas que aceleran (ratios crecientes)."""
    xs = [x for x in xs if x is not None]
    if len(xs) < 3:
        return 0.0
    ratios = [b / a for a, b in zip(xs, xs[1:]) if a > 0]
    if len(ratios) < 2:
        return 0.0
    magnitude = _clamp(mean(ratios) - 1.0)
    accelerating = all(b >= a for a, b in zip(ratios, ratios[1:]))
    return round(magnitude * (1.0 if accelerating else 0.3), 3)


def evaluate(W: list[dict]) -> ScoreSet:
    """Evalúa los tres scores + bonus de secuencia sobre la ventana. Aplica el
    sequence_bonus como multiplicador suave a Accumulation y Persistence (spec §5)."""
    acc, acc_c = accumulation_score(W)
    per, per_c = persistence_score(W)
    rug, rug_c = rug_risk_score(W)
    seq = sequence_bonus(_col(W, "volume")) if W else 0.0
    boost = 1.0 + 0.25 * seq        # hasta +25% por aceleración real de volumen
    acc = int(_clamp(acc * boost, 0, 100))
    per = int(_clamp(per * boost, 0, 100))
    return ScoreSet(accumulation=acc, persistence=per, rug_risk=rug,
                    sequence_bonus=seq, n=len(W),
                    components={"accumulation": acc_c, "persistence": per_c, "rug_risk": rug_c})
