// Compact equity sparkline for BotCard. ~40px tall, no axes, no tooltip.
// Color flips green/red based on first vs last value.

import { Area, AreaChart, ResponsiveContainer } from 'recharts';

interface SparklineProps {
  data: Array<{ value: number }>;
  height?: number;
}

export function Sparkline({ data, height = 40 }: SparklineProps) {
  if (data.length < 2) {
    return <div style={{ height }} className="text-2xs text-text-disabled">—</div>;
  }
  const first = data[0]?.value ?? 0;
  const last = data[data.length - 1]?.value ?? 0;
  const isUp = last >= first;
  const stroke = isUp ? '#22C55E' : '#EF4444';
  const fillId = `spark-${isUp ? 'up' : 'down'}`;

  const pctChange = first > 0 ? ((last - first) / first) * 100 : 0;
  const direction = isUp ? 'up' : 'down';
  const ariaLabel = `Equity sparkline ${direction} ${pctChange.toFixed(1)}% over ${data.length} snapshots`;

  return (
    <div
      style={{ height, width: '100%' }}
      role="img"
      aria-label={ariaLabel}
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 2, bottom: 2, left: 0, right: 0 }}>
          <defs>
            <linearGradient id={fillId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={stroke} stopOpacity={0.4} />
              <stop offset="100%" stopColor={stroke} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="value"
            stroke={stroke}
            strokeWidth={1.5}
            fill={`url(#${fillId})`}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
