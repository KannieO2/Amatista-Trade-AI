import { cn } from '@/lib/cn';
import type { HTMLAttributes } from 'react';

// Trivial wrapper for tabular numbers. Use anywhere a digit can change at
// runtime — prices, equity, percentages — so column widths stay stable (G1).
export function Mono({ className, ...rest }: HTMLAttributes<HTMLSpanElement>) {
  return <span className={cn('font-mono tabular-nums', className)} {...rest} />;
}
