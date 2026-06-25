// H.6 — Grid backtesting engine.
// Pure simulation: takes a grid config + historical candles, walks
// each candle to detect level fills, computes profit/drawdown/equity.
// No real GRVT calls, no DB writes — entirely stateless.

import { getInstrumentSpec } from '../api/client.js';

export interface BacktestConfig {
  pair: string;
  direction: 'long' | 'short';
  leverage: number;
  lowerPrice: number;
  upperPrice: number;
  numGrids: number;
  investmentUSDT: number;
  // Per-side fee in percent. 0.05 means 5 bps (GRVT maker default).
  // Charged on both legs of a round trip → real fee impact = 2 * feePct * notional.
  feePct?: number;
}

export interface BacktestCandle {
  time: number; // unix seconds
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface BacktestResult {
  totalProfit: number;
  totalFees: number;
  netProfit: number;
  maxDrawdownPct: number;
  roundTrips: number;
  avgProfitPerTrip: number;
  equityCurve: Array<{ time: number; equity: number }>;
  daysInMarket: number;
  profitFactor: number;
  candlesProcessed: number;
}

interface SimLevel {
  index: number;
  price: number;
  side: 'buy' | 'sell';
  quantity: number;
  isFilled: boolean;
}

export function runBacktest(
  config: BacktestConfig,
  candles: BacktestCandle[]
): BacktestResult {
  if (candles.length === 0) {
    return {
      totalProfit: 0, totalFees: 0, netProfit: 0,
      maxDrawdownPct: 0, roundTrips: 0,
      avgProfitPerTrip: 0, equityCurve: [], daysInMarket: 0,
      profitFactor: 0, candlesProcessed: 0,
    };
  }

  const { min_size: minSize } = getInstrumentSpec(config.pair);
  const spacing = (config.upperPrice - config.lowerPrice) / config.numGrids;
  const midPrice = (config.lowerPrice + config.upperPrice) / 2;
  const effCap = config.investmentUSDT * config.leverage * 0.75;
  const qty = Math.max(
    Math.ceil((effCap / config.numGrids / midPrice) * 100) / 100,
    minSize
  );
  // 5 bps maker default. Charged on both legs of every round trip.
  const feeRate = (config.feePct ?? 0.05) / 100;

  // Initialize grid levels
  const levels: SimLevel[] = [];
  const firstPrice = candles[0]!.close;
  for (let i = 0; i <= config.numGrids; i++) {
    const price = Math.round((config.lowerPrice + i * spacing) * 100) / 100;
    const side: 'buy' | 'sell' =
      config.direction === 'long'
        ? price < firstPrice ? 'buy' : 'sell'
        : price > firstPrice ? 'sell' : 'buy';
    levels.push({ index: i, price, side, quantity: qty, isFilled: false });
  }

  let equity = config.investmentUSDT;
  let hwm = equity;
  let maxDrawdownPct = 0;
  let grossProfit = 0;
  let grossLoss = 0;
  let totalFees = 0;
  let roundTrips = 0;
  let positionSize = 0;
  let positionCost = 0;
  const equityCurve: Array<{ time: number; equity: number }> = [];

  // Walk candles
  for (const candle of candles) {
    // Check each level for fills within this candle's range
    for (const level of levels) {
      if (level.isFilled) continue;

      const hit =
        level.side === 'buy'
          ? candle.low <= level.price
          : candle.high >= level.price;

      if (!hit) continue;

      // Fill!
      level.isFilled = true;

      if (level.side === 'buy') {
        positionSize += level.quantity;
        positionCost += level.price * level.quantity;
      } else {
        // Sell: realize profit from grid spread
        // Find the corresponding buy level (closest lower price)
        const counterIdx = level.index - 1;
        if (counterIdx >= 0 && counterIdx < levels.length) {
          const counterLevel = levels[counterIdx]!;
          const gross = (level.price - counterLevel.price) * level.quantity;
          // Fee charged on both legs of the round trip.
          const fee = (counterLevel.price + level.price) * level.quantity * feeRate;
          const profit = gross - fee;
          if (gross > 0) grossProfit += gross;
          else grossLoss += Math.abs(gross);
          totalFees += fee;
          roundTrips++;
          equity += profit;
        }
        positionSize = Math.max(0, positionSize - level.quantity);
        positionCost = positionSize > 0
          ? positionCost * (positionSize / (positionSize + level.quantity))
          : 0;
      }

      // Reset the counter level so it can trade again (grid cycling)
      const counterIdx = level.side === 'buy' ? level.index + 1 : level.index - 1;
      if (counterIdx >= 0 && counterIdx < levels.length) {
        levels[counterIdx]!.isFilled = false;
      }
    }

    // Unrealized PnL
    const unrealized = positionSize * (candle.close - (positionCost / Math.max(positionSize, 0.0001)));
    const currentEquity = equity + unrealized;

    if (currentEquity > hwm) hwm = currentEquity;
    const dd = hwm > 0 ? ((hwm - currentEquity) / hwm) * 100 : 0;
    if (dd > maxDrawdownPct) maxDrawdownPct = dd;

    equityCurve.push({ time: candle.time, equity: currentEquity });
  }

  const firstTime = candles[0]!.time;
  const lastTime = candles[candles.length - 1]!.time;
  const daysInMarket = Math.max(1, (lastTime - firstTime) / 86400);

  const netProfit = grossProfit - grossLoss - totalFees;
  return {
    totalProfit: Math.round(grossProfit * 100) / 100,
    totalFees: Math.round(totalFees * 100) / 100,
    netProfit: Math.round(netProfit * 100) / 100,
    maxDrawdownPct: Math.round(maxDrawdownPct * 100) / 100,
    roundTrips,
    avgProfitPerTrip: roundTrips > 0 ? Math.round((netProfit / roundTrips) * 100) / 100 : 0,
    equityCurve,
    daysInMarket: Math.round(daysInMarket),
    profitFactor: grossLoss > 0 ? Math.round((grossProfit / grossLoss) * 100) / 100 : grossProfit > 0 ? Infinity : 0,
    candlesProcessed: candles.length,
  };
}
