// E.6 — Grid activity heatmap.
// Shows fill density per grid level per time bucket. Each cell is colored
// by how many fills occurred at that price level in that time window.
// Built with pure CSS grid + inline background-color (no charting lib needed).

import { useMemo, useState } from 'react';
import type { FillRow, GridLevel } from '@/lib/api-types';
import { Mono } from '../primitives/mono';

interface FillHeatmapProps {
  fills: FillRow[];
  levels: GridLevel[];
  spacing: number;
}

interface HeatmapCell {
  levelIndex: number;
  bucketIndex: number;
  count: number;
}

function buildHeatmapData(
  fills: FillRow[],
  levels: GridLevel[],
  spacing: number
) {
  if (fills.length === 0 || levels.length === 0) return null;

  // Sort levels by price
  const sortedLevels = [...levels].sort((a, b) => a.price - b.price);
  const levelPrices = sortedLevels.map((l) => l.price);

  // Find nearest level for a fill price
  function nearestLevelIndex(price: number): number {
    let best = 0;
    let bestDist = Math.abs(price - levelPrices[0]!);
    for (let i = 1; i < levelPrices.length; i++) {
      const dist = Math.abs(price - levelPrices[i]!);
      if (dist < bestDist) {
        bestDist = dist;
        best = i;
      }
    }
    // Only match if within 1.5x spacing
    return bestDist <= spacing * 1.5 ? best : -1;
  }

  // Time bucketing (4-hour buckets for readability)
  const BUCKET_MS = 4 * 60 * 60 * 1000;
  const times = fills
    .map((f) => {
      const ns = Number(f.event_time);
      return ns > 1e15 ? ns / 1e6 : ns; // ns → ms if needed
    })
    .filter((t) => t > 0);

  if (times.length === 0) return null;

  const minTime = Math.min(...times);
  const maxTime = Math.max(...times);
  const numBuckets = Math.min(
    Math.ceil((maxTime - minTime) / BUCKET_MS) + 1,
    48 // cap at ~8 days
  );

  // Build grid
  const grid = new Map<string, number>(); // "levelIdx:bucketIdx" → count
  let maxCount = 0;

  for (const fill of fills) {
    const ns = Number(fill.event_time);
    const ms = ns > 1e15 ? ns / 1e6 : ns;
    if (ms <= 0) continue;

    const li = nearestLevelIndex(fill.price);
    if (li < 0) continue;

    const bi = Math.min(
      Math.floor((ms - minTime) / BUCKET_MS),
      numBuckets - 1
    );
    const key = `${li}:${bi}`;
    const prev = grid.get(key) ?? 0;
    grid.set(key, prev + 1);
    if (prev + 1 > maxCount) maxCount = prev + 1;
  }

  // Build cells array
  const cells: HeatmapCell[] = [];
  for (const [key, count] of grid) {
    const [li, bi] = key.split(':').map(Number);
    cells.push({ levelIndex: li!, bucketIndex: bi!, count });
  }

  // Bucket labels (dates)
  const bucketLabels: string[] = [];
  for (let i = 0; i < numBuckets; i++) {
    const d = new Date(minTime + i * BUCKET_MS);
    bucketLabels.push(
      `${d.getUTCMonth() + 1}/${d.getUTCDate()} ${d.getUTCHours().toString().padStart(2, '0')}h`
    );
  }

  return {
    numLevels: sortedLevels.length,
    numBuckets,
    cells,
    maxCount,
    levelPrices,
    bucketLabels,
    sortedLevels,
  };
}

function cellColor(count: number, max: number): string {
  if (count === 0 || max === 0) return 'transparent';
  const ratio = count / max;
  // oklch interpolation: low = dim primary, high = bright primary
  const l = 0.35 + ratio * 0.3; // lightness 35% → 65%
  const c = 0.05 + ratio * 0.15; // chroma
  return `oklch(${l} ${c} 220)`; // blue hue matching primary
}

export function FillHeatmap({ fills, levels, spacing }: FillHeatmapProps) {
  const data = useMemo(
    () => buildHeatmapData(fills, levels, spacing),
    [fills, levels, spacing]
  );
  const [tooltip, setTooltip] = useState<{
    x: number;
    y: number;
    text: string;
  } | null>(null);

  if (!data || data.cells.length === 0) {
    return (
      <p className="text-xs text-text-muted py-4 text-center">
        No fill data for heatmap yet.
      </p>
    );
  }

  // Show max 20 level rows (sample evenly if more)
  const step = data.numLevels > 20 ? Math.ceil(data.numLevels / 20) : 1;
  const visibleLevels: number[] = [];
  for (let i = 0; i < data.numLevels; i += step) visibleLevels.push(i);

  // Build lookup for quick access
  const cellMap = new Map<string, number>();
  for (const cell of data.cells) {
    cellMap.set(`${cell.levelIndex}:${cell.bucketIndex}`, cell.count);
  }

  return (
    <div className="relative">
      <div className="text-2xs uppercase tracking-wider text-text-muted mb-2">
        Fill Activity Heatmap
      </div>
      <div
        className="overflow-x-auto"
        onMouseLeave={() => setTooltip(null)}
      >
        <div
          className="inline-grid gap-px"
          style={{
            gridTemplateColumns: `60px repeat(${data.numBuckets}, minmax(18px, 1fr))`,
            gridTemplateRows: `repeat(${visibleLevels.length}, 18px) 20px`,
          }}
        >
          {/* Level rows */}
          {visibleLevels.map((li) => (
            <>
              {/* Price label */}
              <div
                key={`label-${li}`}
                className="flex items-center justify-end pr-1 text-2xs text-text-muted font-mono"
              >
                ${data.levelPrices[li]!.toFixed(0)}
              </div>
              {/* Time bucket cells */}
              {Array.from({ length: data.numBuckets }, (_, bi) => {
                const count = cellMap.get(`${li}:${bi}`) ?? 0;
                return (
                  <div
                    key={`${li}-${bi}`}
                    className="rounded-sm cursor-crosshair transition-colors"
                    style={{ backgroundColor: cellColor(count, data.maxCount) }}
                    onMouseEnter={(e) => {
                      const rect = e.currentTarget.getBoundingClientRect();
                      setTooltip({
                        x: rect.left + rect.width / 2,
                        y: rect.top - 4,
                        text: `$${data.levelPrices[li]!.toFixed(2)} | ${data.bucketLabels[bi]} | ${count} fill${count !== 1 ? 's' : ''}`,
                      });
                    }}
                  />
                );
              })}
            </>
          ))}

          {/* Bottom time axis (abbreviated) */}
          <div /> {/* empty cell for label column */}
          {data.bucketLabels.map((label, i) =>
            i % Math.max(1, Math.floor(data.numBuckets / 6)) === 0 ? (
              <div
                key={`t-${i}`}
                className="text-2xs text-text-muted truncate"
                style={{ gridColumn: `${i + 2}` }}
              >
                {label}
              </div>
            ) : null
          )}
        </div>
      </div>

      {/* Tooltip */}
      {tooltip && (
        <div
          className="fixed z-50 px-2 py-1 rounded bg-bg-elevated border border-border-subtle text-2xs text-text-secondary shadow-lg pointer-events-none whitespace-nowrap"
          style={{
            left: tooltip.x,
            top: tooltip.y,
            transform: 'translate(-50%, -100%)',
          }}
        >
          <Mono>{tooltip.text}</Mono>
        </div>
      )}
    </div>
  );
}
