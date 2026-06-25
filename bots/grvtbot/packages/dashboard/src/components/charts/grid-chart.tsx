// GridChart — the hero of the Bot Detail page.
//
// Renders ETH-USDT-Perp (or any pair) candles with grid level overlays:
//   - green priceLine = active buy order
//   - red priceLine = active sell order
//   - dashed muted line = filled level (gap in the grid)
//   - amber line = pending_replace
//   - bold sky line = current mark price
//
// Implementation notes:
//   - Lightweight Charts v5 API: createChart + chart.addSeries(CandlestickSeries, ...).
//   - Series and chart instances are stored in refs and torn down on unmount.
//   - Grid levels are rendered as priceLines on the candlestick series so they
//     scale with the price axis automatically.
//   - When the bot's grid-state changes, we diff the previous priceLine set
//     against the new one and add/remove minimally — recreating all 93 lines
//     on every tick would thrash the canvas.
//   - When a level transitions filled→active or active→filled, we briefly
//     replace its color with white for 600ms (G-style fill animation).
//   - Resize: ResizeObserver on the container, applyOptions({width, height}).

import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type PriceLineOptions,
  type UTCTimestamp,
} from 'lightweight-charts';
import { useEffect, useRef } from 'react';
import type { Candle, GridLevel } from '@/lib/api-types';

interface GridChartProps {
  candles: Candle[];
  levels: GridLevel[];
  markPrice: number | null;
  entryPrice: number | null;
  liquidationPrice?: number | null;
  // Set of level_index values that just transitioned (for the flash animation).
  // Caller is responsible for clearing this after the animation duration.
  recentlyFilled?: Set<number>;
  className?: string;
}

const COLORS = {
  bgElevated: '#0F172A',
  borderSubtle: '#1E293B',
  textMuted: '#94A3B8',
  candleUp: '#22C55E',
  candleUpBorder: '#16A34A',
  candleDown: '#EF4444',
  candleDownBorder: '#DC2626',
  buy: '#22C55E',
  sell: '#EF4444',
  filled: '#475569',
  pending: '#F59E0B',
  mark: '#38BDF8',
  entry: '#A78BFA',
  liquidation: '#EF4444',
  flash: '#F8FAFC',
  // H.8 virtual grids: muted gray-blue, dotted, so they read as "out of
  // window — placeholder, no live order on exchange".
  virtualBuy: '#1E3A2A',
  virtualSell: '#3A1E1E',
};

const FLASH_DURATION_MS = 600;

interface PriceLineEntry {
  line: IPriceLine;
  signature: string; // for diff
}

