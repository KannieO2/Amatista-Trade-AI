"""Learning lab — did the bot alert BEFORE the pump, and was it right?

Every confirmation alert becomes an *outcome* we then track from live price:

  MFE (max favorable excursion)  = best gain since the alert  (24h and 7d)
  MAE (max adverse excursion)    = worst drawdown since the alert
  lead time                      = alert -> peak  (positive = alerted early)
  label                          = confirmed_pump (MFE >= PUMP_MOVE_PCT)
                                    / no_pump (settled, never ran)

An outcome *settles* once it is past the 7-day horizon. From settled outcomes:

  precision    = confirmed / settled alerts
  recall (est) = confirmed / (confirmed + user-reported missed pumps)
  avg lead     = mean lead time over confirmed alerts

Threshold *proposals* only appear once there are enough settled outcomes, so the
bot never "learns" from noise. Detection-only learning starts ~7 days after
deploy (the first horizon).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import mean
from uuid import uuid4

HORIZON_DAYS = float(os.getenv("PUMP_LEARN_HORIZON_DAYS", "7"))
PUMP_MOVE_PCT = float(os.getenv("PUMP_LEARN_MOVE_PCT", "20"))     # MFE to count as a real pump
WINDOW_DAYS = float(os.getenv("PUMP_LEARN_WINDOW_DAYS", "30"))
MIN_SETTLED_FOR_PROPOSAL = int(os.getenv("PUMP_LEARN_MIN_SETTLED", "8"))
MIN_SAMPLES_COMPONENTS = int(os.getenv("PUMP_LEARN_MIN_SAMPLES", "20"))
# Fast-settle: the 7-day horizon meant NOTHING settled for a week, so the engine
# never learned. Settle the OBVIOUS cases within hours instead — a token that
# already cleared the big move is a confirmed pump; one that never even moved
# FAST_SETTLE_MFE% after FAST_SETTLE_HOURS is dead (no_pump). The ambiguous middle
# still waits for the full horizon.
FAST_SETTLE_HOURS = float(os.getenv("PUMP_LEARN_FAST_SETTLE_HOURS", "6"))
FAST_SETTLE_MFE = float(os.getenv("PUMP_LEARN_FAST_SETTLE_MFE", "2"))
# Live-tracked alerts per tick. Was 40 — too small: older unsettled alerts fell out
# of tracking so their peak/MFE never updated and they wrongly settled as no_pump.
MAX_ACTIVE = int(os.getenv("PUMP_LEARN_MAX_ACTIVE", "120"))


def _vol_bucket(v) -> int:
    """Volume-spike tier: 0=<3x, 1=3-6x, 2=>=6x. The measured data says >6x is the
    only historically profitable bucket, so it's the key conditioning variable."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        v = 0.0
    return 2 if v >= 6 else 1 if v >= 3 else 0


