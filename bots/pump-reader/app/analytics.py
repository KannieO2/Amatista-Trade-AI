"""Quantitative intelligence layer (Phase D — analytics).

PURE MEASUREMENT. Nothing here ever changes a trading decision: it consumes the
trades the existing engine already produces and derives metrics on top. The one
behaviour-adjacent piece — Confidence Position Sizing — is SIMULATION-ONLY by
default: it computes a *theoretical* size and stores it next to the real one,
and never feeds back into live sizing.

Design:
  - TradeRecord is the single permanent fact per closed trade (Module 1).
  - AnalyticsEngine holds the in-memory list (source of truth) + derives
    expectancy / profit-factor / ranking / drawdown / edge / confidence /
    regime breakdowns / reports on demand. Persistence is delegated to a
    caller-supplied hook (main.py routes it through store's async write queue).
  - Fail-safe: every public method guards itself; an analytics error never
    propagates into a trading loop.

Backward compatible: adds tables/endpoints, touches no existing strategy code.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import mean, pstdev

logger = logging.getLogger("pump-reader.analytics")

# How many closed trades a setup/regime needs before its derived stats are
# treated as meaningful (otherwise confidence stays neutral, ranking = "n/a").
MIN_SAMPLE = int(os.getenv("PUMP_ANALYTICS_MIN_SAMPLE", "20"))
HARD_STOP_REF = float(os.getenv("PUMP_STOP_LOSS_PCT", "8"))  # for MAE-control scoring


def _safe_dt(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v)
        except Exception:
            return None
    return None


@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    exchange: str
    setup_type: str = "momentum"      # accumulation | velocity | momentum | hybrid
    signal_timestamp: str | None = None
    entry_timestamp: str | None = None
    exit_timestamp: str | None = None
    entry_price: float = 0.0
    exit_price: float = 0.0
    position_size: float = 0.0        # USD notional actually deployed
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    mfe_pct: float = 0.0              # max favourable excursion
    mae_pct: float = 0.0             # max adverse excursion (negative)
    holding_seconds: float = 0.0
    lead_time_seconds: float = 0.0    # signal -> entry
    entry_slippage_pct: float = 0.0
    exit_slippage_pct: float = 0.0
    exit_reason: str = ""
    confidence_score: float = 0.0     # 0-100 (assigned at entry from history)
    risk_used: float = 0.0           # actual risk fraction used (live)
    market_regime: str = "unknown"
    trade_quality_score: float = 0.0  # 0-100 (Module 10)
    # --- confidence-sizing SIMULATION (never affects live size) ---
    sizing_mode: str = "simulation"
    sizing_multiplier: float = 1.0
    theoretical_size: float = 0.0
    theoretical_pnl_usd: float = 0.0
    user_id: str = ""

    def to_row(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, r: dict) -> "TradeRecord":
        fields = cls.__dataclass_fields__
        return cls(**{k: r.get(k) for k in fields if k in r and r.get(k) is not None})


# ---------------------------------------------------------------------------
#  Pure metric helpers (also reused by the validation suite)
# ---------------------------------------------------------------------------

def expectancy_of(trades: list[TradeRecord]) -> dict:
    """Win rate / avg win / avg loss / R:R / expectancy (per-trade USD)."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": None, "loss_rate": None, "scratch_rate": None,
                "avg_win": None, "avg_loss": None, "rr": None, "expectancy": None}
    wins = [t.pnl_usd for t in trades if t.pnl_usd > 0]
    losses = [t.pnl_usd for t in trades if t.pnl_usd < 0]   # ESTRICTO < 0
    # FORENSIC FIX: un trade scratch (pnl == 0: ghost-reaper / no_progress que cierra
    # plano) NO es una pérdida. Antes el `<= 0` lo metía en `losses` → win_rate caía,
    # loss_rate subía, y los ceros achicaban |avg_loss| → R:R inflado → breakeven_wr
    # subestimado → edge_ok podía mentir. Ahora scratch va a su propia categoría.
    scratch = n - len(wins) - len(losses)
    win_rate = len(wins) / n
    loss_rate = len(losses) / n
    avg_win = mean(wins) if wins else 0.0
    avg_loss = abs(mean(losses)) if losses else 0.0
    # Expectancy per-trade = media de TODOS los pnl (el scratch aporta 0). Idéntico
    # a win_rate*avg_win − loss_rate*avg_loss cuando no hay ceros mal clasificados.
    expectancy = sum(t.pnl_usd for t in trades) / n
    rr = (avg_win / avg_loss) if avg_loss > 0 else None
    return {
        "n": n,
        "win_rate": round(win_rate, 4),
        "loss_rate": round(loss_rate, 4),
        "scratch_rate": round(scratch / n, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "rr": round(rr, 3) if rr is not None else None,
        "expectancy": round(expectancy, 4),
    }


def profit_factor_of(trades: list[TradeRecord]) -> dict:
    """Gross profit / gross loss / profit factor."""
    gross_profit = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    gross_loss = abs(sum(t.pnl_usd for t in trades if t.pnl_usd <= 0))
    if gross_loss == 0:
        pf = None if gross_profit == 0 else math.inf
    else:
        pf = gross_profit / gross_loss
    return {
        "n": len(trades),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": (round(pf, 3) if pf not in (None, math.inf) else
                          ("inf" if pf == math.inf else None)),
    }


