import { ArrowDown, ArrowUp, Minus } from 'lucide-react';
import { cn } from '@/lib/cn';
import { Mono } from './mono';

interface DeltaProps {
  // The numeric value (positive = up, negative = down).
  value: number | null | undefined;
  // The pre-formatted text to render (e.g. "+9.15%" or "+$424.39").
  // Caller is responsible for formatting; Delta only paints + arrows.
  format: (v: number) => string;
  className?: string;
}

// G4 — color is never the only signal. Always pairs ▲/▼ icon with color.
export function Delta({ value, format, className }: DeltaProps) {
  if (value == null || Number.isNaN(value)) {
    return (
      <span className={cn('inline-flex items-center gap-1 text-text-muted', className)}>
        <Minus className="size-3" />
        <Mono>—</Mono>
      </span>
    );
  }

  const isUp = value > 0;
  const isDown = value < 0;
  const tone = isUp
    ? 'text-success'
    : isDown
      ? 'text-danger'
      : 'text-text-muted';
  const Icon = isUp ? ArrowUp : isDown ? ArrowDown : Minus;

  return (
    <span className={cn('inline-flex items-center gap-1', tone, className)}>
      <Icon className="size-3" aria-hidden="true" />
      <Mono>{format(value)}</Mono>
    </span>
  );
}
