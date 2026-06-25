import { NavLink } from 'react-router-dom';
import { FlaskConical, Hexagon, LayoutGrid, Settings } from 'lucide-react';
import { cn } from '@/lib/cn';
import type { LucideIcon } from 'lucide-react';
import { useT } from '@/i18n';

interface NavItem {
  to: string;
  labelKey: string;
  icon: LucideIcon;
  end?: boolean;
}

const NAV: NavItem[] = [
  { to: '/', labelKey: 'nav.overview', icon: LayoutGrid, end: true },
  { to: '/bots', labelKey: 'nav.bots', icon: Hexagon },
  { to: '/backtest', labelKey: 'nav.backtest', icon: FlaskConical },
  { to: '/settings', labelKey: 'nav.settings', icon: Settings },
];

// Mobile bottom nav (visible <md). 4 items, ≤5 limit per design doc §7.1.
// Touch targets are 56px tall to satisfy the 44pt minimum + safe area.
export function BottomNav() {
  const t = useT();
  return (
    <nav
      aria-label="Mobile navigation"
      className={cn(
        'md:hidden flex',
        'fixed bottom-0 inset-x-0 h-14 z-40',
        'bg-bg-surface border-t border-border-subtle',
        'pb-[env(safe-area-inset-bottom)]'
      )}
    >
      {NAV.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          className={({ isActive }) =>
            cn(
              'flex-1 flex flex-col items-center justify-center gap-0.5',
              'text-2xs font-medium',
              isActive ? 'text-primary' : 'text-text-muted'
            )
          }
        >
          <item.icon className="size-5" aria-hidden="true" />
          {t(item.labelKey)}
        </NavLink>
      ))}
    </nav>
  );
}
