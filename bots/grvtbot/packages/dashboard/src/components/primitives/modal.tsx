// Modal — built on the native <dialog> element so we get keyboard trap,
// Escape-to-close, and ARIA roles for free. Backdrop styled via ::backdrop.
//
// Used by the CreateBotWizard and any future destructive-confirm dialog.

import { useEffect, useId, useRef, type ReactNode } from 'react';
import { X } from 'lucide-react';
import { cn } from '@/lib/cn';

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  // 'wide' for wizards, 'regular' for confirms.
  size?: 'regular' | 'wide';
  children: ReactNode;
  footer?: ReactNode;
}

const SIZE_CLASS: Record<'regular' | 'wide', string> = {
  regular: 'max-w-[560px]',
  wide: 'max-w-[720px]',
};

export function Modal({
  open,
  onClose,
  title,
  description,
  size = 'regular',
  children,
  footer,
}: ModalProps) {
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const titleId = useId();
  const descId = useId();

  // Sync open prop with the imperative dialog API.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  // Close on backdrop click (clicking the dialog element directly = backdrop).
  function handleBackdropClick(e: React.MouseEvent<HTMLDialogElement>) {
    if (e.target === dialogRef.current) onClose();
  }

  return (
    <dialog
      ref={dialogRef}
      onClose={onClose}
      onClick={handleBackdropClick}
      className={cn(
        'p-0 overflow-hidden',
        'bg-bg-elevated text-text-primary border border-border-default shadow-lg',
        'backdrop:bg-black/70',
        'open:flex open:flex-col',
        // Mobile: full-width bottom sheet docked at the bottom of the viewport.
        // Desktop: centered modal with size cap.
        'w-full max-h-[92dvh] md:max-h-[85dvh]',
        'rounded-t-lg md:rounded-lg',
        'fixed bottom-0 left-0 right-0 m-0 md:static md:m-auto',
        SIZE_CLASS[size]
      )}
      aria-modal="true"
      role="dialog"
      aria-labelledby={titleId}
      aria-describedby={description ? descId : undefined}
    >
      <header className="flex items-start justify-between px-6 py-4 border-b border-border-subtle">
        <div>
          <h2 id={titleId} className="text-lg font-semibold text-text-primary">
            {title}
          </h2>
          {description && (
            <p id={descId} className="text-xs text-text-muted mt-0.5">
              {description}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close dialog"
          className="text-text-muted hover:text-text-primary -mr-2 -mt-1 p-1 rounded transition-colors"
        >
          <X className="size-4" />
        </button>
      </header>

      <div className="flex-1 overflow-y-auto px-6 py-5">{children}</div>

      {footer && (
        <footer className="border-t border-border-subtle px-6 py-3 flex items-center justify-end gap-3">
          {footer}
        </footer>
      )}
    </dialog>
  );
}
