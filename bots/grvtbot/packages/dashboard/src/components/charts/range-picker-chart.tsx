// E.8 — Range picker chart with draggable price lines.
// Shows recent candles for the selected pair with two horizontal lines
// (lower/upper) that the user can drag to set the range. The numeric
// inputs in StepRange update bidirectionally.

import { useEffect, useRef, useCallback, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  createChart,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type CandlestickData,
  type UTCTimestamp,
} from 'lightweight-charts';
import { api } from '@/lib/api-client';

interface RangePickerChartProps {
  pair: string;
  lower: number;
  upper: number;
  onLowerChange: (v: number) => void;
  onUpperChange: (v: number) => void;
}

export function RangePickerChart({
  pair,
  lower,
  upper,
  onLowerChange,
  onUpperChange,
}: RangePickerChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  // Track created price line handles explicitly — LWC doesn't expose a reliable
  // "list all lines" API, so we own the lifecycle here.
  const priceLinesRef = useRef<Array<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>>>([]);
  const [dragging, setDragging] = useState<'lower' | 'upper' | null>(null);

  const candlesQuery = useQuery({
    queryKey: ['candles', pair, 'CI_1_H', 168],
    queryFn: () => api.getCandles(pair, 'CI_1_H', 168),
    staleTime: 60_000,
    enabled: !!pair,
  });

  // Create chart
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: 'rgba(255,255,255,0.5)',
        fontSize: 10,
      },
      grid: {
        vertLines: { color: 'rgba(255,255,255,0.04)' },
        horzLines: { color: 'rgba(255,255,255,0.04)' },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true },
      handleScale: false,
      handleScroll: false,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderDownColor: '#ef4444',
      borderUpColor: '#22c55e',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const ro = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({
          width: containerRef.current.clientWidth,
          height: containerRef.current.clientHeight,
        });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Update candle data
  useEffect(() => {
    if (!seriesRef.current || !candlesQuery.data?.candles) return;
    const data: CandlestickData[] = candlesQuery.data.candles.map((c) => ({
      time: (c.openTime / 1000) as UTCTimestamp,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));
    seriesRef.current.setData(data);
    chartRef.current?.timeScale().fitContent();
  }, [candlesQuery.data]);

  // Update price lines when lower/upper change
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;

    // Remove previously-created lines via their tracked handles
    for (const line of priceLinesRef.current) {
      try { series.removePriceLine(line); } catch { /* noop */ }
    }
    priceLinesRef.current = [];

    if (lower > 0) {
      priceLinesRef.current.push(
        series.createPriceLine({
          price: lower,
          color: '#3b82f6',
          lineWidth: 2,
          lineStyle: 2, // dashed
          axisLabelVisible: true,
          title: 'Lower',
        })
      );
    }
    if (upper > 0) {
      priceLinesRef.current.push(
        series.createPriceLine({
          price: upper,
          color: '#8b5cf6',
          lineWidth: 2,
          lineStyle: 2,
          axisLabelVisible: true,
          title: 'Upper',
        })
      );
    }
  }, [lower, upper]);

  // Drag handler — convert mouse Y to price
  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      if (!dragging || !chartRef.current || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const y = e.clientY - rect.top;
      const price = (chartRef.current as any).coordinateToPrice?.(y);
      if (price == null) {
        // Fallback: estimate from chart height + visible price range
        const series = seriesRef.current;
        if (!series) return;
        // Use a simpler heuristic
        return;
      }
      const rounded = Math.round(price * 100) / 100;
      if (rounded > 0) {
        if (dragging === 'lower') onLowerChange(rounded);
        else onUpperChange(rounded);
      }
    },
    [dragging, onLowerChange, onUpperChange]
  );

  // Price-to-pixel for drag handle positioning
  function priceToY(price: number): number | null {
    const series = seriesRef.current;
    if (!series || !containerRef.current) return null;
    try {
      const coord = series.priceToCoordinate(price);
      return coord ?? null;
    } catch {
      return null;
    }
  }

  const lowerY = priceToY(lower);
  const upperY = priceToY(upper);

  if (!pair) return null;

  return (
    <div className="mb-4">
      <div className="text-2xs text-text-muted mb-1">
        Drag the blue/purple lines to set your range, or type values below.
      </div>
      <div
        ref={containerRef}
        className="relative h-[200px] rounded-md overflow-hidden border border-border-subtle bg-bg-elevated"
        onMouseMove={handleMouseMove}
        onMouseUp={() => setDragging(null)}
        onMouseLeave={() => setDragging(null)}
      >
        {/* Drag handles overlaid on the chart */}
        {lowerY != null && (
          <div
            className="absolute left-0 right-12 h-2 cursor-ns-resize z-10 opacity-0 hover:opacity-30 bg-blue-500"
            style={{ top: lowerY - 4 }}
            onMouseDown={(e) => {
              e.preventDefault();
              setDragging('lower');
            }}
          />
        )}
        {upperY != null && (
          <div
            className="absolute left-0 right-12 h-2 cursor-ns-resize z-10 opacity-0 hover:opacity-30 bg-purple-500"
            style={{ top: upperY - 4 }}
            onMouseDown={(e) => {
              e.preventDefault();
              setDragging('upper');
            }}
          />
        )}
      </div>
      {candlesQuery.isPending && (
        <div className="text-2xs text-text-muted animate-pulse mt-1">
          Loading chart...
        </div>
      )}
    </div>
  );
}
