// Overview page — multi-bot dashboard with stat-strip + BotCard grid + create CTA.
//
// H.7: aggregates (equity, PnL, exposure, leverage, equity curve) come from
// the dedicated /portfolio-summary + /portfolio-equity-curve endpoints
// instead of being re-summed client-side. Single source of truth, and
// includes risk metrics the client previously didn't have access to.

import { useQuery } from '@tanstack/react-query';
import { lazy, Suspense, useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Plus } from 'lucide-react';
import { api } from '@/lib/api-client';
import type { WizardPreset } from '@/components/create-bot-wizard';
import { formatPercent, formatPnl, formatUsd, formatUsdCompact } from '@/lib/format';
import { StatCard } from '@/components/primitives/stat-card';
import { Delta } from '@/components/primitives/delta';
import { Button } from '@/components/primitives/button';
import { Card } from '@/components/primitives/card';
import { BotCard } from '@/components/bot-card';
import { EquityCurve } from '@/components/charts/equity-curve';
import { useT } from '@/i18n';

// Lazy: only loaded when the user clicks "New bot" — keeps the wizard's
// validation hooks + Modal off the initial page payload.
const CreateBotWizard = lazy(() =>
  import('@/components/create-bot-wizard').then((m) => ({
    default: m.CreateBotWizard,
  }))
);

export function OverviewPage() {
  const t = useT();
  const botsQuery = useQuery({
    queryKey: ['bots'],
    queryFn: () => api.getBots(),
    refetchInterval: 5_000,
    staleTime: 2_000,
  });

  const summaryQuery = useQuery({
    queryKey: ['portfolio-summary'],
    queryFn: () => api.getPortfolioSummary(),
    refetchInterval: 5_000,
    staleTime: 2_000,
  });

  // Equity curve refreshes less often — daily_snapshots are written ~1/day.
  const curveQuery = useQuery({
    queryKey: ['portfolio-equity-curve'],
    queryFn: () => api.getPortfolioEquityCurve(90),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardPreset, setWizardPreset] = useState<WizardPreset | undefined>();

  // E.2: listen for keyboard shortcut `n b` dispatched from AppShell
  useEffect(() => {
    const handler = () => {
      setWizardPreset(undefined);
      setWizardOpen(true);
    };
    window.addEventListener('wizard:open', handler);
    return () => window.removeEventListener('wizard:open', handler);
  }, []);

  // H.6: BacktestPage navigates here with `presetWizard` in router state.
  // Open the wizard pre-filled, then strip the state so a refresh doesn't
  // re-open it.
  const location = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    const preset = (location.state as { presetWizard?: WizardPreset } | null)?.presetWizard;
    if (preset) {
      setWizardPreset(preset);
      setWizardOpen(true);
      navigate(location.pathname, { replace: true });
    }
  }, [location, navigate]);

  if (botsQuery.isPending) return <PageSkeleton />;
  if (botsQuery.isError) {
    return (
      <Card className="border-danger/40">
        <h2 className="text-lg font-semibold text-danger mb-2">
          {t('overview.failedToLoad')}
        </h2>
        <p className="text-sm text-text-muted">
          {(botsQuery.error as Error).message}
        </p>
      </Card>
    );
  }

  // Hide stopped bots from the overview aggregate stats. Stopped bots
  // are visible under the "History" section on the Bots page.
  const allBots = botsQuery.data?.bots ?? [];
  const bots = allBots.filter((b) => b.status !== 'stopped');

  // Prefer server aggregate; fall back to a quick local sum while it loads
  // so the strip never blanks during the first paint.
  const summary = summaryQuery.data;
  const fallbackInvested = bots.reduce((s, b) => s + b.investment_usdt, 0);
  const fallbackRealized = bots.reduce((s, b) => s + b.grid_profit_usdt, 0);
  const fallbackUnrealized = bots.reduce((s, b) => s + b.trend_pnl_usdt, 0);
  const fallbackPnl = fallbackRealized + fallbackUnrealized;
  const fallbackEquity = fallbackInvested + fallbackPnl;
  const fallbackPct = fallbackInvested > 0 ? (fallbackPnl / fallbackInvested) * 100 : 0;

  const totalInvested = summary?.totalInvested ?? fallbackInvested;
  const totalEquity = summary?.totalEquity ?? fallbackEquity;
  const totalPnl = summary?.totalPnl ?? fallbackPnl;
  const totalPnlPct = summary?.totalPnlPct ?? fallbackPct;
  const totalRealized = summary?.totalRealized ?? fallbackRealized;
  const totalUnrealized = summary?.totalUnrealized ?? fallbackUnrealized;
  const totalPositionUsdt = summary?.totalPositionUsdt ?? 0;
  const avgLeverage = summary?.avgLeverage ?? 0;
  const pairExposure = summary?.pairExposure ?? {};
  const runningCount = summary?.runningCount ?? bots.filter((b) => b.status === 'running').length;

  return (
    <div className="flex flex-col gap-6">
      {/* Page header + create CTA */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {t('overview.title')}
          </h1>
          <p className="text-sm text-text-muted mt-1">
            {t(bots.length === 1 ? 'overview.botCount' : 'overview.botCountPlural', { count: bots.length })}
            {' · '}
            <span className="text-success">
              {t('overview.runningCount', { count: runningCount })}
            </span>
          </p>
        </div>
        <Button onClick={() => setWizardOpen(true)}>
          <Plus className="size-4" />
          {t('overview.newBot')}
        </Button>
      </div>

      {/* Aggregate stat strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-px bg-border-subtle rounded-lg overflow-hidden">
        <StatCard
          label={t('overview.totalEquity')}
          value={formatUsd(totalEquity)}
          delta={<Delta value={totalPnlPct} format={formatPercent} />}
        />
        <StatCard
          label={t('overview.totalPnl')}
          value={
            <span
              className={
                totalPnl > 0
                  ? 'text-success'
                  : totalPnl < 0
                    ? 'text-danger'
                    : 'text-text-primary'
              }
            >
              {formatPnl(totalPnl)}
            </span>
          }
        />
        <StatCard label={t('overview.realized')} value={formatPnl(totalRealized)} />
        <StatCard
          label={t('overview.unrealized')}
          value={
            <span
              className={
                totalUnrealized > 0
                  ? 'text-success'
                  : totalUnrealized < 0
                    ? 'text-danger'
                    : 'text-text-primary'
              }
            >
              {formatPnl(totalUnrealized)}
            </span>
          }
        />
      </div>

      {/* Risk strip — H.7 */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-px bg-border-subtle rounded-lg overflow-hidden">
        <StatCard label={t('overview.positionNotional')} value={formatUsdCompact(totalPositionUsdt)} />
        <StatCard label={t('overview.invested')} value={formatUsdCompact(totalInvested)} />
        <StatCard label={t('overview.avgLeverage')} value={`${avgLeverage.toFixed(1)}x`} />
      </div>

      {/* Portfolio equity + pair exposure — H.7 */}
      {bots.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <Card className="lg:col-span-2">
            <h2 className="text-sm font-semibold text-text-secondary uppercase tracking-wider mb-3">
              {t('overview.portfolioEquityDays', { days: 90 })}
            </h2>
            <EquityCurve points={curveQuery.data?.points ?? []} height={220} />
          </Card>
          <Card>
            <h2 className="text-sm font-semibold text-text-secondary uppercase tracking-wider mb-3">
              {t('overview.pairExposure')}
            </h2>
            <PairExposureList exposure={pairExposure} />
          </Card>
        </div>
      )}

      {/* BotCard grid */}
      <div>
        <h2 className="text-sm font-semibold text-text-secondary uppercase tracking-wider mb-3">
          {t('nav.bots')}
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {bots.map((bot) => (
            <BotCard key={bot.id} bot={bot} />
          ))}
          {/* Create-new tile */}
          <button
            type="button"
            onClick={() => setWizardOpen(true)}
            className="rounded-lg border border-dashed border-border-default hover:border-primary hover:bg-primary-soft/30 transition-colors p-5 min-h-[280px] flex flex-col items-center justify-center gap-3 text-text-muted hover:text-primary"
          >
            <div className="size-12 rounded-full bg-bg-elevated flex items-center justify-center">
              <Plus className="size-6" />
            </div>
            <div className="text-sm font-semibold">{t('overview.createNewBot')}</div>
            <div className="text-2xs text-center max-w-[200px]">
              {t('overview.createNewBotSub')}
            </div>
          </button>
        </div>
      </div>

      {wizardOpen && (
        <Suspense fallback={null}>
          <CreateBotWizard
            open={wizardOpen}
            onClose={() => {
              setWizardOpen(false);
              setWizardPreset(undefined);
            }}
            preset={wizardPreset}
          />
        </Suspense>
      )}
    </div>
  );
}

