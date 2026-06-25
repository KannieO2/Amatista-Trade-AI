// ConfirmDialog — styled replacement for window.confirm() that follows
// design doc §5 G3 (destructive emphasis + confirmation).
//
// Three variants:
//   - 'default'    : neutral, primary CTA   (e.g. "save changes")
//   - 'warning'    : amber, primary CTA     (e.g. "leave without saving")
//   - 'destructive': red, danger CTA + always shows the consequence prose
//                    in a danger-tinted box
//
// Usage is imperative-friendly via the `useConfirmDialog()` hook (renders
// a singleton at the bottom of the component tree). For one-off cases you
// can also drop the <ConfirmDialog> directly with controlled `open`.

import { AlertTriangle } from 'lucide-react';
import { type ReactNode, useCallback, useEffect, useState } from 'react';
import { Button } from './button';
import { Modal } from './modal';

export type ConfirmVariant = 'default' | 'warning' | 'destructive';

export interface ConfirmDialogOptions {
  title: string;
  description?: string;
  // Body content shown above the confirm/cancel buttons. Strings are
  // wrapped in a <p>; ReactNodes are rendered as-is for richer layouts
  // (e.g. a list of consequences).
  body?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: ConfirmVariant;
}

interface ConfirmDialogProps extends ConfirmDialogOptions {
  open: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  // While the action is in flight, lock both buttons + show "Working…"
  pending?: boolean;
}

export function ConfirmDialog({
  open,
  title,
  description,
  body,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'default',
  pending = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const isDestructive = variant === 'destructive';
  const isWarning = variant === 'warning';
  const showAlertBox = isDestructive || isWarning;

  return (
    <Modal
      open={open}
      onClose={pending ? () => undefined : onCancel}
      title={title}
      {...(description ? { description } : {})}
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={pending}>
            {cancelLabel}
          </Button>
          <Button
            variant={isDestructive ? 'danger' : 'primary'}
            onClick={onConfirm}
            disabled={pending}
            autoFocus={!isDestructive}
          >
            {pending ? 'Working…' : confirmLabel}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {showAlertBox && (
          <div
            className={
              isDestructive
                ? 'flex items-start gap-3 rounded-md border border-danger/40 bg-danger-soft/30 p-3'
                : 'flex items-start gap-3 rounded-md border border-warning/40 bg-warning-soft/30 p-3'
            }
            role="alert"
          >
            <AlertTriangle
              className={
                isDestructive
                  ? 'size-5 shrink-0 text-danger mt-0.5'
                  : 'size-5 shrink-0 text-warning mt-0.5'
              }
              aria-hidden="true"
            />
            <div className="text-xs text-text-secondary">
              {typeof body === 'string' || body == null ? (
                <p>{body ?? 'This action cannot be undone.'}</p>
              ) : (
                body
              )}
            </div>
          </div>
        )}
        {!showAlertBox && body && (
          <div className="text-sm text-text-secondary">
            {typeof body === 'string' ? <p>{body}</p> : body}
          </div>
        )}
      </div>
    </Modal>
  );
}

// ── Hook: imperative confirm() with awaitable promise ────────────────
//
// Usage:
//   const confirm = useConfirm();
//   ...
//   const ok = await confirm({
//     variant: 'destructive',
//     title: 'Pause bot 42?',
//     body: <>This will <strong>cancel 93 open orders</strong> on GRVT.</>,
//     confirmLabel: 'Pause and cancel orders',
//   });
//   if (ok) pauseMutation.mutate();
//
// Render the host once near the app root (or per page) via:
//   <ConfirmHost />
//
// The hook + host share state through a module-level event bus so any
// component can call confirm() without a context provider.

type Resolver = (ok: boolean) => void;
let pendingResolver: Resolver | null = null;
let pendingOptions: ConfirmDialogOptions | null = null;
const listeners = new Set<() => void>();

function notify() {
  for (const fn of listeners) fn();
}

export function useConfirm() {
  return useCallback((options: ConfirmDialogOptions): Promise<boolean> => {
    return new Promise((resolve) => {
      pendingOptions = options;
      pendingResolver = resolve;
      notify();
    });
  }, []);
}

export function ConfirmHost() {
  // tick is just a re-render trigger; the actual state lives in the
  // module-level pendingOptions/pendingResolver.
  const [, setTick] = useState(0);

  // Subscribe to the bus on mount, unsubscribe on unmount.
  // (The previous version put this inside useState's lazy initializer
  // which only runs once per state slot, doesn't expose a cleanup, and
  // returned a function that became the initial state instead of being
  // run as cleanup. That left a stale listener in the global Set which
  // mostly worked but doubled up under StrictMode.)
  useEffect(() => {
    const fn = () => setTick((t) => t + 1);
    listeners.add(fn);
    return () => {
      listeners.delete(fn);
    };
  }, []);

  const open = pendingOptions != null;
  const opts = pendingOptions;

  function close(result: boolean) {
    const resolve = pendingResolver;
    pendingResolver = null;
    pendingOptions = null;
    notify();
    resolve?.(result);
  }

  if (!open || !opts) {
    // Render nothing when there's no pending confirm. Returning the
    // ConfirmDialog with open=false works too but adds an extra dialog
    // element to the DOM unnecessarily.
    return null;
  }

  return (
    <ConfirmDialog
      open={open}
      title={opts.title}
      {...(opts.description ? { description: opts.description } : {})}
      {...(opts.body !== undefined ? { body: opts.body } : {})}
      {...(opts.confirmLabel ? { confirmLabel: opts.confirmLabel } : {})}
      {...(opts.cancelLabel ? { cancelLabel: opts.cancelLabel } : {})}
      {...(opts.variant ? { variant: opts.variant } : {})}
      onConfirm={() => close(true)}
      onCancel={() => close(false)}
    />
  );
}
