// D.8 — Chart component tests.
// Recharts renders SVG paths that aren't worth asserting; instead we
// test the surrounding logic: aria-labels, empty states, and that the
// data shape is accepted without throwing.

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Sparkline } from '@/components/charts/sparkline';
import { EquityCurve } from '@/components/charts/equity-curve';

describe('Sparkline', () => {
  it('renders a fallback "—" when there are fewer than 2 points', () => {
    const { container } = render(<Sparkline data={[]} />);
    expect(container.textContent).toBe('—');
  });

  it('renders a single-point dataset as the fallback (need >= 2 for a trend)', () => {
    const { container } = render(<Sparkline data={[{ value: 100 }]} />);
    expect(container.textContent).toBe('—');
  });

  it('renders an up trend with aria-label containing "up" and the percent change', () => {
    render(<Sparkline data={[{ value: 100 }, { value: 110 }, { value: 120 }]} />);
    const chart = screen.getByRole('img');
    const label = chart.getAttribute('aria-label')!;
    expect(label).toContain('up');
    expect(label).toContain('20.0%'); // (120-100)/100*100
    expect(label).toContain('3 snapshots');
  });

  it('renders a down trend with aria-label containing "down"', () => {
    render(<Sparkline data={[{ value: 100 }, { value: 80 }]} />);
    const label = screen.getByRole('img').getAttribute('aria-label')!;
    expect(label).toContain('down');
    expect(label).toContain('20.0%'); // (100-80)/100*100 absolute
  });

  it('treats equal endpoints as "up" (>=) — flat = no loss signal', () => {
    render(<Sparkline data={[{ value: 100 }, { value: 100 }]} />);
    expect(screen.getByRole('img').getAttribute('aria-label')).toContain('up');
  });

  it('honors the height prop on its wrapper', () => {
    const { container } = render(
      <Sparkline data={[{ value: 1 }, { value: 2 }]} height={80} />
    );
    const wrapper = container.querySelector('[role="img"]') as HTMLElement;
    expect(wrapper.style.height).toBe('80px');
  });
});

describe('EquityCurve', () => {
  it('renders the empty state when no input given', () => {
    const { container } = render(<EquityCurve />);
    expect(container.textContent).toContain('No snapshot history yet');
  });

  it('renders the empty state when snapshots is an empty array', () => {
    const { container } = render(<EquityCurve snapshots={[]} />);
    expect(container.textContent).toContain('No snapshot history yet');
  });

  it('renders the empty state when points is an empty array', () => {
    const { container } = render(<EquityCurve points={[]} />);
    expect(container.textContent).toContain('No snapshot history yet');
  });

  it('accepts the points input shape and emits an aria-labeled chart', () => {
    render(
      <EquityCurve
        points={[
          { date: '2026-05-01', equity: 1000 },
          { date: '2026-05-02', equity: 1100 },
          { date: '2026-05-03', equity: 1200 },
        ]}
      />
    );
    const label = screen.getByRole('img').getAttribute('aria-label')!;
    expect(label).toContain('Equity curve');
    expect(label).toContain('3 daily snapshots');
    expect(label).toContain('up');
    // (1200-1000)/1000*100 = 20%
    expect(label).toContain('20.0%');
  });

  it('accepts the legacy snapshots input and reverses newest-first → chronological', () => {
    // Caller passes newest-first (the /bots/:id/snapshots endpoint default).
    // The component should reverse and compute up/down from chronological order.
    render(
      <EquityCurve
        snapshots={[
          {
            id: 3, bot_id: 1, date: '2026-05-03',
            equity_usdt: 1200, realized_pnl_usdt: 0, unrealized_pnl_usdt: 0,
            num_round_trips: 0, total_fees_usdt: 0, funding_usdt: 0,
          },
          {
            id: 2, bot_id: 1, date: '2026-05-02',
            equity_usdt: 1100, realized_pnl_usdt: 0, unrealized_pnl_usdt: 0,
            num_round_trips: 0, total_fees_usdt: 0, funding_usdt: 0,
          },
          {
            id: 1, bot_id: 1, date: '2026-05-01',
            equity_usdt: 1000, realized_pnl_usdt: 0, unrealized_pnl_usdt: 0,
            num_round_trips: 0, total_fees_usdt: 0, funding_usdt: 0,
          },
        ]}
      />
    );
    const label = screen.getByRole('img').getAttribute('aria-label')!;
    expect(label).toContain('up');
    expect(label).toContain('20.0%');
  });

  it('honors a custom height prop', () => {
    const { container } = render(
      <EquityCurve points={[{ date: 'a', equity: 1 }, { date: 'b', equity: 2 }]} height={300} />
    );
    const wrapper = container.querySelector('[role="img"]') as HTMLElement;
    expect(wrapper.style.height).toBe('300px');
  });
});
