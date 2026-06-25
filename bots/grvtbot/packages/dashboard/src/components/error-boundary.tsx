// Last-line-of-defense error boundary. Wraps the route Suspense so any
// uncaught render error shows a styled fallback instead of a blank page.
//
// Class component because React still requires that for componentDidCatch.

import { AlertTriangle, RotateCcw } from 'lucide-react';
import { Component, type ErrorInfo, type ReactNode } from 'react';
import { useT } from '@/i18n';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

function ErrorFallback({
  error,
  onReset,
}: {
  error: Error;
  onReset: () => void;
}) {
  const t = useT();
  return (
    <div className="flex items-center justify-center min-h-[60vh] p-6">
      <div className="max-w-lg w-full bg-bg-elevated border border-danger/40 rounded-lg p-6">
        <div className="flex items-start gap-3 mb-4">
          <AlertTriangle className="size-6 text-danger shrink-0 mt-0.5" aria-hidden="true" />
          <div>
            <h2 className="text-lg font-semibold text-danger">
              {t('errorBoundary.title')}
            </h2>
            <p className="text-xs text-text-muted mt-1">
              {t('errorBoundary.body')}
            </p>
          </div>
        </div>
        <div className="bg-bg-base border border-border-subtle rounded p-3 mb-4">
          <code className="text-2xs text-text-secondary font-mono break-all">
            {error.message}
          </code>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => {
              onReset();
              window.location.reload();
            }}
            className="inline-flex items-center gap-2 h-9 px-4 rounded-md bg-primary text-bg-base text-sm font-medium hover:bg-primary-strong transition-colors"
          >
            <RotateCcw className="size-4" />
            {t('errorBoundary.reload')}
          </button>
          <button
            type="button"
            onClick={() => {
              onReset();
              window.history.back();
            }}
            className="inline-flex items-center gap-2 h-9 px-4 rounded-md text-text-secondary hover:bg-bg-muted hover:text-text-primary text-sm font-medium transition-colors"
          >
            {t('errorBoundary.goBack')}
          </button>
        </div>
        <p className="text-2xs text-text-muted mt-4">
          {t('errorBoundary.stackTrace')}
        </p>
      </div>
    </div>
  );
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('[ErrorBoundary]', error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <ErrorFallback
          error={this.state.error}
          onReset={() => this.setState({ error: null })}
        />
      );
    }
    return this.props.children;
  }
}