// Horizontal bars sized to the largest exposure. Empty state when every
// bot has a flat position (e.g. all paused with no fills yet).
function PairExposureList({ exposure }: { exposure: Record<string, number> }) {
  const t = useT();
  const entries = Object.entries(exposure)
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1]);

  if (entries.length === 0) {
    return <p className="text-xs text-text-muted">{t('overview.noOpenPositions')}</p>;
  }

  const total = entries.reduce((s, [, v]) => s + v, 0);
  const max = entries[0][1];

  return (
    <ul className="flex flex-col gap-3">
      {entries.map(([pair, notional]) => {
        const pct = total > 0 ? (notional / total) * 100 : 0;
        const barWidth = max > 0 ? (notional / max) * 100 : 0;
        return (
          <li key={pair} className="flex flex-col gap-1">
            <div className="flex items-center justify-between text-xs">
              <span className="font-mono text-text-primary">{pair}</span>
              <span className="font-mono text-text-muted">
                {formatUsdCompact(notional)} · {pct.toFixed(0)}%
              </span>
            </div>
            <div className="h-1.5 bg-bg-base rounded-full overflow-hidden">
              <div
                className="h-full bg-primary rounded-full transition-[width] duration-300"
                style={{ width: `${barWidth}%` }}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function PageSkeleton() {
  return (
    <div className="flex flex-col gap-6 animate-pulse">
      <div className="h-8 w-48 bg-bg-elevated rounded" />
      <div className="grid grid-cols-2 md:grid-cols-4 gap-px bg-border-subtle rounded-lg overflow-hidden">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-24 bg-bg-elevated" />
        ))}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-72 bg-bg-elevated rounded-lg" />
        ))}
      </div>
    </div>
  );
}
