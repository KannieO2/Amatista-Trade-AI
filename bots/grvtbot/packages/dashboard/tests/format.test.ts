// D.8 — Pure formatter tests for lib/format.ts.
// No DOM, no React — just Intl.NumberFormat output checks. Locked-in
// so a "small" formatter change can't silently drift column widths.

import { describe, it, expect } from 'vitest';
import {
  formatUsd,
  formatUsdCompact,
  formatPercent,
  formatPnl,
  formatSize,
  formatTimeUtc,
} from '@/lib/format';

describe('formatUsd', () => {
  it('renders positive amounts with 2 decimal places', () => {
    expect(formatUsd(1234.5)).toBe('$1,234.50');
  });
  it('renders negative amounts with a leading minus', () => {
    expect(formatUsd(-12.34)).toBe('-$12.34');
  });
  it('renders zero as $0.00', () => {
    expect(formatUsd(0)).toBe('$0.00');
  });
  it('renders null and undefined as $—', () => {
    expect(formatUsd(null)).toBe('$—');
    expect(formatUsd(undefined)).toBe('$—');
  });
  it('renders NaN as $—', () => {
    expect(formatUsd(NaN)).toBe('$—');
  });
});

describe('formatUsdCompact', () => {
  it('drops trailing zeros up to 2 decimals', () => {
    expect(formatUsdCompact(1000)).toBe('$1,000');
  });
  it('keeps decimals when needed', () => {
    expect(formatUsdCompact(1234.56)).toBe('$1,234.56');
  });
});

describe('formatPercent', () => {
  it('prefixes "+" on positive values', () => {
    expect(formatPercent(5.25)).toBe('+5.25%');
  });
  it('renders negative values with the native minus', () => {
    expect(formatPercent(-3.5)).toBe('-3.50%');
  });
  it('returns "—" for null/NaN', () => {
    expect(formatPercent(null)).toBe('—');
    expect(formatPercent(NaN)).toBe('—');
  });
  it('does NOT prefix "+" on zero', () => {
    expect(formatPercent(0)).toBe('0.00%');
  });
});

describe('formatPnl', () => {
  it('always shows a sign — "+" for non-negative', () => {
    expect(formatPnl(100)).toBe('+$100.00');
    expect(formatPnl(0)).toBe('+$0.00');
  });
  it('uses the native minus for negative', () => {
    expect(formatPnl(-50.5)).toBe('-$50.50');
  });
  it('returns $— for null / NaN', () => {
    expect(formatPnl(null)).toBe('$—');
    expect(formatPnl(NaN)).toBe('$—');
  });
});

describe('formatSize', () => {
  it('renders 4 decimal places by default', () => {
    expect(formatSize(0.05)).toBe('0.0500');
  });
  it('renders "—" for null/NaN', () => {
    expect(formatSize(null)).toBe('—');
    expect(formatSize(NaN)).toBe('—');
  });
});

describe('formatTimeUtc', () => {
  it('formats unix-ms as HH:MM:SS UTC', () => {
    // 2026-05-08T14:30:45.000Z
    const ms = Date.UTC(2026, 4, 8, 14, 30, 45);
    expect(formatTimeUtc(ms)).toBe('14:30:45 UTC');
  });
  it('zero-pads single-digit components', () => {
    const ms = Date.UTC(2026, 0, 1, 1, 2, 3);
    expect(formatTimeUtc(ms)).toBe('01:02:03 UTC');
  });
  it('returns "—" for null/NaN', () => {
    expect(formatTimeUtc(null)).toBe('—');
    expect(formatTimeUtc(NaN)).toBe('—');
  });
});