def drawdown_of(trades: list[TradeRecord]) -> dict:
    """Equity-curve drawdown from cumulative USD PnL (chronological)."""
    ts = sorted(trades, key=lambda t: t.exit_timestamp or "")
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    cur_dd = 0.0
    for t in ts:
        equity += t.pnl_usd
        if equity > peak:
            peak = equity
        cur_dd = peak - equity
        if cur_dd > max_dd:
            max_dd = cur_dd
    return {
        "current_drawdown": round(cur_dd, 4),
        "max_drawdown": round(max_dd, 4),
        "peak_equity": round(peak, 4),
        "net_equity": round(equity, 4),
    }


def quality_score(rec: TradeRecord, entry_grade: str) -> float:
    """0-100 trade quality (Module 10). Blends entry/exit quality, MFE capture,
    MAE control, slippage, holding efficiency. Pure, bounded, never throws."""
    try:
        entry_q = {"early_entry": 100, "perfect_entry": 85, "late_entry": 50}.get(entry_grade, 60)
        exit_q = {"trailing": 95, "break_even": 80, "timeout": 60,
                  "hard_stop": 25, "dump": 30}.get(rec.exit_reason, 60)
        # MFE capture: how much of the favourable move was banked.
        mfe = rec.mfe_pct if rec.mfe_pct > 0 else 0.0
        capture = max(0.0, min(1.0, (rec.pnl_pct / mfe))) if mfe > 0 else (1.0 if rec.pnl_pct > 0 else 0.0)
        mfe_capture = capture * 100
        # MAE control: how far the trade stayed from a full stop-out.
        mae = abs(rec.mae_pct)
        mae_control = max(0.0, 1.0 - min(1.0, mae / HARD_STOP_REF)) * 100
        # Slippage: penalise execution drag.
        slip = abs(rec.entry_slippage_pct) + abs(rec.exit_slippage_pct)
        slip_score = max(0.0, 100.0 - slip * 50)
        # Holding efficiency: pnl per hour, soft-normalised.
        hrs = max(rec.holding_seconds / 3600, 1 / 60)
        eff = rec.pnl_pct / hrs
        hold_eff = max(0.0, min(100.0, 50 + eff * 5))
        score = (0.20 * entry_q + 0.20 * exit_q + 0.20 * mfe_capture +
                 0.20 * mae_control + 0.10 * slip_score + 0.10 * hold_eff)
        return round(max(0.0, min(100.0, score)), 1)
    except Exception:
        return 0.0


def grade_setup(pf, expectancy, win_rate, n) -> str:
    """Letter grade A+..F from a setup's stats. 'n/a' below the sample floor."""
    if n < MIN_SAMPLE:
        return "n/a"
    if expectancy is None:
        return "F"
    pf_v = math.inf if pf == "inf" else (pf or 0)
    # Score from profit factor + expectancy sign + win rate.
    s = 0
    s += 3 if pf_v >= 2 else 2 if pf_v >= 1.5 else 1 if pf_v >= 1.1 else 0
    s += 2 if expectancy > 0 else 0
    s += 1 if (win_rate or 0) >= 0.45 else 0
    return {6: "A+", 5: "A", 4: "B", 3: "C", 2: "D", 1: "F", 0: "F"}.get(s, "F")


