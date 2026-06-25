// D.8 — Primitive component tests.
// Render with @testing-library/react in jsdom. Coverage is by-design
// shallow — these primitives are visual atoms, so tests focus on their
// contract (slots, props, accessibility) not internals.

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Mono } from '@/components/primitives/mono';
import { Delta } from '@/components/primitives/delta';
import { StatusPill } from '@/components/primitives/status-pill';
import { StatCard } from '@/components/primitives/stat-card';

describe('Mono', () => {
  it('renders children inside a span with the mono+tabular-nums classes', () => {
    const { container } = render(<Mono>12345</Mono>);
    const span = container.querySelector('span')!;
    expect(span.textContent).toBe('12345');
    expect(span.className).toContain('font-mono');
    expect(span.className).toContain('tabular-nums');
  });
  it('merges caller className with the defaults', () => {
    const { container } = render(<Mono className="extra-class">x</Mono>);
    expect(container.querySelector('span')!.className).toContain('extra-class');
  });
});

describe('Delta', () => {
  it('renders "—" with muted color when value is null', () => {
    render(<Delta value={null} format={(v) => `${v}`} />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
  it('renders "—" when value is NaN', () => {
    render(<Delta value={NaN} format={(v) => `${v}`} />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
  it('renders the formatted value with success color when positive', () => {
    const { container } = render(<Delta value={5.25} format={(v) => `+${v.toFixed(2)}%`} />);
    expect(screen.getByText('+5.25%')).toBeInTheDocument();
    expect(container.querySelector('span')!.className).toContain('text-success');
  });
  it('renders danger color when negative', () => {
    const { container } = render(<Delta value={-3.5} format={(v) => `${v.toFixed(1)}%`} />);
    expect(container.querySelector('span')!.className).toContain('text-danger');
  });
  it('renders muted color (and Minus icon) when exactly zero', () => {
    const { container } = render(<Delta value={0} format={(v) => `${v}`} />);
    expect(container.querySelector('span')!.className).toContain('text-text-muted');
  });
});

describe('StatusPill', () => {
  it('renders the running state with the correct aria-label', () => {
    render(<StatusPill status="running" />);
    const pill = screen.getByRole('status');
    expect(pill).toHaveAttribute('aria-label', 'Bot status: running');
    expect(pill).toHaveTextContent('running');
    expect(pill.className).toContain('text-success');
  });
  it('renders paused with warning color and "paused" label', () => {
    render(<StatusPill status="paused" />);
    const pill = screen.getByRole('status');
    expect(pill).toHaveTextContent('paused');
    expect(pill.className).toContain('text-warning');
  });
  it('renders stopped with muted color', () => {
    render(<StatusPill status="stopped" />);
    expect(screen.getByRole('status').className).toContain('text-text-muted');
  });
  it('renders error with danger color', () => {
    render(<StatusPill status="error" />);
    expect(screen.getByRole('status').className).toContain('text-danger');
  });
});

describe('StatCard', () => {
  it('renders the label and value', () => {
    render(<StatCard label="Total equity" value="$1,234.56" />);
    expect(screen.getByText('Total equity')).toBeInTheDocument();
    expect(screen.getByText('$1,234.56')).toBeInTheDocument();
  });
  it('renders an optional delta slot when provided', () => {
    render(<StatCard label="L" value="V" delta={<span>+5%</span>} />);
    expect(screen.getByText('+5%')).toBeInTheDocument();
  });
  it('omits delta wrapper when delta prop is not given', () => {
    const { container } = render(<StatCard label="L" value="V" />);
    // The delta slot is conditionally rendered (lines 30-32 of stat-card.tsx);
    // verify nothing extra rendered when omitted.
    expect(container.textContent).toBe('LV');
  });
});
