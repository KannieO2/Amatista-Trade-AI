"""Real-time price feed over exchange WebSockets (latency ~<1s vs polling 10-60s).

FAIL-SAFE BY DESIGN. This is an *accelerator*, never a dependency: every caller
reads `get_price()` and falls back to its normal REST polling when the cache is
empty (None). If a socket never connects, parses wrong, or the whole module is
disabled (PUMP_USE_WEBSOCKETS=false), the bot keeps working exactly as before —
just on polling. No exception here ever propagates into a trading loop.

Per-exchange ticker formats differ; Binance is validated live. Bybit/OKX/MEXC/
Bitget are best-effort: if their payload doesn't parse, those symbols simply stay
on polling. Symbols use the bot's ccxt form ("WAL/USDT"); the manager converts to
each exchange's native form and maps the echoes back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import websockets

logger = logging.getLogger("pump-reader.websocket")

USE_WEBSOCKETS = os.getenv("PUMP_USE_WEBSOCKETS", "true").lower() == "true"
RECONNECT_DELAY = float(os.getenv("PUMP_WEBSOCKET_RECONNECT_SECONDS", "5"))
WS_MAX_SYMBOLS = int(os.getenv("PUMP_WEBSOCKET_MAX_SYMBOLS", "40"))

# Public ticker endpoints (no API key).
WS_URLS = {
    "binance": "wss://stream.binance.com:9443/ws",
    "bybit": "wss://stream.bybit.com/v5/public/spot",
    "okx": "wss://ws.okx.com:8443/ws/v5/public",
    "mexc": "wss://wbs.mexc.com/ws",
    "bitget": "wss://ws.bitget.com/v2/ws/public",
}


def _native(exchange: str, ccxt_symbol: str) -> str:
    base, _, quote = ccxt_symbol.partition("/")
    if exchange == "okx":
        return f"{base}-{quote}"
    return f"{base}{quote}"


class WebSocketManager:
    def __init__(self) -> None:
        self.subs: dict[str, set[str]] = {}            # exchange -> {ccxt_symbol}
        self.tasks: dict[str, asyncio.Task] = {}        # exchange -> connection task
        self.native2ccxt: dict[str, str] = {}           # "exchange:NATIVE" -> ccxt_symbol
        self.price_cache: dict[str, float] = {}         # "exchange:ccxt_symbol" -> price
        self.price_ts: dict[str, float] = {}            # "exchange:ccxt_symbol" -> monotonic ts
        self.callbacks: list = []
        self.running = False
        # --- health / diagnostics counters ---
        self.msg_count = 0
        self.last_msg_at = 0.0       # monotonic of the last parsed price
        self.reconnects = 0
        self.connects = 0

    def add_callback(self, cb) -> None:
        """cb(exchange: str, ccxt_symbol: str, price: float) -> Coroutine."""
        self.callbacks.append(cb)

    def get_price(self, exchange: str, ccxt_symbol: str) -> float | None:
        return self.price_cache.get(f"{exchange}:{ccxt_symbol}")

    def get_price_fresh(self, exchange: str, ccxt_symbol: str, max_age_s: float) -> float | None:
        """Price only if the last WS update is within max_age_s — else None so the
        caller falls back to REST (never act on a frozen feed)."""
        key = f"{exchange}:{ccxt_symbol}"
        ts = self.price_ts.get(key)
        if ts is None or (time.monotonic() - ts) > max_age_s:
            return None
        return self.price_cache.get(key)

    def health(self) -> dict:
        """Live socket health for the diagnostics panel / degradation alerts."""
        now = time.monotonic()
        n_sub = sum(len(s) for s in self.subs.values())
        fresh = sum(1 for ts in self.price_ts.values() if now - ts <= 30)
        stale = sum(1 for ts in self.price_ts.values() if now - ts > 30)
        return {
            "enabled": USE_WEBSOCKETS,
            "running": self.running,
            "exchanges": sorted(self.subs.keys()),
            "subscriptions": n_sub,
            "tracked_symbols": len(self.price_ts),
            "fresh_feeds": fresh,
            "stale_feeds": stale,
            "messages": self.msg_count,
            "connects": self.connects,
            "reconnects": self.reconnects,
            "last_msg_age_s": round(now - self.last_msg_at, 1) if self.last_msg_at else None,
        }

    async def resync(self, pairs: list[tuple[str, str]]) -> None:
        """Set the desired (exchange, ccxt_symbol) subscriptions. (Re)starts the
        connection for any exchange whose symbol set changed; drops the rest."""
        if not USE_WEBSOCKETS:
            return
        want: dict[str, set[str]] = {}
        for ex, sym in pairs:
            if ex in WS_URLS:
                want.setdefault(ex, set()).add(sym)
        # Cap per-exchange to keep a single socket light.
        for ex in want:
            if len(want[ex]) > WS_MAX_SYMBOLS:
                want[ex] = set(sorted(want[ex])[:WS_MAX_SYMBOLS])
        self.running = True
        for ex, syms in want.items():
            if self.subs.get(ex) != syms:
                self.subs[ex] = syms
                for s in syms:
                    self.native2ccxt[f"{ex}:{_native(ex, s)}"] = s
                old = self.tasks.get(ex)
                if old:
                    old.cancel()
                self.tasks[ex] = asyncio.create_task(self._run(ex))
        for ex in list(self.subs):
            if ex not in want:
                self.subs.pop(ex, None)
                t = self.tasks.pop(ex, None)
                if t:
                    t.cancel()

    async def stop(self) -> None:
        self.running = False
        for t in self.tasks.values():
            t.cancel()
        self.tasks.clear()

    async def _run(self, exchange: str) -> None:
        url = WS_URLS[exchange]
        while self.running:
            syms = sorted(self.subs.get(exchange, set()))
            if not syms:
                return
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=15,
                                              close_timeout=5, max_queue=256) as ws:
                    self.connects += 1
                    await self._subscribe(ws, exchange, syms)
                    async for msg in ws:
                        try:
                            self._handle(exchange, json.loads(msg))
                        except Exception:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.reconnects += 1
                logger.debug("ws %s down: %s — reconnect in %ss", exchange, e, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

    async def _subscribe(self, ws, exchange: str, syms: list[str]) -> None:
        nat = [_native(exchange, s) for s in syms]
        if exchange == "binance":
            await ws.send(json.dumps({"method": "SUBSCRIBE",
                                      "params": [f"{n.lower()}@ticker" for n in nat], "id": 1}))
        elif exchange == "bybit":
            await ws.send(json.dumps({"op": "subscribe", "args": [f"tickers.{n}" for n in nat]}))
        elif exchange == "okx":
            await ws.send(json.dumps({"op": "subscribe",
                                      "args": [{"channel": "tickers", "instId": n} for n in nat]}))
        elif exchange == "mexc":
            await ws.send(json.dumps({"method": "SUBSCRIPTION",
                                      "params": [f"spot@public.miniTicker.v3.api@{n}@UTC+0" for n in nat]}))
        elif exchange == "bitget":
            await ws.send(json.dumps({"op": "subscribe",
                                      "args": [{"instType": "SPOT", "channel": "ticker", "instId": n} for n in nat]}))

    def _handle(self, exchange: str, data: dict) -> None:
        pairs: list[tuple[str, str]] = []  # (native, raw_price)
        if exchange == "binance":
            if isinstance(data, dict) and data.get("s") and data.get("c"):
                pairs.append((data["s"], data["c"]))
        elif exchange == "bybit":
            d = data.get("data") if isinstance(data, dict) else None
            if isinstance(d, dict) and d.get("symbol") and d.get("lastPrice"):
                pairs.append((d["symbol"], d["lastPrice"]))
        elif exchange == "okx":
            for it in (data.get("data") or []):
                if it.get("instId") and it.get("last"):
                    pairs.append((it["instId"], it["last"]))
        elif exchange == "mexc":
            d = data.get("d") or {}
            sym = data.get("s") or d.get("s") or d.get("symbol")
            px = d.get("c") or d.get("last") or d.get("p")
            if sym and px:
                pairs.append((sym, px))
        elif exchange == "bitget":
            for it in (data.get("data") or []):
                px = it.get("lastPr") or it.get("last")
                if it.get("instId") and px:
                    pairs.append((it["instId"], px))
        for native, raw in pairs:
            try:
                price = float(raw)
            except (TypeError, ValueError):
                continue
            ccxt_symbol = self.native2ccxt.get(f"{exchange}:{native}")
            if not ccxt_symbol or price <= 0:
                continue
            now = time.monotonic()
            key = f"{exchange}:{ccxt_symbol}"
            self.price_cache[key] = price
            self.price_ts[key] = now
            self.msg_count += 1
            self.last_msg_at = now
            for cb in self.callbacks:
                try:
                    asyncio.create_task(cb(exchange, ccxt_symbol, price))
                except Exception:
                    pass


_manager: WebSocketManager | None = None


def get_manager() -> WebSocketManager:
    global _manager
    if _manager is None:
        _manager = WebSocketManager()
    return _manager


async def initialize_websockets(exchanges: list[str], symbols: list[str]) -> None:
    if not USE_WEBSOCKETS:
        return
    mgr = get_manager()
    await mgr.resync([(e, s) for e in exchanges for s in symbols])
