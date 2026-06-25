import { cn } from '@/lib/cn';
import type { HTMLAttributes } from 'react';

// Flat container. Background `bg-elevated`, subtle border, no shadow by default.
// Elevation here is meaning, not decoration — only use shadow on overlays.
export function Card({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'bg-bg-elevated border border-border-subtle rounded-lg p-5',
        'transition-colors duration-150',
        className
      )}
      {...rest}
    />
  );
}
