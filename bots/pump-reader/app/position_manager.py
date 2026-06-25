"""Dynamic trailing-stop exit + dump detector for paper pump positions.

Addresses the #1 gap: the bot used to only buy. This manages each open position
so it is never the exit liquidity. ONE strategy on the FULL position (no 60/40):

  Hard stop:    a loss past -HARD_STOP% sells everything.
  Dump:         an abrupt one-tick drop panic-sells at market.
  Break-even:   once gain crosses +BREAKEVEN%, the stop locks at entry+margin.
  Dynamic stop: while in profit a trailing stop ratchets up to
                peak*(1 - DYNAMIC_STOP%) and only moves up; a fall back to it
                banks the WHOLE run at once.
  Time-stop:    a flat move whose 1m volume has FADED is freed (volume-aware).

Exit params are cluster-aware: long_pump runs tight & fast, classic grinds loose
& patient (see exit_profile / CLUSTER_TUNE). Entry quality is graded
(early/perfect/late) vs the peak to feed the learning loop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

# NOTE: hard_stop / dump / dynamic_stop / timeout / max_hold / breakeven NO viven
# como constantes aquí: los lee exit_profile() desde os.getenv EN VIVO para que el
# auto-optimizador de 24h los retunee sin reinicio (ver exit_profile + CLUSTER_TUNE).
# Solo quedan abajo los parámetros que step() usa directo (trail + time-stop).
# Profit ratchet (user-requested): trail from a SMALL gain, not +5%. Once the gain
# clears TRAIL_ARM_PCT the stop locks (100 - GIVEBACK)% of the PEAK gain and only
# moves up. Give-back is floored at TRAIL_MIN_PCT (the spread floor) so a normal
# wiggle never shakes a winner out at the first downtick — without the floor a
# 10%-of-a-tiny-gain trail fires inside the spread and caps every winner at ~0.
# Tightened (user: pumps subían ~4% y devolvían demasiado antes de cerrar). 8% give-back
# (was 10) banks more of a big run; 0.6% floor (was 0.8) banks a SMALL pop closer to its
# peak instead of riding it back down — still above the typical spread of a >$40k book so
# a normal wiggle doesn't shake a winner out.
TRAIL_GIVEBACK_PCT = float(os.getenv("PUMP_TRAIL_GIVEBACK_PCT", "8"))   # give back N% of the gain
TRAIL_ARM_PCT = float(os.getenv("PUMP_TRAIL_ARM_PCT", "0.8"))          # arm once +this% green
TRAIL_MIN_PCT = float(os.getenv("PUMP_TRAIL_MIN_PCT", "0.6"))          # never trail tighter (spread floor)

# Dynamic risk management (live-used by step / _time_stop_fires; not auto-tuned).
TIMEOUT_BAND_PCT = float(os.getenv("PUMP_TIMEOUT_BAND_PCT", "3"))  # lateral = |gain| <= band
BREAKEVEN_MARGIN_PCT = float(os.getenv("PUMP_BREAKEVEN_MARGIN_PCT", "0.5"))  # SL above entry
# Volume-aware (dynamic) time-stop: a sideways pump with LIVE volume keeps its
# capital; only a flat move whose volume has FADED is cut. "Alive" = the latest
# 1m volume is still >= VOLUME_ALIVE_FRAC of the peak 1m volume seen in the trade.
VOLUME_ALIVE_FRAC = float(os.getenv("PUMP_VOLUME_ALIVE_FRAC", "0.5"))
# When no volume signal is available, fall back to a (longer) plain time-stop.
TIMEOUT_NO_VOL_MINUTES = float(os.getenv("PUMP_TIMEOUT_NO_VOL_MINUTES", "20"))
# (max_hold también lo lee exit_profile() en vivo — no constante aquí.)
# Fast dead-trade cut — the #1 MEASURED bleed. Real paper data (49 trades, current
# config): 86% exited by slow `timeout` at avg -1.25% after sitting flat for up to
# an hour. A position that NEVER reached +FAST_CUT_MIN_PROGRESS_PCT within
# FAST_CUT_MINUTES and is now non-green is dead weight — free it at the SMALL loss
# instead of letting it drift to the full timeout/hard-stop. Turns a -1.25% slow
# bleed into a ~-0.4% quick cut and frees capital for a fresh shot. 0 disables.
FAST_CUT_MINUTES = float(os.getenv("PUMP_FAST_CUT_MINUTES", "3"))
FAST_CUT_MIN_PROGRESS_PCT = float(os.getenv("PUMP_FAST_CUT_MIN_PROGRESS_PCT", "0.6"))

# --- Cluster-aware exit profiles --------------------------------------------
# long_pump and classic are DIFFERENT setups → different trade management:
#   long_pump (buyer impulse / parabolic): run the spike, TIGHT trail, FAST cut,
#             sensitive dump detector — the move is violent and round-trips fast.
#   classic   (short-squeeze grind): LOOSE trail so the grind isn't shaken out,
#             PATIENT time-stop, tighter hard stop.
#   accumulation / n.a.: unknown breakout character → plain env base, no tuning.
# CLUSTER_TUNE = multipliers applied ON TOP of the live env base, so the 24h
# auto-optimizer (which mutates os.environ) still tunes the baseline while each
# cluster keeps its own character relative to it.
CLUSTER_TUNE = {
    "long_pump": {"dynamic_stop_pct": 0.8, "hard_stop_pct": 1.3, "dump_tick_pct": 1.1,
                  "timeout_min": 0.4, "max_hold_min": 0.5},
    "classic":   {"dynamic_stop_pct": 1.5, "hard_stop_pct": 0.9, "dump_tick_pct": 1.4,
                  "timeout_min": 1.2, "max_hold_min": 1.2},
}


# Prepump (accumulation) HOLD HORIZON. The confirmed-pump lead time is ~21h
# (measured): a minute-scale time-stop guarantees exiting BEFORE the move, so the
# prepump book holds for HOURS. Only the flat time-stop horizon is extended — the
# hard-stop, dump detector and trailing stop still cap downside every tick, so a
# long hold never means an unprotected one. Read live (env-tunable, no restart).
def _prepump_timeout_min() -> float:
    return float(os.getenv("PUMP_PREPUMP_TIMEOUT_MINUTES", "360"))    # 6h flat+faded-vol


def _prepump_max_hold_min() -> float:
    return float(os.getenv("PUMP_PREPUMP_MAX_HOLD_MINUTES", "1440"))  # 24h backstop (covers 21h lead)


def _prepump_faded_cut_min() -> float:
    """Responsive volume-collapse cut (user: 'si baja el volumen de compra, salir').
    A flat accumulation whose 1m volume has STAYED faded (< VOLUME_ALIVE_FRAC of peak)
    this many minutes is cut NOW instead of waiting the 6h flat timeout — the fuel for
    the ignition is gone. 0 disables (fall back to the old 6h gate)."""
    return float(os.getenv("PUMP_PREPUMP_FADED_CUT_MINUTES", "45"))


def exit_profile(cluster: str, book: str = "prepump") -> dict:
    """Per-trade exit params. Reads env in real time (so the 24h auto-optimizer can
    retune by mutating os.environ without a restart), then applies the cluster
    multipliers so long_pump and classic are managed differently. The prepump book
    overrides the time-stop horizon to HOURS (the ~21h accumulation→pump lead)."""
    base = {
        "dynamic_stop_pct": float(os.getenv("PUMP_DYNAMIC_STOP_PCT", "5.0")),
        "hard_stop_pct": float(os.getenv("PUMP_STOP_LOSS_PCT", "8")),
        "dump_tick_pct": float(os.getenv("PUMP_DUMP_TICK_PCT", "10")),
        "timeout_min": float(os.getenv("PUMP_TIMEOUT_MINUTES", "8")),
        "max_hold_min": float(os.getenv("PUMP_MAX_HOLD_MINUTES", "45")),
        # P3: break-even read LIVE so the 24h optimizer can retune it (was a frozen
        # import constant). Cluster-neutral on purpose — not in CLUSTER_TUNE.
        "breakeven_pct": float(os.getenv("PUMP_BREAKEVEN_PCT", "4")),
    }
    tune = CLUSTER_TUNE.get(cluster)
    if tune:
        for k, m in tune.items():
            base[k] = round(base[k] * m, 2)
    # Prepump = detect-before thesis → patience. Override AFTER cluster tune so the
    # horizon is the accumulation one regardless of long_pump/classic character.
    if book == "prepump":
        base["timeout_min"] = _prepump_timeout_min()
        base["max_hold_min"] = _prepump_max_hold_min()
        # Hard-stop for the accumulation book. RR fix (medido): con WR 28.6% y hard-stop
        # 8% las perdedoras corrían al -8% completo (avg_loss 3.69 ≈ avg_win 3.96 → RR
        # 1.07, expectancy negativa). La tesis "tolera dip profundo" NO pagaba: los dips
        # se volvían stop completo, no recuperación. Tightened a 5% (1.7× la banda lateral
        # ±3% → deja margen al shakeout normal pero corta el desastre). Esto baja avg_loss
        # ~37% sin tocar las ganadoras. Tunable (0 = volver al hard-stop por-cluster).
        hs = float(os.getenv("PUMP_PREPUMP_HARD_STOP_PCT", "5"))
        if hs > 0:
            base["hard_stop_pct"] = hs
    return base


@dataclass
class ManagedPosition:
    symbol: str
    exchange: str
    entry_price: float
    qty: float
    initial_qty: float
    entry_at: datetime
    peak_price: float
    peak_at: datetime
    phase: int = 1
    realized_pnl: float = 0.0
    last_price: float = 0.0
    closed: bool = False
    pump_score: int = 0
    classification: str = "n/a"
    cluster: str = "n/a"          # long_pump | classic | accumulation → exit profile
    book: str = "prepump"         # prepump (FSM accumulation) | gainers (velocity/momentum) — P&L bucket
    entry_phase: str = "ruptura"  # lead (on-chain, antes del pump) | ruptura (FSM confirmó el arranque) | momentum (gainers) — etiqueta HONESTA del timing
    confidence: float = 100.0     # entry confidence (0-100), telemetry only
    be_armed: bool = False        # break-even stop activated (gain crossed BREAKEVEN_PCT)
    be_stop: float = 0.0          # break-even stop price (entry + margin)
    dynamic_stop: float = 0.0     # trailing stop off the peak (full position, ratchets up only)
    peak_volume: float = 0.0      # max 1m volume seen during the trade (fuel gauge)
    last_volume: float = 0.0      # latest 1m volume (vs peak → alive / faded)
    stale_since: datetime | None = None  # first tick with no valid price (ghost reaper)
    vol_fade_since: datetime | None = None  # first tick where 1m buy volume fell below alive-frac (responsive vol-collapse cut)
    # --- telemetry only (never affect exit decisions) ---
    signal_at: datetime | None = None   # when the signal that triggered entry fired
    be_at: datetime | None = None       # when break-even armed
    trail_at: datetime | None = None    # when the trailing stop first armed
    exit_source: str = ""               # price source at the tick that triggered the exit
    exit_price_age_ms: float | None = None  # WS price age at the exit tick (ms)


@dataclass
class ExitEvent:
    symbol: str
    exchange: str
    reason: str
    sold_qty: float
    price: float
    pnl: float
    fraction: float
    closed: bool
    book: str = "prepump"   # which P&L bucket this exit belongs to (mirrors the position)
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class PositionManager:
    def __init__(self) -> None:
        self.positions: dict[str, ManagedPosition] = {}
        self.history: list[ExitEvent] = []

    def key(self, exchange: str, symbol: str) -> str:
        return f"{exchange}:{symbol}"

    def has(self, exchange: str, symbol: str) -> bool:
        pos = self.positions.get(self.key(exchange, symbol))
        return pos is not None and not pos.closed

    def open(self, *, symbol: str, exchange: str, entry_price: float, qty: float,
             pump_score: int = 0, classification: str = "n/a", cluster: str = "n/a",
             confidence: float = 100.0, book: str = "prepump", entry_phase: str = "ruptura",
             signal_at: datetime | None = None, now: datetime | None = None) -> None:
        if entry_price <= 0 or qty <= 0:
            return
        now = now or datetime.now(UTC)
        self.positions[self.key(exchange, symbol)] = ManagedPosition(
            symbol=symbol, exchange=exchange, entry_price=entry_price, qty=qty,
            initial_qty=qty, entry_at=now, peak_price=entry_price, peak_at=now,
            last_price=entry_price, pump_score=pump_score, classification=classification,
            cluster=cluster, confidence=confidence, book=book, entry_phase=entry_phase,
            signal_at=signal_at,
        )

    def step(self, key: str, price: float, volume: float | None = None,
             now: datetime | None = None) -> list[ExitEvent]:
        pos = self.positions.get(key)
        if not pos or pos.closed or price <= 0:
            return []
        now = now or datetime.now(UTC)
        prev = pos.last_price or pos.entry_price
        pos.last_price = price
        if price > pos.peak_price:
            pos.peak_price = price
            pos.peak_at = now
        if volume is not None and volume > 0:
            pos.last_volume = volume
            if volume > pos.peak_volume:
                pos.peak_volume = volume

        gain = (price - pos.entry_price) / pos.entry_price * 100
        tick_drop = (prev - price) / prev * 100 if prev > 0 else 0
        elapsed_min = (now - pos.entry_at).total_seconds() / 60

        # Cluster-aware management: long_pump rides tight/fast, classic grinds
        # patient/loose (see CLUSTER_TUNE). Prepump book extends the hold horizon.
        p = exit_profile(pos.cluster, pos.book)
        trail_arm = TRAIL_ARM_PCT
        trail_give = TRAIL_GIVEBACK_PCT
        trail_floor = TRAIL_MIN_PCT

        events: list[ExitEvent] = []
        # Hard stop first (capital protection priority).
        if gain <= -p["hard_stop_pct"]:
            events.append(self._sell(pos, price, 1.0, "hard_stop"))
            return events
        # Dump detector: abrupt one-tick collapse -> panic sell.
        if tick_drop >= p["dump_tick_pct"]:
            events.append(self._sell(pos, price, 1.0, "dump"))
            return events
        # Break-even: once gain crossed +BREAKEVEN_PCT, the stop moves to entry +
        # margin. Falling back to it locks the trade at ~breakeven (no give-back).
        if not pos.be_armed and gain >= p["breakeven_pct"]:
            pos.be_armed = True
            pos.be_stop = pos.entry_price * (1 + BREAKEVEN_MARGIN_PCT / 100)
            pos.be_at = now
        if pos.be_armed and price <= pos.be_stop:
            events.append(self._sell(pos, price, 1.0, "break_even"))
            return events
        # PROFIT RATCHET (trailing, FULL position). Trails from a SMALL gain: once
        # the PEAK gain clears TRAIL_ARM_PCT, the stop locks (100 - GIVEBACK)% of the
        # peak gain and ratchets up only. Give-back = max(GIVEBACK% of the gain,
        # TRAIL_MIN_PCT) — the floor keeps a normal wiggle (spread) from shaking a
        # winner out, while big runs only give back GIVEBACK% (≈10%). A fall back to
        # the stop banks the whole run at once.
        peak_gain = (pos.peak_price - pos.entry_price) / pos.entry_price * 100
        if peak_gain >= trail_arm:
            trail_pct = max(trail_give / 100 * peak_gain, trail_floor)
            new_stop = pos.peak_price * (1 - trail_pct / 100)
            if new_stop > pos.dynamic_stop:
                if pos.dynamic_stop == 0 and pos.trail_at is None:
                    pos.trail_at = now
                pos.dynamic_stop = new_stop
        if pos.dynamic_stop > 0 and price <= pos.dynamic_stop:
            events.append(self._sell(pos, price, 1.0, "trailing"))
            return events
        # Responsive VOLUME-COLLAPSE cut (prepump) — user: "analizar si baja el volumen
        # de compra para salir". The fuel gauge: track how long the 1m volume has STAYED
        # faded (< VOLUME_ALIVE_FRAC of the peak). On a FLAT trade (|gain| <= band; a
        # moving one is owned by TP/trail/stop) that's been faded for FADED_CUT_MINUTES,
        # the accumulation isn't igniting → free the capital NOW (not at the 6h timeout).
        # vol_fade_since resets the instant volume revives, so a brief dip never cuts.
        _fcm = _prepump_faded_cut_min()
        if (pos.book == "prepump" and _fcm > 0 and pos.peak_volume > 0
                and pos.last_volume > 0 and abs(gain) <= TIMEOUT_BAND_PCT):
            if pos.last_volume < VOLUME_ALIVE_FRAC * pos.peak_volume:
                pos.vol_fade_since = pos.vol_fade_since or now
                if (now - pos.vol_fade_since).total_seconds() / 60 >= _fcm:
                    events.append(self._sell(pos, price, 1.0, "vol_fade"))
                    return events
            else:
                pos.vol_fade_since = None  # volume revived → reset the fade clock
        # Fast dead-trade cut — GAINERS ONLY. For a momentum entry, flat-after-entry
        # = the move we chased is dead, cut cheap NOW (attacks the #1 measured bleed:
        # slow flat timeouts at -1.25%). For PREPUMP this is WRONG: an accumulation
        # token is flat BY DESIGN and the confirmed-pump lead time is ~21h, so a 3-min
        # flat cut guarantees exiting BEFORE the move (the no_progress churn). Prepump
        # falls through to the volume-aware time-stop, which keeps a token with live
        # volume alive instead of cutting it for merely sitting still.
        if (pos.book == "gainers" and FAST_CUT_MINUTES > 0 and elapsed_min >= FAST_CUT_MINUTES
                and peak_gain < FAST_CUT_MIN_PROGRESS_PCT and gain <= 0):
            events.append(self._sell(pos, price, 1.0, "no_progress"))
            return events
        # Volume-aware time-stop (backup). A flat move (|gain| <= band) is NOT cut
        # just for being slow — only when its FUEL is gone. While 1m volume stays
        # alive (>= frac of peak) a sideways pump keeps running; once it fades, free
        # the capital.
        if self._time_stop_fires(pos, gain, elapsed_min, p):
            events.append(self._sell(pos, price, 1.0, "timeout"))
            return events
        return events

    def _time_stop_fires(self, pos: ManagedPosition, gain: float, elapsed_min: float,
                         p: dict | None = None) -> bool:
        """Volume-aware time-stop. Returns True only for a flat move that should be
        cut. Logic:
          - not lateral (|gain| > band)            -> never (let TP/trail/stop run)
          - volume FADED + past TIMEOUT_MINUTES     -> cut (dead move)
          - no volume data + past NO_VOL_MINUTES    -> cut (longer fallback grace)
          - volume ALIVE                            -> hold, until MAX_HOLD backstop
        """
        p = p or exit_profile(pos.cluster, pos.book)
        if abs(gain) > TIMEOUT_BAND_PCT:
            return False
        have_vol = pos.peak_volume > 0 and pos.last_volume > 0
        if have_vol:
            faded = pos.last_volume < VOLUME_ALIVE_FRAC * pos.peak_volume
            if faded and elapsed_min >= p["timeout_min"]:
                return True            # flat + fuel gone = dead
            if elapsed_min >= p["max_hold_min"]:
                return True            # backstop: capped even if volume persists
            return False               # alive volume -> keep the sideways pump
        # No volume signal: fall back to a plain time-stop. Prepump uses its extended
        # horizon so a no-data accumulator isn't cut in 20 min (it waits the lead).
        no_vol_limit = p["timeout_min"] if pos.book == "prepump" else TIMEOUT_NO_VOL_MINUTES
        return elapsed_min >= no_vol_limit

    def _sell(self, pos: ManagedPosition, price: float, fraction: float, reason: str) -> ExitEvent:
        sell_qty = pos.qty if fraction >= 1.0 else pos.qty * fraction
        pnl = (price - pos.entry_price) * sell_qty
        pos.qty -= sell_qty
        pos.realized_pnl += pnl
        closed = pos.qty <= 1e-12
        pos.closed = pos.closed or closed
        event = ExitEvent(
            symbol=pos.symbol, exchange=pos.exchange, reason=reason,
            sold_qty=round(sell_qty, 8), price=round(price, 8), pnl=round(pnl, 4),
            fraction=round(fraction, 3), closed=closed, book=pos.book,
        )
        self.history.append(event)
        del self.history[:-100]
        return event

    def reap(self, key: str, reason: str = "stale_no_price") -> list[ExitEvent]:
        """Force-close a position whose price feed has gone dark (delisted / dead
        venue / bad symbol). Closes at the LAST KNOWN price — we have nothing newer,
        and an un-priced position can never step → it would sit open forever,
        miscounting slots. Honest exit at the last real price, ~flat PnL."""
        pos = self.positions.get(key)
        if not pos or pos.closed:
            return []
        price = pos.last_price if pos.last_price > 0 else pos.entry_price
        return [self._sell(pos, price, 1.0, reason)]

    def entry_quality(self, pos: ManagedPosition) -> str:
        """Grade the entry vs the peak to feed the learning loop."""
        secs_to_peak = (pos.peak_at - pos.entry_at).total_seconds()
        peak_gain = (pos.peak_price - pos.entry_price) / pos.entry_price * 100 if pos.entry_price > 0 else 0
        if peak_gain < 5 or secs_to_peak < 60:
            return "late_entry"        # bought near the top — barely ran after entry
        if peak_gain >= 30:
            return "early_entry"
        return "perfect_entry"
