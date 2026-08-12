import { lazy, Suspense, useEffect } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Toaster, toast } from 'sonner';
import { AuthProvider } from './lib/auth-context';
import { LangProvider } from './i18n';
import { AppShell } from './components/layout/app-shell';
import { ProtectedRoute } from './components/protected-route';
import { ErrorBoundary } from './components/error-boundary';
import { ConfirmHost } from './components/primitives/confirm-dialog';
import { OverviewPage } from './pages/overview';
import { BotsListPage } from './pages/bots-list';
import { SettingsPage } from './pages/settings';
import { LoginPage } from './pages/login';
import { SignupPage } from './pages/signup';
import { ForgotPasswordPage } from './pages/forgot-password';
import { ResetPasswordPage } from './pages/reset-password';
import { GrvtOnboardingPage } from './pages/onboarding/grvt';

// Bot Detail owns the heaviest dependencies (lightweight-charts + recharts).
// Lazy-load it so the Overview page doesn't pay the cost on first paint.
const BotDetailPage = lazy(() =>
  import('./pages/bot-detail').then((m) => ({ default: m.BotDetailPage }))
);

// Backtest is form-heavy + recharts — also lazy.
const BacktestPage = lazy(() =>
  import('./pages/backtest').then((m) => ({ default: m.BacktestPage }))
);

function RouteFallback() {
  return (
    <div className="flex items-center justify-center h-64 text-sm text-text-muted animate-pulse">
      Loading...
    </div>
  );
}

// E.7: global error handler — surfaces network failures as a toast
// so the user knows something is wrong instead of staring at stale data.
// Individual mutation onError handlers still fire for specific error messages;
// this catches the background query refetch failures that would otherwise
// be silent.
let lastNetworkToastAt = 0;
const NETWORK_TOAST_COOLDOWN = 10_000; // don't spam

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 2_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
    mutations: {
      onError: (err) => {
        // Mutations already show their own toast in onError handlers,
        // but this catches any that forget to.
        const msg = err instanceof Error ? err.message : String(err);
        if (msg.includes('network error') && Date.now() - lastNetworkToastAt > NETWORK_TOAST_COOLDOWN) {
          lastNetworkToastAt = Date.now();
          toast.error('Network error — check your connection', { id: 'network-error' });
        }
      },
    },
  },
});

export default function App() {
  // E.7: show a persistent toast when the browser goes offline,
  // dismiss when back online. Simple and covers WiFi drops, VPN
  // disconnects, etc.
  useEffect(() => {
    const onOffline = () => toast.error('You are offline', { id: 'offline', duration: Infinity });
    const onOnline = () => {
      toast.dismiss('offline');
      toast.success('Back online', { id: 'online', duration: 3000 });
    };
    window.addEventListener('offline', onOffline);
    window.addEventListener('online', onOnline);
    return () => {
      window.removeEventListener('offline', onOffline);
      window.removeEventListener('online', onOnline);
    };
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      <LangProvider>
        <AuthProvider>
          <BrowserRouter basename={import.meta.env.BASE_URL.replace(/\/$/, '')}>
          <Routes>
            {/* Public auth routes — no AppShell, no ProtectedRoute */}
            <Route path="login" element={<LoginPage />} />
            <Route path="signup" element={<SignupPage />} />
            <Route path="forgot-password" element={<ForgotPasswordPage />} />
            <Route path="reset-password" element={<ResetPasswordPage />} />
            <Route
              path="onboarding/grvt"
              element={
                // Require login but NOT grvt creds (that's what this
                // page sets up). No AppShell — standalone full-page form.
                <ProtectedRoute requireGrvt={false}>
                  <GrvtOnboardingPage />
                </ProtectedRoute>
              }
            />

            {/* Protected dashboard routes — wrapped in AppShell */}
            <Route
              element={
                <ProtectedRoute>
                  <AppShell />
                </ProtectedRoute>
              }
            >
              <Route
                index
                element={
                  <ErrorBoundary>
                    <OverviewPage />
                  </ErrorBoundary>
                }
              />
              <Route
                path="bots"
                element={
                  <ErrorBoundary>
                    <BotsListPage />
                  </ErrorBoundary>
                }
              />
              <Route
                path="bots/:id"
                element={
                  <ErrorBoundary>
                    <Suspense fallback={<RouteFallback />}>
                      <BotDetailPage />
                    </Suspense>
                  </ErrorBoundary>
                }
              />
              <Route
                path="backtest"
                element={
                  <ErrorBoundary>
                    <Suspense fallback={<RouteFallback />}>
                      <BacktestPage />
                    </Suspense>
                  </ErrorBoundary>
                }
              />
              <Route
                path="settings"
                element={
                  <ErrorBoundary>
                    <SettingsPage />
                  </ErrorBoundary>
                }
              />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
        <Toaster
          theme="dark"
          position="bottom-right"
          toastOptions={{
            style: {
              background: 'var(--color-bg-elevated)',
              color: 'var(--color-text-primary)',
              border: '1px solid var(--color-border-default)',
            },
          }}
        />
          <ConfirmHost />
        </AuthProvider>
      </LangProvider>
    </QueryClientProvider>
  );
}
