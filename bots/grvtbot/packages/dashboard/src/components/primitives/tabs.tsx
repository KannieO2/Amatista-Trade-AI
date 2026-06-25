// Minimal tab strip — single source of truth via the controlled `value` prop.
// Renders only the active panel to avoid wasted DOM + queries.

import { cn } from '@/lib/cn';
import type { ReactNode } from 'react';

export interface TabItem {
  value: string;
  label: string;
  badge?: number | string;
}

interface TabsProps {
  items: TabItem[];
  value: string;
  onChange: (value: string) => void;
  children: ReactNode;
}

export function Tabs({ items, value, onChange, children }: TabsProps) {
  return (
    <div className="flex flex-col">
      <div
        role="tablist"
        className="flex gap-1 border-b border-border-subtle"
      >
        {items.map((item) => {
          const active = item.value === value;
          return (
            <button
              key={item.value}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => onChange(item.value)}
              className={cn(
                'px-4 py-2 text-xs font-semibold uppercase tracking-wider',
                'border-b-2 -mb-px transition-colors',
                active
                  ? 'border-primary text-primary'
                  : 'border-transparent text-text-muted hover:text-text-secondary'
              )}
            >
              {item.label}
              {item.badge != null && (
                <span className="ml-2 text-2xs text-text-muted">
                  {item.badge}
                </span>
              )}
            </button>
          );
        })}
      </div>
      <div className="mt-4">{children}</div>
    </div>
  );
}
