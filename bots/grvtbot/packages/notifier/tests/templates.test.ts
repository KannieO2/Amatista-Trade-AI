// D.7 — Notifier template tests.
// Pure-function tests: feed in synthetic rows, assert the rendered
// Markdown contains the expected substrings + signs. The templates'
// internal fmtUsd/fmtPnl/fmtPct helpers aren't exported, so we test
// them indirectly via the public templates.

import { describe, it, expect } from 'vitest';
import {
  fillsTemplate,
  drawdownTemplate,
  statusChangeTemplate,
  liqProximityTemplate,
  dailySummaryTemplate,
} from '../src/templates';
import type { BotRow, DailySnapshotRow, RoundtripRow } from '../src/db';

function rt(overrides: Partial<RoundtripRow> = {}): RoundtripRow {
  return {
    id: 1,
    buy_price: 2000,
    sell_price: 2010,
    size: 0.05,
    profit: 0.5,
    created_at: '2026-05-08T12:00:00Z',
    ...overrides,
  };
}

function bot(overrides: Partial<BotRow> = {}): BotRow {
  return {
    id: 42,
    pair: 'ETH_USDT_Perp',
    status: 'running',
    direction: 'long',
    leverage: 5,
    investment_usdt: 1000,
    total_pnl_usdt: 100,
    grid_profit_usdt: 80,
    trend_pnl_usdt: 20,
    avg_entry_price: 2000,
    liquidation_price: 1700,
    ...overrides,
  };
}

describe('fillsTemplate', () => {
  it('returns empty string when no roundtrips', () => {
    expect(fillsTemplate([])).toBe('');
  });

  it('renders singular wording for exactly 1 round-trip', () => {
    const out = fillsTemplate([rt({ profit: 1.5 })]);
    expect(out).toContain('1 new round-trip*'); // not "round-trips*"
    expect(out).toContain('+$1.50');
  });

  it('uses plural wording for 2+ round-trips', () => {
    const out = fillsTemplate([rt(), rt(), rt()]);
    expect(out).toContain('3 new round-trips*');
  });

  it('shows the running total of profits at the top', () => {
    const out = fillsTemplate([
      rt({ profit: 2.5 }),
      rt({ profit: 1.5 }),
      rt({ profit: -0.5 }),
    ]);
    // total = 3.5 → "+$3.50"
    expect(out).toContain('total +$3.50');
  });

  it('caps individual rows at 10 and shows the overflow count', () => {
    const many = Array.from({ length: 15 }, () => rt());
    const out = fillsTemplate(many);
    expect(out).toContain('…+5 more');
    // Should have 15 in the header but only 10 detail rows.
    expect(out.split('\n').filter((l) => l.startsWith('✅')).length).toBe(10);
  });

  it('uses ✅ for positive profit and ❌ for negative', () => {
    const out = fillsTemplate([
      rt({ profit: 1.0 }),
      rt({ profit: -1.0 }),
    ]);
    expect(out).toContain('✅');
    expect(out).toContain('❌');
  });
});

describe('drawdownTemplate', () => {
  it('renders equity / HWM / drop / threshold', () => {
    const out = drawdownTemplate(8000, 10000, 15);
    expect(out).toContain('Drawdown alert');
    expect(out).toContain('$8000.00'); // current equity
    expect(out).toContain('$10000.00'); // HWM
    // fmtPnl puts the negative inside the $ string ($-2000.00) — minor
    // quirk of the helper, but we're testing actual output.
    expect(out).toContain('$-2000.00'); // drop
    expect(out).toContain('-20.00%');   // dropPct
    expect(out).toContain('Threshold: 15%');
  });
});

describe('statusChangeTemplate', () => {
  it('uses ▶️ for running', () => {
    const out = statusChangeTemplate(bot(), 'paused', 'running');
    expect(out).toContain('▶️');
    expect(out).toContain('RUNNING');
    expect(out).toContain('Bot 42');
    expect(out).toContain('ETH_USDT_Perp');
  });

  it('uses ⏸ for paused, ⏹ for stopped, 🔴 for error', () => {
    expect(statusChangeTemplate(bot(), 'running', 'paused')).toContain('⏸');
    expect(statusChangeTemplate(bot(), 'running', 'stopped')).toContain('⏹');
    expect(statusChangeTemplate(bot(), 'running', 'error')).toContain('🔴');
  });

  it('appends last_error when present', () => {
    const out = statusChangeTemplate(
      bot({ last_error: 'GRVT timeout' }),
      'running',
      'error'
    );
    expect(out).toContain('GRVT timeout');
  });

  it('does not append an empty Error: line when last_error is null', () => {
    const out = statusChangeTemplate(bot({ last_error: null }), 'running', 'paused');
    expect(out).not.toContain('Error:');
  });
});

describe('liqProximityTemplate', () => {
  it('escalates to 🚨🚨 + CRITICAL when distance < 5%', () => {
    const out = liqProximityTemplate(bot(), 1750, 1700, 2.5);
    expect(out).toContain('🚨🚨');
    expect(out).toContain('CRITICAL');
    expect(out).toContain('2.5%');
  });

  it('uses ⚠️ + soft language when distance >= 5%', () => {
    const out = liqProximityTemplate(bot(), 1900, 1700, 10);
    expect(out).toContain('⚠️');
    expect(out).not.toContain('CRITICAL');
    expect(out).toContain('Monitor closely');
  });
});

function snap(overrides: Partial<DailySnapshotRow> = {}): DailySnapshotRow {
  return {
    id: 1,
    bot_id: 42,
    date: '2026-05-08',
    equity: 1100,
    grid_profit_net: 80,
    trend_pnl: 20,
    total_pnl: 100,
    round_trips: 12,
    ...overrides,
  };
}

describe('dailySummaryTemplate', () => {
  it('computes equity = investment + total_pnl and percent vs investment', () => {
    const out = dailySummaryTemplate(bot(), snap(), null);
    // 1000 + 100 = 1100; pct = 100 / 1000 = +10.00%
    expect(out).toContain('$1100.00');
    expect(out).toContain('+10.00%');
    expect(out).toContain('Round-trips today: 12');
  });

  it('renders day delta when yesterdayEquity is provided', () => {
    // Today equity 1100, yesterday 1050 → +$50.00 (+4.76%)
    const out = dailySummaryTemplate(bot(), snap(), 1050);
    expect(out).toContain('+$50.00');
    expect(out).toContain('+4.76%');
  });

  it('falls back to "—" for round-trips when snapshot is missing', () => {
    const out = dailySummaryTemplate(bot(), undefined, null);
    expect(out).toContain('Round-trips today: —');
  });

  it('shows status in lowercase', () => {
    const out = dailySummaryTemplate(bot({ status: 'paused' }), snap(), null);
    expect(out).toContain('Status: `paused`');
  });
});
