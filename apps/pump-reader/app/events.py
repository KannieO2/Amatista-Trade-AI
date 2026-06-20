"""In-process async event bus (Phase D-0.7 — hybrid event-driven architecture).

Decouples the critical trading path from its observers (telemetry, logging,
learning). Producers call `emit()` and return immediately; subscribed handlers
run as fire-and-forget tasks. FAIL-SAFE BY DESIGN: a slow or throwing handler can
never block or break the producer — emit() only schedules, it never awaits a
handler, and a dispatch error is swallowed.

This is NOT a message broker. It is in-process, best-effort, and ordered only by
asyncio scheduling. Its job is (a) a clean seam between "decide" and "observe",
and (b) a live throughput counter per event type for /diagnostics. The hottest
path (per-tick PRICE_UPDATE) is deliberately NOT emitted here to keep zero
overhead on the price feed; the WebSocket health monitor already counts ticks.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum

logger = logging.getLogger("pump-reader.events")


class EventType(str, Enum):
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    PARTIAL_EXIT = "partial_exit"
    TRAILING_TRIGGER = "trailing_trigger"
    BREAK_EVEN_TRIGGER = "break_even_trigger"
    STOP_LOSS_TRIGGER = "stop_loss_trigger"
    DUMP_TRIGGER = "dump_trigger"
    TIME_STOP_TRIGGER = "time_stop_trigger"
    WS_DISCONNECTED = "ws_disconnected"
    WS_RECONNECTED = "ws_reconnected"


# Exit-reason (from position_manager._sell) -> event type, for the exit path.
EXIT_REASON_EVENT = {
    "hard_stop": EventType.STOP_LOSS_TRIGGER,
    "dump": EventType.DUMP_TRIGGER,
    "break_even": EventType.BREAK_EVEN_TRIGGER,
    "trailing": EventType.TRAILING_TRIGGER,
    "timeout": EventType.TIME_STOP_TRIGGER,
}


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[EventType, list] = {}
        self.counts: dict[str, int] = {}

    def subscribe(self, etype: EventType, handler) -> None:
        """handler(etype: EventType, data: dict) -> Coroutine."""
        self._subs.setdefault(etype, []).append(handler)

    def emit(self, etype: EventType, **data) -> None:
        """Record + dispatch. Non-blocking; never raises into the producer."""
        self.counts[etype.value] = self.counts.get(etype.value, 0) + 1
        for h in self._subs.get(etype, []):
            try:
                asyncio.create_task(h(etype, data))
            except Exception:
                logger.debug("event dispatch failed: %s", etype.value, exc_info=True)

    def stats(self) -> dict:
        return {"counts": dict(self.counts), "total": sum(self.counts.values())}


_bus = EventBus()


def get_bus() -> EventBus:
    return _bus