def classify_regime(closes: list[float]) -> tuple[str, str]:
    """(trend, volatility) from a price series (e.g. BTC daily closes). Pure.
    trend: bull/bear/sideways via first->last %; vol: high/low via stdev of returns."""
    if not closes or len(closes) < 3:
        return "unknown", "unknown"
    chg = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0.0
    trend = "bull" if chg > 5 else "bear" if chg < -5 else "sideways"
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]]
    vol = pstdev(rets) * 100 if len(rets) > 1 else 0.0
    vol_label = "high_vol" if vol > 4 else "low_vol"
    return trend, vol_label


# ---------------------------------------------------------------------------
#  Engine
# ---------------------------------------------------------------------------

class AnalyticsEngine:
    def __init__(self) -> None:
        self.trades: list[TradeRecord] = []
        self._pending: dict[str, dict] = {}     # position key -> entry-time context
        self._live_low: dict[str, float] = {}    # position key -> min adverse price
        self.regime_trend = "unknown"
        self.regime_vol = "unknown"
        self.persist = None                       # set by main.py: fn(row: dict) -> None

    # --- regime ---
    def set_regime(self, trend: str, vol: str) -> None:
        self.regime_trend = trend
        self.regime_vol = vol

    @property
    def regime(self) -> str:
        return f"{self.regime_trend}/{self.regime_vol}"

    # --- entry-time (confidence + sizing simulation) ---
    def confidence_for(self, setup_type: str, exchange: str) -> float:
        """0-100 confidence from this setup's historical edge. Neutral (60) until
        enough samples exist. Inputs: win rate, profit factor, expectancy sign,
        regime performance. Read-only; used for sizing SIMULATION + stored on trade."""
        try:
            subset = [t for t in self.trades if t.setup_type == setup_type]
            if len(subset) < MIN_SAMPLE:
                return 60.0
            ex = expectancy_of(subset)
            pf = profit_factor_of(subset)["profit_factor"]
            pf_v = 3.0 if pf == "inf" else (pf or 0.0)
            wr = ex["win_rate"] or 0.0
            exp_pos = 1.0 if (ex["expectancy"] or 0) > 0 else 0.0
            # regime alignment: how this setup does in the CURRENT regime.
            reg = [t for t in subset if t.market_regime == self.regime]
            reg_bonus = 0.0
            if len(reg) >= 5:
                reg_bonus = 10.0 if (expectancy_of(reg)["expectancy"] or 0) > 0 else -10.0
            score = 40 * wr + 20 * min(pf_v / 2, 1.0) + 20 * exp_pos + reg_bonus + 10
            return round(max(0.0, min(100.0, score)), 1)
        except Exception:
            return 60.0

    @staticmethod
    def sizing_multiplier(confidence: float) -> float:
        if confidence >= 90:
            return 1.5
        if confidence >= 70:
            return 1.0
        return 0.5

    def note_open(self, key: str, ctx: dict) -> None:
        """Record entry-time context for a position so the closed trade can be
        fully reconstructed. ctx holds setup_type/entry_price/size/confidence/etc."""
        self._pending[key] = ctx
        self._live_low[key] = ctx.get("entry_price", 0.0) or 0.0

    def observe_price(self, key: str, price: float) -> None:
        """Track the worst adverse price during an open trade (for MAE)."""
        if price <= 0:
            return
        lo = self._live_low.get(key)
        if lo is None or price < lo:
            self._live_low[key] = price

    # --- close-time ingest ---
    def close_trade(self, key: str, *, pos, event, entry_grade: str) -> TradeRecord | None:
        """Build + ingest the permanent TradeRecord for a closed position. Returns
        the record (or None on failure). Pulls entry context from _pending."""
        try:
            ctx = self._pending.pop(key, {})
            low = self._live_low.pop(key, pos.entry_price)
            entry_price = pos.entry_price
            exit_price = event.price
            size = ctx.get("position_size", 0.0) or 0.0
            mfe = (pos.peak_price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
            mae = (low - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
            entry_at = pos.entry_at
            exit_at = _safe_dt(event.at) or datetime.now(UTC)
            holding = (exit_at - entry_at).total_seconds() if entry_at else 0.0
            lead = ((entry_at - pos.signal_at).total_seconds()
                    if (pos.signal_at and entry_at) else 0.0)
            cost = entry_price * pos.initial_qty
            pnl_pct = (pos.realized_pnl / cost * 100) if cost > 0 else 0.0
            rec = TradeRecord(
                trade_id=ctx.get("trade_id") or f"{key}:{int(exit_at.timestamp())}",
                symbol=pos.symbol, exchange=pos.exchange,
                setup_type=ctx.get("setup_type", "momentum"),
                signal_timestamp=pos.signal_at.isoformat() if pos.signal_at else None,
                entry_timestamp=entry_at.isoformat() if entry_at else None,
                exit_timestamp=exit_at.isoformat(),
                entry_price=round(entry_price, 10), exit_price=round(exit_price, 10),
                position_size=round(size, 2),
                pnl_pct=round(pnl_pct, 4), pnl_usd=round(pos.realized_pnl, 4),
                mfe_pct=round(mfe, 4), mae_pct=round(mae, 4),
                holding_seconds=round(holding, 1), lead_time_seconds=round(lead, 1),
                entry_slippage_pct=round(ctx.get("entry_slippage_pct", 0.0), 5),
                exit_slippage_pct=0.0,
                exit_reason=event.reason,
                confidence_score=round(ctx.get("confidence_score", 60.0), 1),
                risk_used=ctx.get("risk_used", 0.0),
                market_regime=ctx.get("market_regime") or self.regime,
                sizing_mode=ctx.get("sizing_mode", "simulation"),
                sizing_multiplier=ctx.get("sizing_multiplier", 1.0),
                theoretical_size=round(ctx.get("theoretical_size", size), 2),
                user_id=ctx.get("user_id", ""),
            )
            # Theoretical PnL the confidence-sized position WOULD have made
            # (same %, scaled notional) — SIMULATION only.
            rec.theoretical_pnl_usd = round(rec.pnl_usd * (rec.theoretical_size / size), 4) if size > 0 else rec.pnl_usd
            rec.trade_quality_score = quality_score(rec, entry_grade)
            self.ingest(rec)
            return rec
        except Exception:
            logger.exception("close_trade analytics failed for %s", key)
            return None

    def ingest(self, rec: TradeRecord) -> None:
        self.trades.append(rec)
        if len(self.trades) > 5000:
            self.trades = self.trades[-5000:]
        if self.persist:
            try:
                self.persist(rec.to_row())
            except Exception:
                logger.debug("analytics persist hook failed", exc_info=True)

    # --- startup hydrate ---
    def load_rows(self, rows: list[dict]) -> int:
        n = 0
        for r in rows or []:
            try:
                self.trades.append(TradeRecord.from_row(r))
                n += 1
            except Exception:
                continue
        self.trades.sort(key=lambda t: t.exit_timestamp or "")
        if len(self.trades) > 5000:
            self.trades = self.trades[-5000:]
        return n

    # --- segmentation helpers ---
    def _by(self, attr: str) -> dict[str, list[TradeRecord]]:
        out: dict[str, list[TradeRecord]] = {}
        for t in self.trades:
            out.setdefault(getattr(t, attr, "?") or "?", []).append(t)
        return out

    # --- Module 2/3 segmented ---
    def expectancy(self) -> dict:
        return {
            "global": expectancy_of(self.trades),
            "by_setup": {k: expectancy_of(v) for k, v in self._by("setup_type").items()},
            "by_exchange": {k: expectancy_of(v) for k, v in self._by("exchange").items()},
            "by_regime": {k: expectancy_of(v) for k, v in self._by("market_regime").items()},
        }

    def profit_factor(self) -> dict:
        def rolling(n):
            return profit_factor_of(self.trades[-n:]) if len(self.trades) >= 1 else profit_factor_of([])
        return {
            "overall": profit_factor_of(self.trades),
            "by_setup": {k: profit_factor_of(v) for k, v in self._by("setup_type").items()},
            "by_exchange": {k: profit_factor_of(v) for k, v in self._by("exchange").items()},
            "by_regime": {k: profit_factor_of(v) for k, v in self._by("market_regime").items()},
            "rolling": {"last_30": rolling(30), "last_50": rolling(50), "last_100": rolling(100)},
        }

    # --- Module 4 ---
    def setup_ranking(self) -> list[dict]:
        rows = []
        for setup, ts in self._by("setup_type").items():
            ex = expectancy_of(ts)
            pf = profit_factor_of(ts)["profit_factor"]
            dd = drawdown_of(ts)["max_drawdown"]
            q = mean([t.trade_quality_score for t in ts]) if ts else 0.0
            rows.append({
                "setup": setup, "n": len(ts),
                "profit_factor": pf, "expectancy": ex["expectancy"],
                "win_rate": ex["win_rate"], "max_drawdown": dd,
                "avg_quality": round(q, 1),
                "grade": grade_setup(pf, ex["expectancy"], ex["win_rate"], len(ts)),
            })
        order = {"A+": 0, "A": 1, "B": 2, "C": 3, "D": 4, "F": 5, "n/a": 6}
        return sorted(rows, key=lambda r: (order.get(r["grade"], 9), -(r["n"])))

    # --- Module 9 ---
    def drawdown(self) -> dict:
        now = datetime.now(UTC)
        def window(days):
            cut = now - timedelta(days=days)
            return [t for t in self.trades
                    if (_safe_dt(t.exit_timestamp) or now) >= cut]
        return {
            "overall": drawdown_of(self.trades),
            "rolling_30d": drawdown_of(window(30)),
            "rolling_90d": drawdown_of(window(90)),
        }

    # --- Module 6 ---
    def edge_status(self) -> dict:
        """Compare the recent window vs the prior window; emit an edge label.
        ALERTS ONLY — never acts."""
        n = len(self.trades)
        if n < 2 * max(10, MIN_SAMPLE // 2):
            return {"status": "EDGE_STABLE", "reason": "insufficient history",
                    "samples": n}
        w = max(10, MIN_SAMPLE // 2)
        recent = self.trades[-w:]
        prior = self.trades[-2 * w:-w]
        r_pf = profit_factor_of(recent)["profit_factor"]
        p_pf = profit_factor_of(prior)["profit_factor"]
        r_exp = expectancy_of(recent)["expectancy"] or 0
        p_exp = expectancy_of(prior)["expectancy"] or 0
        rv = 3.0 if r_pf == "inf" else (r_pf or 0)
        pv = 3.0 if p_pf == "inf" else (p_pf or 0)
        if rv < 1.0 and r_exp <= 0:
            status = "EDGE_BROKEN"
        elif rv < pv * 0.8 or r_exp < p_exp * 0.8:
            status = "EDGE_WEAKENING"
        elif pv < 1.0 and rv >= 1.0:
            status = "EDGE_RECOVERING"
        elif rv > pv * 1.2 and r_exp > p_exp:
            status = "EDGE_IMPROVING"
        else:
            status = "EDGE_STABLE"
        return {"status": status, "recent_pf": r_pf, "prior_pf": p_pf,
                "recent_expectancy": round(r_exp, 4), "prior_expectancy": round(p_exp, 4),
                "window": w}

    # --- distributions for the dashboard ---
    def confidence_distribution(self) -> dict:
        buckets = {"<70": 0, "70-89": 0, "90+": 0}
        for t in self.trades:
            c = t.confidence_score
            buckets["90+" if c >= 90 else "70-89" if c >= 70 else "<70"] += 1
        return buckets

    def quality_distribution(self) -> dict:
        buckets = {"0-40": 0, "40-60": 0, "60-80": 0, "80-100": 0}
        for t in self.trades:
            q = t.trade_quality_score
            key = "80-100" if q >= 80 else "60-80" if q >= 60 else "40-60" if q >= 40 else "0-40"
            buckets[key] += 1
        return buckets

    def regime_distribution(self) -> dict:
        return {k: len(v) for k, v in self._by("market_regime").items()}

    # --- Module 8 sizing simulation summary ---
    def sizing_simulation(self) -> dict:
        actual = sum(t.pnl_usd for t in self.trades)
        theo = sum(t.theoretical_pnl_usd for t in self.trades)
        return {
            "mode_default": "simulation",
            "n": len(self.trades),
            "actual_pnl_usd": round(actual, 2),
            "theoretical_pnl_usd": round(theo, 2),
            "delta_usd": round(theo - actual, 2),
            "note": "theoretical = confidence-scaled notional, never applied live",
        }

    # --- Module 11 ---
    def dashboard(self) -> dict:
        ex = expectancy_of(self.trades)
        pf = profit_factor_of(self.trades)
        dd = drawdown_of(self.trades)
        ranking = self.setup_ranking()
        # Break-even win rate the current payoff demands: BE = avgLoss/(avgWin+avgLoss)
        # = 1/(1+R:R). Below it the edge is negative no matter how it "feels".
        # No live R:R yet → fall back to the engine-derived reference (R:R 2.51 → ~28%).
        rr = ex["rr"]
        if rr and rr > 0:
            be_wr = round(1.0 / (1.0 + rr), 4)
        else:
            be_wr = round(float(os.getenv("PUMP_BREAKEVEN_WR_REF", "0.285")), 4)
        wr = ex["win_rate"]
        edge_ok = (wr is not None) and (wr >= be_wr)
        return {
            "total_trades": len(self.trades),
            "win_rate": ex["win_rate"], "loss_rate": ex["loss_rate"],
            "breakeven_wr": be_wr, "rr": rr, "edge_ok": edge_ok,
            "win_margin": (round(wr - be_wr, 4) if wr is not None else None),
            "profit_factor": pf["profit_factor"],
            "expectancy": ex["expectancy"],
            "avg_win": ex["avg_win"], "avg_loss": ex["avg_loss"],
            "current_drawdown": dd["current_drawdown"], "max_drawdown": dd["max_drawdown"],
            "net_equity": dd["net_equity"],
            "confidence_distribution": self.confidence_distribution(),
            "regime_distribution": self.regime_distribution(),
            "current_regime": self.regime,
            "top_setups": ranking[:3],
            "bottom_setups": ranking[-3:][::-1] if len(ranking) > 3 else [],
            "quality_distribution": self.quality_distribution(),
            "edge_status": self.edge_status(),
            "sizing_simulation": self.sizing_simulation(),
        }

    # --- Module 12 ---
    def report(self, period: str = "daily") -> dict:
        days = {"daily": 1, "weekly": 7, "monthly": 30}.get(period, 1)
        now = datetime.now(UTC)
        cut = now - timedelta(days=days)
        ts = [t for t in self.trades if (_safe_dt(t.exit_timestamp) or now) >= cut]
        ex = expectancy_of(ts)
        pf = profit_factor_of(ts)
        rank = []
        for setup, sub in {k: [t for t in ts if t.setup_type == k]
                           for k in {t.setup_type for t in ts}}.items():
            e = expectancy_of(sub)
            rank.append({"setup": setup, "n": len(sub), "expectancy": e["expectancy"],
                         "profit_factor": profit_factor_of(sub)["profit_factor"]})
        rank.sort(key=lambda r: (r["expectancy"] or -1e9), reverse=True)
        regimes = {}
        for t in ts:
            regimes[t.market_regime] = regimes.get(t.market_regime, 0) + 1
        return {
            "period": period, "from": cut.isoformat(), "to": now.isoformat(),
            "trades": len(ts),
            "pnl_usd": round(sum(t.pnl_usd for t in ts), 2),
            "profit_factor": pf["profit_factor"], "expectancy": ex["expectancy"],
            "win_rate": ex["win_rate"],
            "drawdown": drawdown_of(ts),
            "best_setups": rank[:3], "worst_setups": rank[-3:][::-1] if len(rank) > 3 else [],
            "market_regime_analysis": regimes,
            "confidence_analysis": {
                "avg": round(mean([t.confidence_score for t in ts]), 1) if ts else None,
                "distribution": {b: sum(1 for t in ts
                                        if (b == "90+" and t.confidence_score >= 90) or
                                           (b == "70-89" and 70 <= t.confidence_score < 90) or
                                           (b == "<70" and t.confidence_score < 70))
                                 for b in ("<70", "70-89", "90+")},
            },
            "trade_quality_analysis": {
                "avg": round(mean([t.trade_quality_score for t in ts]), 1) if ts else None,
            },
        }

    def recent(self, limit: int = 50) -> list[dict]:
        return [t.to_row() for t in self.trades[-limit:][::-1]]


_engine = AnalyticsEngine()


def get_engine() -> AnalyticsEngine:
    return _engine
