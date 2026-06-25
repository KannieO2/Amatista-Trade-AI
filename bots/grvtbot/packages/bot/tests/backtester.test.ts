// H.6 — Backtester unit tests.
// Pure-function tests: feed synthetic candles, assert stats. No GRVT,
// no DB. getInstrumentSpec falls back to a sane default when the pair
// isn't in the cache, so no mocking is needed.

import { describe, it, expect } from 'vitest';
import { runBacktest, type BacktestCandle } from '../src/bot/backtester';

const BASE = {
  pair: 'TEST_USDT_Perp',
  direction: 'long' as const,
  leverage: 1,
  lowerPrice: 100,
  upperPrice: 110,
  numGrids: 10,
  investmentUSDT: 1000,
};

// Build a candle that sweeps from `low` to `high` so every grid level
// inside the range is touched. One candle per second from t=0.
function sweepCandle(low: number, high: number, time: number): BacktestCandle {
  return { time, open: low, close: high, low, high };
}

// Anchor candle at a fixed price — used as the first candle so the
// engine's level-side assignment (buys below mid, sells above) lines up
// with what the test wants. `close` here = `firstPrice` in runBacktest.
function anchor(price: number, time: number): BacktestCandle {
  return { time, open: price, close: price, low: price, high: price };
}

describe('runBacktest', () => {
  it('returns zero result on empty candles', () => {
    const r = runBacktest(BASE, []);
    expect(r.candlesProcessed).toBe(0);
    expect(r.roundTrips).toBe(0);
    expect(r.totalProfit).toBe(0);
    expect(r.netProfit).toBe(0);
    expect(r.equityCurve).toEqual([]);
  });

  it('records round trips when price oscillates through the range', () => {
    // Anchor at mid (105) so levels split into 5 buys (100-104) and
    // 6 sells (105-110). Each upward sweep fills sells, each downward
    // sweep refills the matching buys → round trips.
    const candles: BacktestCandle[] = [
      anchor(105, 0),
      sweepCandle(99, 111, 3600),
      sweepCandle(99, 111, 7200),
    ];
    const r = runBacktest(BASE, candles);
    expect(r.candlesProcessed).toBe(3);
    expect(r.roundTrips).toBeGreaterThan(0);
    expect(r.totalProfit).toBeGreaterThan(0);
    expect(r.equityCurve).toHaveLength(3);
  });

  it('charges fees on every round trip and reduces netProfit accordingly', () => {
    const candles: BacktestCandle[] = [
      anchor(105, 0),
      sweepCandle(99, 111, 3600),
    ];
    const noFee = runBacktest({ ...BASE, feePct: 0 }, candles);
    const withFee = runBacktest({ ...BASE, feePct: 0.05 }, candles);

    expect(noFee.roundTrips).toBeGreaterThan(0);
    expect(noFee.totalFees).toBe(0);
    expect(withFee.totalFees).toBeGreaterThan(0);
    expect(withFee.netProfit).toBeLessThan(noFee.netProfit);
    // Same gross profit either way — fees only affect net.
    expect(withFee.totalProfit).toBeCloseTo(noFee.totalProfit, 2);
  });

  it('tracks max drawdown when price falls below the range', () => {
    // Anchor at mid → buys at 100-104. Sweep upward fills nothing on
    // buys but does fill sells (no position yet — short sells aren't
    // modeled, this is a long grid). Then a crash candle creates
    // mark-to-market drawdown on whatever long position was opened.
    const candles: BacktestCandle[] = [
      anchor(105, 0),
      { time: 3600, open: 105, high: 105, low: 99, close: 99 },  // hits all buys
      { time: 7200, open: 99, high: 99, low: 50, close: 50 },    // crash
    ];
    const r = runBacktest(BASE, candles);
    expect(r.maxDrawdownPct).toBeGreaterThan(0);
  });

  it('reports daysInMarket from first to last candle time', () => {
    const oneDay = 86400;
    const candles: BacktestCandle[] = [
      anchor(105, 0),
      anchor(105, oneDay * 7),
    ];
    const r = runBacktest(BASE, candles);
    expect(r.daysInMarket).toBe(7);
  });
});
