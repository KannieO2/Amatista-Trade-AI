"""Execution telemetry: entry-latency stages + exit diagnostics (in-memory).

Pure observability. Nothing here ever changes a trading decision; it only records
how fast / from where the engine acted, so hidden delays surface. Fail-safe:
every record() swallows its own errors. Exit rows are ALSO persisted by main.py
for cross-restart analytics; the in-memory buffers here back the live dashboard.
"""

from __future__ import annotations

import time
from collections import deque
from statistics import mean, median


def _pctile(values: list[float], p: float):
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round((p / 100) * (len(s) - 1))))
    return s[k]


class Stopwatch:
    """Monotonic stage timer. mark(name) stores ms since the previous mark."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.last = self.t0
        self.stages: dict[str, float] = {}

    def mark(self, name: str) -> float:
        now = time.monotonic()
        ms = round((now - self.last) * 1000, 1)
        self.stages[name] = ms
        self.last = now
        return ms

    def total_ms(self) -> float:
        return round((time.monotonic() - self.t0) * 1000, 1)


class LatencyTracker:
    """Entry-pipeline stage latencies (ms): Market Event -> Detection -> Signal ->
    Risk Validation -> Order Submission. Keeps the last `maxlen` entries."""

    STAGES = ("detection_ms", "validation_ms", "order_ms", "total_ms")

    def __init__(self, maxlen: int = 500) -> None:
        self.samples: deque[dict] = deque(maxlen=maxlen)

    def record(self, stages_ms: dict) -> None:
        try:
            self.samples.append(stages_ms)
        except Exception:
            pass

    def metrics(self) -> dict:
        out: dict = {"n": len(self.samples)}
        for k in self.STAGES:
            vals = [s[k] for s in self.samples if isinstance(s.get(k), (int, float))]
            out[k] = {
                "avg": round(mean(vals), 1) if vals else None,
                "median": round(median(vals), 1) if vals else None,
                "p95": round(_pctile(vals, 95), 1) if vals else None,
                "worst": round(max(vals), 1) if vals else None,
            }
        return out


class ExitTelemetry:
    """Per-exit diagnostic rows (signal/entry/exit times, reaction delay, etc.)."""

    def __init__(self, maxlen: int = 300) -> None:
        self.rows: deque[dict] = deque(maxlen=maxlen)

    def record(self, row: dict) -> None:
        try:
            self.rows.append(row)
        except Exception:
            pass

    def recent(self, limit: int = 50) -> list[dict]:
        return list(self.rows)[-limit:][::-1]

    def summary(self) -> dict:
        rows = list(self.rows)
        if not rows:
            return {"n": 0}
        holds = [r["holding_secs"] for r in rows if isinstance(r.get("holding_secs"), (int, float))]
        ws = sum(1 for r in rows if r.get("exit_source") == "ws")
        delays = [r["ws_reaction_delay_ms"] for r in rows
                  if isinstance(r.get("ws_reaction_delay_ms"), (int, float))]
        reasons: dict[str, int] = {}
        for r in rows:
            reasons[r.get("exit_reason", "?")] = reasons.get(r.get("exit_reason", "?"), 0) + 1
        return {
            "n": len(rows),
            "ws_exit_rate": round(ws / len(rows), 3),
            "avg_hold_secs": round(mean(holds), 1) if holds else None,
            "avg_ws_reaction_delay_ms": round(mean(delays), 1) if delays else None,
            "exit_reasons": reasons,
        }


# Module-level singletons consumed by main.py.
latency = LatencyTracker()
exits = ExitTelemetry()