@dataclass
class Outcome:
    symbol: str
    exchange: str
    source: str                     # 'alert' | 'missed'
    alert_at: datetime
    alert_price: float
    pump_score: int = 0
    cluster: str = "long_pump"
    classification: str = "n/a"
    signals: dict = field(default_factory=dict)
    peak_price: float = 0.0
    peak_at: datetime | None = None
    peak_24h: float = 0.0
    low_price: float = 0.0
    last_price: float = 0.0
    settled: bool = False
    label: str = "pending"
    id: str = field(default_factory=lambda: str(uuid4()))

    def mfe_7d(self) -> float:
        return (self.peak_price - self.alert_price) / self.alert_price * 100 if self.alert_price > 0 else 0.0

    def mfe_24h(self) -> float:
        return (self.peak_24h - self.alert_price) / self.alert_price * 100 if self.alert_price > 0 else 0.0

    def mae_7d(self) -> float:
        return (self.low_price - self.alert_price) / self.alert_price * 100 if self.alert_price > 0 else 0.0

    def lead_secs(self) -> float:
        if not self.peak_at:
            return 0.0
        return max(0.0, (self.peak_at - self.alert_at).total_seconds())

    # --- persistence round-trip (one row per outcome) ------------------------
    def to_row(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "exchange": self.exchange,
            "source": self.source, "alert_at": self.alert_at.isoformat(),
            "alert_price": self.alert_price, "pump_score": self.pump_score,
            "cluster": self.cluster, "classification": self.classification,
            "signals": self.signals, "peak_price": self.peak_price,
            "peak_at": self.peak_at.isoformat() if self.peak_at else None,
            "peak_24h": self.peak_24h, "low_price": self.low_price,
            "last_price": self.last_price, "settled": bool(self.settled), "label": self.label,
        }

    @classmethod
    def from_row(cls, r: dict) -> "Outcome":
        def _dt(v):
            return datetime.fromisoformat(v) if v else None
        sig = r.get("signals")
        if isinstance(sig, str):
            import json
            try:
                sig = json.loads(sig)
            except Exception:
                sig = {}
        return cls(
            symbol=r.get("symbol", ""), exchange=r.get("exchange", ""),
            source=r.get("source", "alert"),
            alert_at=_dt(r.get("alert_at")) or datetime.now(UTC),
            alert_price=float(r.get("alert_price") or 0.0),
            pump_score=int(r.get("pump_score") or 0),
            cluster=r.get("cluster") or "long_pump",
            classification=r.get("classification") or "n/a",
            signals=sig or {}, peak_price=float(r.get("peak_price") or 0.0),
            peak_at=_dt(r.get("peak_at")), peak_24h=float(r.get("peak_24h") or 0.0),
            low_price=float(r.get("low_price") or 0.0),
            last_price=float(r.get("last_price") or 0.0),
            settled=bool(r.get("settled")), label=r.get("label") or "pending",
            id=r.get("id") or str(uuid4()),
        )


