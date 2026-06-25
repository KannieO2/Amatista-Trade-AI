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

// Desktop sidebar (hidden on mobile — see BottomNav).
// 224px fixed width per wireframe §7.1.
export function Sidebar() {
  const t = useT();
  return (
    <aside
      className={cn(
        'hidden md:flex flex-col',
        'w-56 shrink-0 bg-bg-surface border-r border-border-subtle',
        'pt-4 pb-4'
      )}
    >
      <nav aria-label="Main navigation" className="flex-1 px-2 flex flex-col gap-1">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              cn(
                'flex items-center gap-3 rounded-md px-3 h-9',
                'text-sm font-medium transition-colors',
                isActive
                  ? 'bg-primary-soft text-primary'
                  : 'text-text-secondary hover:bg-bg-muted hover:text-text-primary'
              )
            }
          >
            <item.icon className="size-4" aria-hidden="true" />
            {t(item.labelKey)}
          </NavLink>
        ))}
      </nav>
      <div className="px-3 pt-3 mt-2 border-t border-border-subtle">
        <span className="text-2xs uppercase tracking-wider text-text-disabled">
          v0.1.0 · B.5
        </span>
      </div>
    </aside>
  );
}
