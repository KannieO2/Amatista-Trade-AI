export type ExchangeId = "binance" | "bitget" | "mexc" | "hyperliquid" | "grvt";

export type PumpCandidateStatus =
  | "watching"
  | "waiting_confirmation"
  | "approved"
  | "rejected"
  | "expired";

export interface PumpCandidate {
  id: string;
  symbol: string;
  exchange: ExchangeId;
  pumpScore: number;
  confidenceScore: number;
  volumeRatio: number;
  holderConcentration: number;
  orderbookImbalance: number;
  status: PumpCandidateStatus;
  updatedAt: string;
}

export interface RiskLimits {
  maxDailyLossUsd: number;
  maxDrawdownPct: number;
  maxPositionSizeUsd: number;
  maxOpenTrades: number;
  maxLeverage: number;
}

export interface RiskDecision {
  allowed: boolean;
  reason: string;
  killSwitchActive: boolean;
}

