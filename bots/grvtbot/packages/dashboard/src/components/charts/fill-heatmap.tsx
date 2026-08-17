// E.6 — Grid activity heatmap.
// Shows fill density per grid level per time bucket. Each cell is colored
// by how many fills occurred at that price level in that time window.
// Built with pure CSS grid + inline background-color (no charting lib needed).

import { Fragment, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
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
    above: boolean;   // si no entra arriba, se dibuja debajo de la celda
    text: string;
  } | null>(null);

  if (!data || data.cells.length === 0) {
    return (
      <p className="text-xs text-text-muted py-4 text-center">
        No fill data for heatmap yet.
      </p>
    );
  }

  // Máximo 20 filas. Con más niveles se AGRUPAN de a `step`, no se saltean:
  // antes se mostraba uno de cada `step` y los fills de los niveles omitidos
  // no se sumaban a ninguna fila — simplemente desaparecían del mapa. Con 21
  // niveles (step=2) se perdía la mitad de la actividad, que es lo que hacía
  // ver el heatmap casi vacío.
  const step = data.numLevels > 20 ? Math.ceil(data.numLevels / 20) : 1;
  const visibleLevels: number[] = [];
  for (let i = 0; i < data.numLevels; i += step) visibleLevels.push(i);

  // count por (fila visible, bucket), agrupando los niveles que cubre la fila
  const cellMap = new Map<string, number>();
  for (const cell of data.cells) {
    // el nivel cae en la fila que empieza en el múltiplo de step anterior
    const row = Math.min(
      Math.floor(cell.levelIndex / step) * step,
      visibleLevels[visibleLevels.length - 1]!
    );
    const key = `${row}:${cell.bucketIndex}`;
    cellMap.set(key, (cellMap.get(key) ?? 0) + cell.count);
  }
  // el máximo tiene que recalcularse sobre los valores agrupados, si no las
  // celdas se pintan más tenues de lo que corresponde
  const maxCount = Math.max(1, ...cellMap.values());

  // rango de precios que cubre cada fila, para el tooltip
  const rowRange = (li: number): string => {
    const hasta = Math.min(li + step - 1, data.numLevels - 1);
    const dec = spacing < 1 ? 2 : 0;
    return hasta > li
      ? `$${data.levelPrices[li]!.toFixed(dec)}–$${data.levelPrices[hasta]!.toFixed(dec)}`
      : `$${data.levelPrices[li]!.toFixed(2)}`;
  };

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
          {/* Level rows. El Fragment necesita key: sin ella React no puede
              reconciliar las filas y las celdas se remontan al re-render,
              perdiendo el hover a mitad de movimiento. */}
          {visibleLevels.map((li) => (
            <Fragment key={`row-${li}`}>
              {/* Price label */}
              <div className="flex items-center justify-end pr-1 text-2xs text-text-muted font-mono">
                ${data.levelPrices[li]!.toFixed(spacing < 1 ? 1 : 0)}
              </div>
              {/* Time bucket cells */}
              {Array.from({ length: data.numBuckets }, (_, bi) => {
                const count = cellMap.get(`${li}:${bi}`) ?? 0;
                return (
                  <div
                    key={`${li}-${bi}`}
                    className={
                      'rounded-sm transition-colors ' +
                      (count > 0 ? 'cursor-crosshair' : 'cursor-default')
                    }
                    style={{ backgroundColor: cellColor(count, maxCount) }}
                    onMouseEnter={(e) => {
                      const rect = e.currentTarget.getBoundingClientRect();
                      // clamp: sin esto el tooltip se sale por arriba cuando la
                      // celda esta en las primeras filas (top - 4 queda negativo)
                      const arriba = rect.top > 44;
                      setTooltip({
                        x: Math.min(Math.max(rect.left + rect.width / 2, 80), window.innerWidth - 80),
                        y: arriba ? rect.top - 4 : rect.bottom + 4,
                        above: arriba,
                        text: `${rowRange(li)} · ${data.bucketLabels[bi]} · ${
                          count === 0 ? 'sin fills' : `${count} fill${count !== 1 ? 's' : ''}`
                        }`,
                      });
                    }}
                    onMouseLeave={() => setTooltip(null)}
                  />
                );
              })}
            </Fragment>
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

      {/* Tooltip — va por PORTAL a document.body a proposito.
          globals.css aplica `animation: riseIn ... both` a #main-content > *.
          El fill-mode `both` deja la animacion rellenando para siempre, y una
          animacion que toca `transform` crea un bloque contenedor: con el
          tooltip dentro del arbol, su `position: fixed` se resolvia contra la
          tarjeta en vez de contra la ventana y aparecia cientos de px mas
          abajo, encima de otras secciones. El portal lo saca de #main-content
          y las coordenadas de getBoundingClientRect vuelven a valer. */}
      {tooltip &&
        createPortal(
          <div
            className="fixed z-50 px-2 py-1 rounded bg-bg-elevated border border-border-subtle text-2xs text-text-secondary shadow-lg pointer-events-none whitespace-nowrap"
            style={{
              left: tooltip.x,
              top: tooltip.y,
              transform: tooltip.above ? 'translate(-50%, -100%)' : 'translate(-50%, 0)',
            }}
          >
            <Mono>{tooltip.text}</Mono>
          </div>,
          document.body
        )}
    </div>
  );
}
