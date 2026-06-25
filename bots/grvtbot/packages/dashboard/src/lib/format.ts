// Number formatting helpers.
// All formatters return strings safe to render inside <span class="font-mono">,
// so column widths stay stable thanks to tabular figures (G1).

const usdFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const usdCompactFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

const percentFormatter = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const sizeFormatter = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 4,
  maximumFractionDigits: 4,
});

export function formatUsd(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '$—';
  return usdFormatter.format(value);
}

export function formatUsdCompact(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '$—';
  return usdCompactFormatter.format(value);
}

export function formatPercent(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—';
  const sign = value > 0 ? '+' : '';
  return `${sign}${percentFormatter.format(value)}%`;
}

export function formatSize(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—';
  return sizeFormatter.format(value);
}

export function formatPnl(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '$—';
  const sign = value >= 0 ? '+' : '';
  return `${sign}${usdFormatter.format(value)}`;
}

// Format a unix-ms timestamp as a short HH:MM:SS UTC tag.
export function formatTimeUtc(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return '—';
  const d = new Date(ms);
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  const ss = String(d.getUTCSeconds()).padStart(2, '0');
  return `${hh}:${mm}:${ss} UTC`;
}
