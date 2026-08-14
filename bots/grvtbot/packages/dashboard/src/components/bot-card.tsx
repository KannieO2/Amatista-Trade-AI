// BotCard — Overview page tile per design doc §8.1.
// Click anywhere navigates to the bot detail page.

import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { Card } from './primitives/card';
import { Mono } from './primitives/mono';
import { StatusPill } from './primitives/status-pill';
import { Delta } from './primitives/delta';
import { Sparkline } from './charts/sparkline';
import { api } from '@/lib/api-client';
import { useWsChannel } from '@/lib/use-ws-channel';
import {
  formatPercent,
  formatPnl,
  formatSize,
  formatUsd,
} from '@/lib/format';
import type { BotSummary } from '@/lib/api-types';
import { useT } from '@/i18n';

interface BotTick {
  status: BotSummary['status'];
  positionSize: number;
  avgEntryPrice: number;
  gridProfit: number;
  trendPnl: number;
  totalPnl: number;
}

interface BotCardProps {
  bot: BotSummary;
}

export function BotCard({ bot }: BotCardProps) {
  const t = useT();
  // Per-card WS subscription. Each card listens to its own bot:N channel
  // so the parent (Overview / BotsList) doesn't need a hardcoded bot id
  // or a global tick map. The hook is stable per card instance.
  const [tick, setTick] = useState<BotTick | null>(null);
  useWsChannel<BotTick>(`bot:${bot.id}`, (msg) => {
    if (msg.type === 'tick') setTick(msg.data);
  });
  // Pull last 30 days of snapshots for the sparkline. Cheap query (≤30 rows).
  const snapshots = useQuery({
    queryKey: ['snapshots', bot.id],
    queryFn: () => api.getSnapshots(bot.id),
    staleTime: 5 * 60_000, // 5 min — daily snapshots only update once/day
  });

  const sparkData = (snapshots.data?.snapshots ?? [])
    .slice(0, 30)
    .reverse()
    .map((s) => ({ value: s.equity_usdt }));

  const status = tick?.status ?? bot.status;
  const totalPnl = tick?.totalPnl ?? bot.total_pnl_usdt;
  const gridProfit = tick?.gridProfit ?? bot.grid_profit_usdt;
  const trendPnl = tick?.trendPnl ?? bot.trend_pnl_usdt;
  const positionSize = tick?.positionSize ?? bot.position_size;
  const avgEntry = tick?.avgEntryPrice ?? bot.avg_entry_price;
  // OJO: esto NO es equity. Es el capital asignado en la config más el PnL,
  // y el label lo dice así. El equity de verdad es por CUENTA, no por bot —
  // GRVT usa cross margin, así que no existe un equity atribuible a un bot
  // solo. El número real de la cuenta está en el Resumen, que lo pide a GRVT.
  //
  // La diferencia no es cosmética: el tamaño de orden se redondea hacia
  // arriba al min_size del par, así que un bot puede desplegar bastante más
  // que su investment_usdt. Bot 7, 2026-08-14: config $16, nocional real $121.
  const allocatedPlusPnl = bot.investment_usdt + totalPnl;
  const allocatedPct = (totalPnl / bot.investment_usdt) * 100;
  // Nocional real de la posición de ESTE bot — sí es atribuible y sí es real.
  const notional = Math.abs(positionSize) * avgEntry;

  return (
    <Link
      to={`/bots/${bot.id}`}
      // The Link itself gets no outline because we move the focus indicator
      // to the Card border below — more on-style. The global *:focus-visible
      // rule in globals.css would otherwise paint a primary outline outside
      // the card border, which clashes with the rounded corners.
      className="block hover:no-underline focus-visible:outline-none focus-visible:[&_div[data-card]]:border-primary"
      aria-label={`Open bot ${bot.id} ${bot.pair} ${bot.direction} ${bot.leverage}x`}
    >
      <Card
        data-card
        className="hover:border-border-default cursor-pointer p-5 transition-colors"
      >
        {/* Header */}
        <div className="flex items-start justify-between mb-3">
          <div>
            <h3 className="text-base font-semibold text-text-primary">
              {bot.pair}
            </h3>
            <p className="text-2xs uppercase tracking-wider text-text-muted mt-0.5">
              {bot.direction} · {bot.leverage}x
            </p>
          </div>
          <StatusPill status={status} />
        </div>

        {/* Equity hero */}
        <div className="mb-1">
          <span className="text-2xs uppercase tracking-wider text-text-muted">
            {t('bots.cardEquity')}
          </span>
        </div>
        <div className="flex items-baseline gap-3 mb-2">
          <Mono className="text-2xl font-semibold text-text-primary">
            {formatUsd(allocatedPlusPnl)}
          </Mono>
          <Delta value={allocatedPct} format={formatPercent} />
        </div>

        {/* Sparkline */}
        <div className="mb-4">
          <Sparkline data={sparkData} />
        </div>

        {/* Stats grid */}
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
          <SummaryRow
            label={t('bots.cardPosition')}
            value={`${formatSize(positionSize)} @ ${formatUsd(avgEntry)}`}
          />
          <SummaryRow
            label={t('bots.cardRange')}
            value={`${formatUsd(bot.lower_price)}–${formatUsd(bot.upper_price)}`}
          />
          <SummaryRow label={t('bots.cardGrids')} value={t('bots.cardLevels', { n: bot.num_grids })} />
          <SummaryRow label={t('bots.cardRealized')} value={formatPnl(gridProfit)} />
          <SummaryRow label={t('bots.cardUnrealized')} value={formatPnl(trendPnl)} />
          <SummaryRow label={t('bots.cardInvestment')} value={formatUsd(bot.investment_usdt)} />
          <SummaryRow label={t('bots.cardNotional')} value={formatUsd(notional)} />
        </dl>
      </Card>
    </Link>
  );
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-2xs uppercase tracking-wider text-text-muted">
        {label}
      </dt>
      <dd className="font-mono tabular-nums text-text-secondary truncate">
        {value}
      </dd>
    </div>
  );
}
