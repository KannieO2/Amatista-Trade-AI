// Form input + label + helper/error text. Used in the Create Wizard.

import { cn } from '@/lib/cn';
import { forwardRef, type InputHTMLAttributes } from 'react';

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  helper?: string;
  error?: string;
  // Whether the value is numeric — switches to mono right-aligned by default.
  numeric?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, helper, error, numeric, className, id, ...rest },
  ref
) {
  const inputId = id ?? rest.name ?? `input-${Math.random().toString(36).slice(2, 8)}`;

  return (
    <div className="flex flex-col gap-1.5 w-full">
      {label && (
        <label
          htmlFor={inputId}
          className="text-2xs font-semibold uppercase tracking-wider text-text-muted"
        >
          {label}
        </label>
      )}
      <input
        ref={ref}
        id={inputId}
        className={cn(
          'h-10 px-3 rounded-md',
          'bg-bg-surface border text-sm',
          'text-text-primary placeholder:text-text-disabled',
          'transition-colors',
          numeric && 'font-mono tabular-nums text-right',
          error
            ? 'border-danger focus-visible:border-danger'
            : 'border-border-subtle focus-visible:border-primary',
          className
        )}
        aria-invalid={!!error || undefined}
        aria-describedby={
          error ? `${inputId}-err` : helper ? `${inputId}-help` : undefined
        }
        {...rest}
      />
      {error && (
        <span id={`${inputId}-err`} className="text-2xs text-danger">
          {error}
        </span>
      )}
      {!error && helper && (
        <span id={`${inputId}-help`} className="text-2xs text-text-muted">
          {helper}
        </span>
      )}
    </div>
  );
});
