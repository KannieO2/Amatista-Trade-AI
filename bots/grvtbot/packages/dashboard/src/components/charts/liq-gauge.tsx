// E.1 — Liquidation distance gauge.
// SVG semicircle showing how close the mark price is to the estimated
// liquidation price. Green (>30%), yellow (15-30%), red (<15%).

interface LiqGaugeProps {
  markPrice: number | null;
  liqPrice: number;
  direction: 'long' | 'short';
}

export function LiqGauge({ markPrice, liqPrice, direction }: LiqGaugeProps) {
  if (!markPrice || !liqPrice || liqPrice <= 0) return null;

  const distancePct =
    direction === 'long'
      ? ((markPrice - liqPrice) / markPrice) * 100
      : ((liqPrice - markPrice) / markPrice) * 100;

  // Clamp to 0-100 for display
  const clamped = Math.max(0, Math.min(100, distancePct));

  // Color thresholds
  const color =
    clamped > 30 ? 'var(--color-success)' :
    clamped > 15 ? 'var(--color-warning)' :
    'var(--color-danger)';

  const label =
    clamped > 30 ? 'Safe' :
    clamped > 15 ? 'Caution' :
    'Danger';

  // SVG arc math — 180° semicircle
  const R = 40; // radius
  const CX = 50;
  const CY = 48;
  const STROKE = 8;
  const circumference = Math.PI * R; // half circle
  const filled = (clamped / 100) * circumference;

  return (
    <div className="flex items-center gap-4 rounded-lg border border-border-subtle bg-bg-elevated p-4">
      <svg viewBox="0 0 100 56" className="w-24 h-14 shrink-0">
        {/* Background arc (gray) */}
        <path
          d={`M ${CX - R} ${CY} A ${R} ${R} 0 0 1 ${CX + R} ${CY}`}
          fill="none"
          stroke="var(--color-border-subtle)"
          strokeWidth={STROKE}
          strokeLinecap="round"
        />
        {/* Filled arc (colored) */}
        <path
          d={`M ${CX - R} ${CY} A ${R} ${R} 0 0 1 ${CX + R} ${CY}`}
          fill="none"
          stroke={color}
          strokeWidth={STROKE}
          strokeLinecap="round"
          strokeDasharray={`${filled} ${circumference}`}
          className="transition-all duration-700 ease-out"
        />
        {/* Percentage text */}
        <text
          x={CX}
          y={CY - 8}
          textAnchor="middle"
          fill="var(--color-text-primary)"
          fontSize="14"
          fontWeight="700"
          fontFamily="var(--font-mono, monospace)"
        >
          {clamped.toFixed(1)}%
        </text>
      </svg>
      <div className="min-w-0">
        <div className="text-2xs uppercase tracking-wider text-text-muted">
          Liq distance
        </div>
        <div className="text-sm font-semibold" style={{ color }}>
          {label}
        </div>
        <div className="text-2xs text-text-muted mt-0.5 truncate">
          Liq @ ${liqPrice.toFixed(2)}
        </div>
      </div>
    </div>
  );
}
