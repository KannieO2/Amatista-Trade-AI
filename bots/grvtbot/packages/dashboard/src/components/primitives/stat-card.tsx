import { cn } from '@/lib/cn';
import type { ReactNode } from 'react';
import { Mono } from './mono';

interface StatCardProps {
  label: string;
  value: ReactNode;     // Pre-formatted string or a node (e.g. Mono-wrapped)
  delta?: ReactNode;    // Optional Delta component instance
  className?: string;
}

// §8.2 — used in BotDetail header strip and Overview top stat-strip.
// Container is `bg-elevated`. Borders only between cards (use a parent with
// `divide-x divide-border-subtle` rather than putting a border on each card).
export function StatCard({ label, value, delta, className }: StatCardProps) {
  return (
    <div
      className={cn(
        'flex flex-col gap-1 bg-bg-elevated p-4',
        'first:rounded-l-lg last:rounded-r-lg',
        className
      )}
    >
      <span className="text-2xs font-semibold uppercase tracking-wider text-text-muted">
        {label}
      </span>
      <span className="text-2xl font-semibold text-text-primary">
        <Mono>{value}</Mono>
      </span>
      {delta && <span className="text-sm">{delta}</span>}
    </div>
  );
}