class LearningLab:
    def __init__(self) -> None:
        self.outcomes: list[Outcome] = []

    # --- persistence (so MFE/MAE/lead-time accumulate across restarts) --------
    def export_rows(self) -> list[dict]:
        """All outcomes as DB rows (for periodic upsert by main.py)."""
        return [o.to_row() for o in self.outcomes]

    def load_rows(self, rows: list[dict]) -> int:
        """Rebuild outcomes from persisted rows at startup. Replaces in-memory
        state (deduped by id, newest 500 kept)."""
        loaded: list[Outcome] = []
        seen: set[str] = set()
        for r in rows or []:
            try:
                o = Outcome.from_row(r)
            except Exception:
                continue
            if o.id in seen:
                continue
            seen.add(o.id)
            loaded.append(o)
        loaded.sort(key=lambda o: o.alert_at)
        self.outcomes = loaded[-500:]
        return len(self.outcomes)

    # --- recording -----------------------------------------------------------
    def record_alert(self, *, symbol: str, exchange: str, alert_price: float,
                     pump_score: int, cluster: str, classification: str,
                     signals: dict | None = None) -> None:
        if alert_price <= 0:
            return
        now = datetime.now(UTC)
        # Dedupe: skip if an unsettled alert for this token fired in the last 6h.
        for o in self.outcomes:
            if (o.exchange == exchange and o.symbol == symbol and o.source == "alert"
                    and not o.settled and (now - o.alert_at) < timedelta(hours=6)):
                return
        self.outcomes.append(Outcome(
            symbol=symbol, exchange=exchange, source="alert", alert_at=now,
            alert_price=alert_price, pump_score=pump_score, cluster=cluster,
            classification=classification, signals=signals or {},
            peak_price=alert_price, peak_at=now, peak_24h=alert_price, low_price=alert_price,
            last_price=alert_price,
        ))
        del self.outcomes[:-500]

    def record_missed(self, symbol: str, exchange: str = "n/a") -> dict:
        """User reports a pump the bot did NOT alert — lowers recall."""
        now = datetime.now(UTC)
        self.outcomes.append(Outcome(
            symbol=symbol.upper(), exchange=exchange.lower(), source="missed",
            alert_at=now, alert_price=0.0, settled=True, label="missed",
        ))
        return {"recorded": True, "symbol": symbol.upper()}

    def record_dangerous(self, *, symbol: str, exchange: str, cluster: str = "long_pump",
                         signals: dict | None = None, reason: str = "") -> None:
        """Data-integrity §4: un token BLOQUEADO por un tell de scam/rug (concentración
        de holders, dump on-chain, MANIPULATION_SUSPECT). NUNCA se borra — es la 3ª
        clase del aprendizaje (Successful / Failed / Dangerous). No tiene MFE (no se
        compró), así que NO cuenta para precision; sirve para que pump_probability
        PENALICE activamente el perfil de señal que llevó a estafas. Dedupe 6h."""
        now = datetime.now(UTC)
        for o in self.outcomes:
            if (o.exchange == exchange and o.symbol == symbol and o.source == "dangerous"
                    and (now - o.alert_at) < timedelta(hours=6)):
                return
        sig = dict(signals or {})
        if reason:
            sig.setdefault("reason", reason)
        self.outcomes.append(Outcome(
            symbol=symbol.upper(), exchange=exchange.lower(), source="dangerous",
            alert_at=now, alert_price=0.0, cluster=cluster, classification="dangerous",
            signals=sig, settled=True, label="dangerous",
        ))
        del self.outcomes[:-500]

    # --- live updates --------------------------------------------------------
    def active_symbols(self) -> list[tuple[str, str]]:
        seen, out = set(), []
        for o in self.outcomes:
            if o.source == "alert" and not o.settled:
                key = (o.exchange, o.symbol)
                if key not in seen:
                    seen.add(key); out.append(key)
        return out[:MAX_ACTIVE]

    def step(self, exchange: str, symbol: str, price: float) -> None:
        if price <= 0:
            return
        now = datetime.now(UTC)
        for o in self.outcomes:
            if o.source != "alert" or o.settled or o.exchange != exchange or o.symbol != symbol:
                continue
            o.last_price = price
            if price > o.peak_price:
                o.peak_price = price
                o.peak_at = now
            if o.low_price <= 0 or price < o.low_price:
                o.low_price = price
            if (now - o.alert_at) <= timedelta(hours=24) and price > o.peak_24h:
                o.peak_24h = price

    def settle_due(self) -> None:
        now = datetime.now(UTC)
        for o in self.outcomes:
            if o.source != "alert" or o.settled:
                continue
            age = now - o.alert_at
            if age >= timedelta(days=HORIZON_DAYS):
                o.settled = True
                o.label = "confirmed_pump" if o.mfe_7d() >= PUMP_MOVE_PCT else "no_pump"
            elif age >= timedelta(hours=FAST_SETTLE_HOURS):
                # Fast-settle the obvious cases so learning has data within hours.
                mfe = o.mfe_7d()
                if mfe >= PUMP_MOVE_PCT:
                    o.settled, o.label = True, "confirmed_pump"
                elif mfe < FAST_SETTLE_MFE:
                    o.settled, o.label = True, "no_pump"

    # --- metrics -------------------------------------------------------------
    def _in_window(self) -> list[Outcome]:
        cutoff = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
        return [o for o in self.outcomes if o.alert_at >= cutoff]

    def metrics(self) -> dict:
        win = self._in_window()
        alerts = [o for o in win if o.source == "alert"]
        settled = [o for o in alerts if o.settled]
        confirmed = [o for o in settled if o.label == "confirmed_pump"]
        missed = [o for o in win if o.source == "missed"]
        dangerous = [o for o in win if o.source == "dangerous"]

        precision = (len(confirmed) / len(settled)) if settled else None
        recall_den = len(confirmed) + len(missed)
        recall = (len(confirmed) / recall_den) if recall_den else None
        avg_lead = mean(o.lead_secs() for o in confirmed) if confirmed else None

        return {
            "window_days": WINDOW_DAYS,
            "horizon_days": HORIZON_DAYS,
            "n_alerts": len(alerts),
            "n_settled": len(settled),
            "n_confirmed": len(confirmed),
            "n_missed": len(missed),
            "n_dangerous": len(dangerous),
            "precision": round(precision, 3) if precision is not None else None,
            "recall": round(recall, 3) if recall is not None else None,
            "avg_lead_secs": round(avg_lead) if avg_lead is not None else None,
            "components": self._components(settled),
            "proposals": self._proposals(settled, confirmed, precision, avg_lead),
        }

    def _components(self, settled: list[Outcome]) -> dict:
        out = {}
        for cluster in ("classic", "long_pump"):
            rows = [o for o in settled if o.cluster == cluster and o.signals]
            if len(rows) < MIN_SAMPLES_COMPONENTS:
                out[cluster] = {"ready": False, "have": len(rows), "need": MIN_SAMPLES_COMPONENTS}
                continue
            keys = ["volume_spike", "price_change_pct_24h", "orderbook_imbalance", "liquidity_usd"]
            contrib = []
            for k in keys:
                conf = [o.signals.get(k, 0) for o in rows if o.label == "confirmed_pump"]
                noo = [o.signals.get(k, 0) for o in rows if o.label != "confirmed_pump"]
                if conf and noo:
                    mc, mn = mean(conf), mean(noo)
                    # Lift NORMALIZADO (dimensionless [-1,1]): separación relativa
                    # confirmed-vs-dud. Sin esto, liquidity_usd ($) aplasta a
                    # volume_spike (x) y la barra era ilegible (-61323). El signo dice
                    # la dirección (ej liquidez negativa = microcaps pumpean).
                    denom = abs(mc) + abs(mn)
                    norm = (mc - mn) / denom if denom else 0.0
                    contrib.append({"signal": k, "lift": round(norm, 3),
                                    "conf": round(mc, 2), "dud": round(mn, 2)})
            out[cluster] = {"ready": True, "contrib": sorted(contrib, key=lambda x: -abs(x["lift"]))}
        return out

    def _proposals(self, settled, confirmed, precision, avg_lead) -> list[dict]:
        if len(settled) < MIN_SETTLED_FOR_PROPOSAL:
            return []
        props = []
        if precision is not None and precision < 0.5:
            props.append({"kind": "raise_threshold",
                          "text": f"Precision {precision:.0%} is low — raise the confirmation threshold +5 to cut false alerts."})
        if confirmed and avg_lead is not None and avg_lead < 3600:
            props.append({"kind": "lower_threshold",
                          "text": "Confirmed pumps peaked <1h after the alert — lower the threshold to alert earlier."})
        if precision is not None and precision >= 0.7 and not props:
            props.append({"kind": "hold",
                          "text": f"Precision {precision:.0%} and lead time healthy — keep the current threshold."})
        return props

    def table(self, limit: int = 50) -> list[dict]:
        rows = sorted(self.outcomes, key=lambda o: o.alert_at, reverse=True)[:limit]
        return [{
            "symbol": o.symbol, "exchange": o.exchange, "cluster": o.cluster,
            "pump_score": o.pump_score, "label": o.label, "source": o.source,
            "settled": o.settled,
            "mfe_24h": round(o.mfe_24h(), 1) if o.source == "alert" else None,
            "mfe_7d": round(o.mfe_7d(), 1) if o.source == "alert" else None,
            "mae_7d": round(o.mae_7d(), 1) if o.source == "alert" else None,
            "lead_mins": round(o.lead_secs() / 60) if o.source == "alert" else None,
            "alert_at": o.alert_at.isoformat(),
        } for o in rows]

    def snapshot(self) -> dict:
        return {**self.metrics(), "table": self.table()}

    def pump_probability(self, *, cluster: str, tier: str, vol_spike: float,
                         min_n: int | None = None) -> tuple[float, int]:
        """EMPIRICAL P(sube) for the candidate's bucket, from the bot's OWN settled
        track record (NOT a guess). Beta-shrunk so small samples don't overclaim:
        p = (confirmed + a) / (n + a + b), prior mean 10% (the measured base rate),
        weak strength so it converges to the true rate as evidence grows. ALWAYS
        conditioned on the volume bucket (the key variable) — never borrows a different
        bucket's success. No data → ~10% (honest 'unknown'), so the loud 'va a subir'
        alert stays silent until a setup's record actually earns a high P. Returns
        (probability 0..1, n_samples in the bucket used)."""
        min_n = MIN_SETTLED_FOR_PROPOSAL if min_n is None else min_n
        win = self._in_window()
        src = [o for o in win if o.source == "alert" and o.settled and o.signals]
        # Dangerous del MISMO bucket (cluster + vol) = fallos extra: bajan la P sin
        # falsear la precision (no se compraron). Así el aprendizaje EVITA el perfil scam.
        vb = _vol_bucket(vol_spike)
        danger_n = sum(1 for o in win if o.source == "dangerous"
                       and o.cluster == cluster
                       and _vol_bucket((o.signals or {}).get("volume_spike")) == vb)
        PRIOR_A, PRIOR_B = 0.5, 4.5      # Beta(0.5,4.5): mean 0.10, weak (strength 5)

        def shrunk(rows: list) -> tuple[float, int]:
            n = len(rows)
            conf = sum(1 for o in rows if o.label == "confirmed_pump")
            # +danger_n al denominador = trata cada tell de scam del bucket como un
            # cuasi-fallo → un setup con historial peligroso entra con P más baja.
            return round((conf + PRIOR_A) / (n + danger_n + PRIOR_A + PRIOR_B), 3), n

        # Finest → coarsest, but ALWAYS conditioned on the volume bucket.
        levels = [
            [o for o in src if o.cluster == cluster
             and o.signals.get("tier") == tier
             and _vol_bucket(o.signals.get("volume_spike")) == vb],
            [o for o in src if o.cluster == cluster
             and _vol_bucket(o.signals.get("volume_spike")) == vb],
            [o for o in src if _vol_bucket(o.signals.get("volume_spike")) == vb],
        ]
        for rows in levels:
            if len(rows) >= min_n:
                return shrunk(rows)
        for rows in levels:          # not enough anywhere → finest non-empty, shrunk
            if rows:
                return shrunk(rows)
        return shrunk([])            # no vol-bucket data at all → prior only (~10%)

    @staticmethod
    def _book_of(o: "Outcome") -> str:
        """Book de un outcome desde el tier del alert (ya persistido en signals, sin
        cambio de esquema). tier 'ahora' = gainers/momentum (lead de minutos); el
        resto (inminente/onchain/acumulando) = prepump/acumulación (lead de horas)."""
        return "gainers" if (o.signals or {}).get("tier") == "ahora" else "prepump"

    def optimize_timeout(self) -> dict:
        """Sugerencia de timeout SEPARADA por book, desde el lead time de los pumps
        confirmados de CADA book. Antes mezclaba prepump (~21h) y gainers (minutos)
        en un solo promedio → el lead de prepump inflaba el timeout base/gainers (el
        bug de mezcla). Ahora cada book se afina con su propia población y banda:
        gainers [5,30] min, prepump [30,1440] min (hasta 24h)."""
        import statistics
        confirmed = [o for o in self.outcomes if o.label == "confirmed_pump"]
        out: dict = {}
        for book, lo, hi in (("gainers", 5, 30), ("prepump", 30, 1440)):
            rows = [o for o in confirmed if self._book_of(o) == book]
            if len(rows) < 10:
                out[book] = {"timeout": None, "reason": "insufficient data",
                             "n_samples": len(rows)}
                continue
            leads = [o.lead_secs() / 60 for o in rows]
            avg = statistics.mean(leads)
            std = statistics.stdev(leads) if len(leads) > 1 else 0.0
            out[book] = {
                "timeout": max(lo, min(hi, round(avg + 2 * std, 1))),
                "avg_lead": round(avg, 1), "std_lead": round(std, 1),
                "n_samples": len(rows),
            }
        return out
