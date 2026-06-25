// Bot Detail equity curve — Recharts AreaChart with axes + tooltip.
// Cleaner-looking than Lightweight Charts for slow-moving daily data.

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { formatUsd } from '@/lib/format';
import type { DailySnapshot } from '@/lib/api-types';

export interface EquityPoint {
  date: string;
  equity: number;
}

interface EquityCurveProps {
  // Either pass per-bot snapshots (newest-first, mapped from /bots/:id/snapshots)
  // OR pre-aggregated points (oldest-first, e.g. /portfolio-equity-curve).
  snapshots?: DailySnapshot[];
  points?: EquityPoint[];
  height?: number;
}

export function EquityCurve({ snapshots, points, height = 240 }: EquityCurveProps) {
  // Normalize either input to chronological {date, equity}[].
  // - snapshots: newest-first, equity field is `equity_usdt` (legacy alias).
  // - points: already chronological; pass through.
  const data: EquityPoint[] = points
    ? points
    : (snapshots ?? []).slice().reverse().map((s) => ({
        date: s.date,
        equity: s.equity_usdt,
      }));

  if (data.length === 0) {
    return (
      <div
        style={{ height }}
        className="flex items-center justify-center text-sm text-text-muted"
      >
        No snapshot history yet
      </div>
    );
  }

  const first = data[0]?.equity ?? 0;
  const last = data[data.length - 1]?.equity ?? 0;
  const isUp = last >= first;
  const stroke = isUp ? '#22C55E' : '#EF4444';

  const pctChange = first > 0 ? ((last - first) / first) * 100 : 0;
  const ariaLabel = `Equity curve ${data.length} daily snapshots ${
    isUp ? 'up' : 'down'
  } ${pctChange.toFixed(1)}% from ${first.toFixed(2)} to ${last.toFixed(2)}`;

  return (
    <div
      style={{ width: '100%', height }}
      role="img"
      aria-label={ariaLabel}
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart
          data={data}
          margin={{ top: 5, right: 5, bottom: 5, left: 5 }}
        >
          <defs>
            <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.3} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#1E293B" vertical={false} />
          <XAxis
            dataKey="date"
            tick={{ fill: '#94A3B8', fontSize: 10, fontFamily: 'JetBrains Mono' }}
            stroke="#1E293B"
            tickFormatter={(v: string) => v.slice(5)}
          />
          <YAxis
            tick={{ fill: '#94A3B8', fontSize: 10, fontFamily: 'JetBrains Mono' }}
            stroke="#1E293B"
            domain={['auto', 'auto']}
            tickFormatter={(v: number) => `$${v.toFixed(0)}`}
            width={60}
          />
          <Tooltip
            contentStyle={{
              background: '#0F172A',
              border: '1px solid #334155',
              borderRadius: 6,
              fontSize: 12,
              fontFamily: 'JetBrains Mono',
            }}
            labelStyle={{ color: '#94A3B8' }}
            itemStyle={{ color: '#F8FAFC' }}
            formatter={(v: number) => [formatUsd(v), 'Equity']}
          />
          <Area
            type="monotone"
            dataKey="equity"
            stroke={stroke}
            strokeWidth={2}
            fill="url(#equity-fill)"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