export function GridChart({
  candles,
  levels,
  markPrice,
  entryPrice,
  liquidationPrice,
  recentlyFilled,
  className,
}: GridChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const levelLinesRef = useRef<Map<number, PriceLineEntry>>(new Map());
  const markLineRef = useRef<IPriceLine | null>(null);
  const entryLineRef = useRef<IPriceLine | null>(null);
  const liqLineRef = useRef<IPriceLine | null>(null);

  // ── Chart lifecycle ────────────────────────────────────────────────
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      width: el.clientWidth,
      height: el.clientHeight,
      layout: {
        background: { type: ColorType.Solid, color: COLORS.bgElevated },
        textColor: COLORS.textMuted,
        fontFamily:
          'JetBrains Mono, SF Mono, Monaco, Consolas, monospace',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: COLORS.borderSubtle, style: 1 },
        horzLines: { color: COLORS.borderSubtle, style: 1 },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: COLORS.textMuted, width: 1, style: 3 },
        horzLine: { color: COLORS.textMuted, width: 1, style: 3 },
      },
      rightPriceScale: {
        borderColor: COLORS.borderSubtle,
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      timeScale: {
        borderColor: COLORS.borderSubtle,
        timeVisible: true,
        secondsVisible: false,
      },
      autoSize: false,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: COLORS.candleUp,
      downColor: COLORS.candleDown,
      borderUpColor: COLORS.candleUpBorder,
      borderDownColor: COLORS.candleDownBorder,
      wickUpColor: COLORS.candleUpBorder,
      wickDownColor: COLORS.candleDownBorder,
      priceLineVisible: false,
    });

    chartRef.current = chart;
    seriesRef.current = series;

    // Resize observer — keeps the chart matching its container.
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        chart.applyOptions({
          width: Math.floor(entry.contentRect.width),
          height: Math.floor(entry.contentRect.height),
        });
      }
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      levelLinesRef.current.clear();
      markLineRef.current = null;
      entryLineRef.current = null;
      liqLineRef.current = null;
    };
  }, []);

  // ── Candle data ────────────────────────────────────────────────────
  useEffect(() => {
    const series = seriesRef.current;
    if (!series || candles.length === 0) return;

    // Lightweight Charts wants seconds, not milliseconds.
    // It also wants ascending order with no duplicate timestamps.
    const data: CandlestickData[] = candles.map((c) => ({
      time: Math.floor(c.openTime / 1000) as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));

    series.setData(data);
    chartRef.current?.timeScale().fitContent();
  }, [candles]);

  // ── Grid level priceLines ──────────────────────────────────────────
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;

    const existing = levelLinesRef.current;
    const nextIndices = new Set<number>();

    for (const level of levels) {
      nextIndices.add(level.level_index);

      const flash = recentlyFilled?.has(level.level_index) ?? false;
      const opts = priceLineOptionsFor(level, flash);
      const signature = signatureFor(opts);
      const entry = existing.get(level.level_index);

      if (!entry) {
        // Create new line
        existing.set(level.level_index, {
          line: series.createPriceLine(opts),
          signature,
        });
      } else if (entry.signature !== signature) {
        // Recreate (LWC priceLines don't expose updateOptions for color in
        // all versions; remove + create is the safe path).
        series.removePriceLine(entry.line);
        existing.set(level.level_index, {
          line: series.createPriceLine(opts),
          signature,
        });
      }
    }

    // Remove lines whose level no longer exists.
    for (const [idx, entry] of existing) {
      if (!nextIndices.has(idx)) {
        series.removePriceLine(entry.line);
        existing.delete(idx);
      }
    }
  }, [levels, recentlyFilled]);

  // ── Mark / entry / liquidation reference lines ─────────────────────
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;

    // Mark price (current)
    if (markPrice != null && Number.isFinite(markPrice)) {
      const opts: PriceLineOptions = {
        price: markPrice,
        color: COLORS.mark,
        lineWidth: 2,
        lineStyle: 0, // solid
        axisLabelVisible: true,
        title: 'MARK',
        lineVisible: true,
        axisLabelColor: COLORS.mark,
        axisLabelTextColor: '#020617',
      };
      if (markLineRef.current) series.removePriceLine(markLineRef.current);
      markLineRef.current = series.createPriceLine(opts);
    } else if (markLineRef.current) {
      series.removePriceLine(markLineRef.current);
      markLineRef.current = null;
    }

    // Entry price
    if (entryPrice != null && entryPrice > 0 && Number.isFinite(entryPrice)) {
      const opts: PriceLineOptions = {
        price: entryPrice,
        color: COLORS.entry,
        lineWidth: 1,
        lineStyle: 2, // dashed
        axisLabelVisible: true,
        title: 'ENTRY',
        lineVisible: true,
        axisLabelColor: COLORS.entry,
        axisLabelTextColor: '#020617',
      };
      if (entryLineRef.current) series.removePriceLine(entryLineRef.current);
      entryLineRef.current = series.createPriceLine(opts);
    } else if (entryLineRef.current) {
      series.removePriceLine(entryLineRef.current);
      entryLineRef.current = null;
    }

    // Liquidation
    if (
      liquidationPrice != null &&
      liquidationPrice > 0 &&
      Number.isFinite(liquidationPrice)
    ) {
      const opts: PriceLineOptions = {
        price: liquidationPrice,
        color: COLORS.liquidation,
        lineWidth: 2,
        lineStyle: 2, // dashed
        axisLabelVisible: true,
        title: 'LIQ',
        lineVisible: true,
        axisLabelColor: COLORS.liquidation,
        axisLabelTextColor: '#F8FAFC',
      };
      if (liqLineRef.current) series.removePriceLine(liqLineRef.current);
      liqLineRef.current = series.createPriceLine(opts);
    } else if (liqLineRef.current) {
      series.removePriceLine(liqLineRef.current);
      liqLineRef.current = null;
    }
  }, [markPrice, entryPrice, liquidationPrice]);

  // Build a screen-reader summary of the chart state. Lightweight Charts
  // renders to a canvas which is opaque to assistive tech, so we expose
  // the key facts as an aria-label.
  const activeBuy = levels.filter((l) => l.side === 'buy' && l.is_filled === 0 && l.state !== 'virtual').length;
  const activeSell = levels.filter((l) => l.side === 'sell' && l.is_filled === 0 && l.state !== 'virtual').length;
  const virtualCount = levels.filter((l) => l.state === 'virtual').length;
  const filledCount = levels.filter((l) => l.is_filled === 1).length;
  const virtualSegment = virtualCount > 0 ? `, ${virtualCount} virtual` : '';
  const ariaLabel =
    `Grid chart with ${candles.length} candles. ` +
    `${levels.length} levels: ${activeBuy} active buys, ${activeSell} active sells, ${filledCount} filled${virtualSegment}. ` +
    (markPrice ? `Mark price ${markPrice.toFixed(2)}. ` : '') +
    (entryPrice ? `Entry price ${entryPrice.toFixed(2)}.` : '');

  return (
    <div
      ref={containerRef}
      className={className}
      style={{ width: '100%', height: '100%', minHeight: 320 }}
      role="img"
      aria-label={ariaLabel}
    />
  );
}

// ── Helpers ──────────────────────────────────────────────────────────

function priceLineOptionsFor(level: GridLevel, flash: boolean): PriceLineOptions {
  const isFilled = level.is_filled === 1;
  const isPending = level.pending_replace === 1;
  const isVirtual = level.state === 'virtual';

  let color: string;
  let lineStyle = 0; // solid
  let lineWidth = 1;
  const title = '';

  if (flash) {
    color = COLORS.flash;
    lineWidth = 2;
  } else if (isPending) {
    color = COLORS.pending;
    lineStyle = 2; // dashed
  } else if (isFilled) {
    color = COLORS.filled;
    lineStyle = 2; // dashed for filled gap
  } else if (isVirtual) {
    // Virtual levels: muted variant of buy/sell color, dotted line — read
    // as "scheduled but not on exchange yet, will activate when window
    // rotates here".
    color = level.side === 'buy' ? COLORS.virtualBuy : COLORS.virtualSell;
    lineStyle = 3; // dotted (LWC: 0=solid, 1=dotted, 2=dashed, 3=large-dashed, 4=sparse-dotted)
  } else if (level.side === 'buy') {
    color = COLORS.buy;
  } else {
    color = COLORS.sell;
  }

  return {
    price: level.price,
    color,
    lineWidth: lineWidth as 1 | 2 | 3 | 4,
    lineStyle,
    axisLabelVisible: false,
    title,
    lineVisible: true,
    axisLabelColor: color,
    axisLabelTextColor: '#020617',
  };
}

function signatureFor(opts: PriceLineOptions): string {
  return `${opts.price}|${opts.color}|${opts.lineStyle}|${opts.lineWidth}`;
}

export const FILL_FLASH_DURATION_MS = FLASH_DURATION_MS;
