import { AlertCircle, Circle, Pause, Square } from 'lucide-react';
import { cn } from '@/lib/cn';
import type { BotStatus } from '@/lib/api-types';

// G2 — state clarity. Each state has a fixed visual signature (background,
// text color, icon, optional pulse) that NEVER varies, anywhere in the app.
// See docs/design/DESIGN_LANGUAGE.md §5.

interface StatusPillProps {
  status: BotStatus;
  className?: string;
}

const STATE_STYLES: Record<
  BotStatus,
  { bg: string; text: string; label: string }
> = {
  running: {
    bg: 'bg-success-soft',
    text: 'text-success',
    label: 'running',
  },
  paused: {
    bg: 'bg-warning-soft',
    text: 'text-warning',
    label: 'paused',
  },
  stopped: {
    bg: 'bg-bg-muted',
    text: 'text-text-muted',
    label: 'stopped',
  },
  error: {
    bg: 'bg-danger-soft',
    text: 'text-danger',
    label: 'error',
  },
};

export function StatusPill({ status, className }: StatusPillProps) {
  const styles = STATE_STYLES[status];

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5',
        'text-2xs font-semibold uppercase tracking-wider',
        styles.bg,
        styles.text,
        className
      )}
      role="status"
      aria-label={`Bot status: ${styles.label}`}
    >
      <StatusIcon status={status} />
      {styles.label}
    </span>
  );
}

function StatusIcon({ status }: { status: BotStatus }) {
  switch (status) {
    case 'running':
      return (
        <Circle
          className="size-2 fill-current pulse-slow"
          aria-hidden="true"
        />
      );
    case 'paused':
      return <Pause className="size-3" aria-hidden="true" />;
    case 'stopped':
      return <Square className="size-3" aria-hidden="true" />;
    case 'error':
      return (
        <AlertCircle className="size-3 pulse-fast" aria-hidden="true" />
      );
  }
}
