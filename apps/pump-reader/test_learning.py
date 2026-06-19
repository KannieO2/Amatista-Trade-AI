"""test_learning.py — verifica que el módulo de learning produce métricas.

SOLO LECTURA + simulación EN MEMORIA. No toca el bot en producción, ni su DB,
ni el .env. Crea un LearningLab NUEVO, le inyecta alertas sintéticas realistas,
fuerza la liquidación (como si pasaran 7 días) y muestra precision / recall /
avg lead / proposals / components.

POR QUÉ tu dashboard muestra "34 alertas / 0 settled / sin métricas":
  Una alerta SOLO se liquida (settle) cuando pasa el horizonte de 7 días
  (PUMP_LEARN_HORIZON_DAYS=7). El bot lleva horas, no 7 días → 0 settled.
  Sin outcomes settled NO hay precision/recall/proposals. Es POR DISEÑO, no un
  bug. Este script demuestra que EN CUANTO hay settled, las métricas salen bien.

NOTA: las 34 alertas reales viven en la MEMORIA del proceso del bot (LearningLab
no se persiste a disco), así que un script aparte no puede leerlas. Por eso aquí
se reconstruye un set sintético realista para validar la LÓGICA del pipeline.

Ejecutar (Windows PowerShell, desde la raíz del repo):
    cd apps/pump-reader
    ../../.venv/Scripts/python.exe test_learning.py
"""
from __future__ import annotations

import random
import sys
from datetime import UTC, datetime, timedelta

# Consola de Windows en cp1252 rompe con acentos/símbolos — forzar UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.learning import (
    HORIZON_DAYS,
    MIN_SAMPLES_COMPONENTS,
    MIN_SETTLED_FOR_PROPOSAL,
    PUMP_MOVE_PCT,
    LearningLab,
)

random.seed(7)


def build_lab() -> LearningLab:
    lab = LearningLab()
    # long_pump >= MIN_SAMPLES_COMPONENTS para que un cluster quede "ready"
    clusters = ["long_pump"] * 24 + ["classic"] * 10
    now = datetime.now(UTC)
    for i, cluster in enumerate(clusters):
        price = round(random.uniform(0.001, 2.0), 6)
        confirmed = random.random() < 0.35            # ~35% pumpean (precision < 0.5)
        vol = random.uniform(4, 12) if confirmed else random.uniform(1, 3)
        lab.record_alert(
            symbol=f"SIM{i}/USDT", exchange="mexc", alert_price=price,
            pump_score=random.randint(50, 95), cluster=cluster,
            classification="active_pump" if confirmed else "volume_anomaly",
            signals={
                "volume_spike": round(vol, 2),
                "price_change_pct_24h": round(random.uniform(5, 40), 1),
                "orderbook_imbalance": round(random.uniform(0.5, 0.9), 2),
                "liquidity_usd": round(random.uniform(40_000, 300_000)),
            },
        )
        o = lab.outcomes[-1]
        # envejecer la alerta 8 días -> settle_due la liquidará (horizonte 7d)
        o.alert_at = now - timedelta(days=8)
        # simular el recorrido: confirmados >= PUMP_MOVE_PCT, el resto plano
        mult = random.uniform(1.20, 1.65) if confirmed else random.uniform(0.95, 1.12)
        o.peak_price = round(price * mult, 8)
        o.peak_24h = o.peak_price
        o.low_price = round(price * random.uniform(0.85, 0.99), 8)
        o.last_price = o.peak_price
        o.peak_at = o.alert_at + timedelta(minutes=random.randint(20, 240))
    # unos pumps que el bot NO alertó -> bajan el recall
    for s in ("MISS1", "MISS2", "MISS3"):
        lab.record_missed(s, "mexc")
    return lab


def show(title):
    print("\n" + "=" * 58 + f"\n  {title}\n" + "=" * 58)


def main() -> None:
    print(f"Config learning: horizonte={HORIZON_DAYS}d · pump>= {PUMP_MOVE_PCT}% MFE")
    print(f"  proposals necesitan >= {MIN_SETTLED_FOR_PROPOSAL} settled")
    print(f"  components necesitan >= {MIN_SAMPLES_COMPONENTS} muestras por cluster")

    lab = build_lab()

    show("ANTES de liquidar (igual que tu dashboard ahora)")
    m0 = lab.metrics()
    print(f"  alertas={m0['n_alerts']}  settled={m0['n_settled']}  "
          f"precision={m0['precision']}  proposals={len(m0['proposals'])}")
    assert m0["n_settled"] == 0, "esperaba 0 settled antes de forzar el horizonte"
    print("  -> 0 settled / sin métricas = EXACTAMENTE lo que ves. Correcto.")

    show("FORZAR liquidación (simular que pasaron 7 días)")
    lab.settle_due()
    m = lab.metrics()
    print(f"  alertas={m['n_alerts']}  settled={m['n_settled']}  "
          f"confirmed={m['n_confirmed']}  missed={m['n_missed']}")

    show("MÉTRICAS (lo que el módulo produce cuando hay settled)")
    pr = m["precision"]; rc = m["recall"]; lead = m["avg_lead_secs"]
    print(f"  Precisión .......... {pr:.0%}" if pr is not None else "  Precisión .......... —")
    print(f"  Recall (est) ....... {rc:.0%}" if rc is not None else "  Recall (est) ....... —")
    print(f"  Avg lead time ...... {lead/60:.1f} min" if lead is not None else "  Avg lead time ...... —")

    show("PROPUESTAS (auto-ajuste de umbral)")
    props = m["proposals"]
    if props:
        for p in props:
            print(f"  [{p['kind']}] {p['text']}")
    else:
        print("  (sin propuestas — se necesitan más settled)")

    show("COMPONENTES (qué señal predice el pump, por cluster)")
    for cluster, comp in m["components"].items():
        if comp.get("ready"):
            top = comp["contrib"][:3]
            print(f"  {cluster}: READY — top señales:")
            for c in top:
                print(f"     {c['signal']:24} lift={c['lift']:+.3f}")
        else:
            print(f"  {cluster}: faltan datos ({comp.get('have')}/{comp.get('need')})")

    show("VEREDICTO")
    ok = (m["n_settled"] > 0 and pr is not None and rc is not None
          and lead is not None and len(props) > 0)
    if ok:
        print("  [OK] El modulo de learning FUNCIONA. Liquida, calcula precision/")
        print("       recall/lead, genera propuestas y ranking de componentes.")
        print("  [OK] Tu '0 settled' en produccion es solo el horizonte de 7 dias.")
    else:
        print("  [FALLA] Algo no produjo metricas — revisar arriba que quedo en None.")


if __name__ == "__main__":
    main()
