import { Activity, Moon, Sun } from 'lucide-react';
import { cn } from '@/lib/cn';
import { useWsStatus } from '@/lib/use-ws-channel';
import type { WsStatus } from '@/lib/ws-client';
import { applyThemeToDocument, useUiStore } from '@/stores/ui-store';
import { useEffect } from 'react';
import { LanguageToggle, useT } from '@/i18n';

const STATUS_KEY: Record<WsStatus, { color: string; key: string }> = {
  open: { color: 'text-success', key: 'wsLive' },
  connecting: { color: 'text-warning', key: 'wsConnecting' },
  closed: { color: 'text-text-muted', key: 'wsOffline' },
  error: { color: 'text-danger', key: 'wsError' },
};

const WS_LABELS_FALLBACK: Record<string, string> = {
  wsLive: 'Live',
  wsConnecting: 'Connecting',
  wsOffline: 'Offline',
  wsError: 'Error',
};

export function Header() {
  const t = useT();
  const status = useWsStatus();
  const styles = STATUS_KEY[status];
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);

  // Re-apply on every theme change so the document attribute stays in sync.
  useEffect(() => {
    applyThemeToDocument(theme);
  }, [theme]);

  return (
    <header
      className={cn(
        'flex items-center gap-4',
        'h-14 shrink-0 px-4 md:px-6',
        'bg-bg-surface border-b border-border-subtle'
      )}
    >
      <div className="flex items-center gap-2">
        <Activity className="size-5 text-primary" aria-hidden="true" />
        <span className="font-semibold tracking-tight text-text-primary">
          {t('header.brand').toUpperCase()}
        </span>
      </div>

      <div className="flex-1" />

      <div
        className={cn(
          'flex items-center gap-1.5 text-xs font-medium',
          styles.color
        )}
        title={`WebSocket: ${status}`}
        role="status"
        aria-live="polite"
      >
        <span
          className={cn(
            'inline-block size-2 rounded-full bg-current',
            status === 'open' && 'pulse-slow'
          )}
          aria-hidden="true"
        />
        {WS_LABELS_FALLBACK[styles.key]}
      </div>

      <LanguageToggle variant="compact" className="ml-2" />

      <button
        type="button"
        onClick={toggleTheme}
        aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
        className="ml-1 size-8 rounded-md flex items-center justify-center text-text-secondary hover:text-text-primary hover:bg-bg-muted transition-colors"
      >
        {theme === 'dark' ? (
          <Sun className="size-4" aria-hidden="true" />
        ) : (
          <Moon className="size-4" aria-hidden="true" />
        )}
      </button>
    </header>
  );
}
