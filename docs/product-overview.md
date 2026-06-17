# Product Overview

TradeOS AI contains two independent trading products and a shared safety layer.

## Pump Reader

Pump Reader detects early opportunities before explosive crypto market moves. It analyzes criminal pumps, scam pumps, short squeezes, whale accumulation, liquidity traps, and micro-cap manipulation.

Immutable rule: Pump Reader can prepare and recommend orders, but it cannot buy, sell, withdraw funds, or change critical risk parameters. Human approval is always required before execution.

Core engines:

- Discover Engine: runs every 6 hours and produces `token_candidates`.
- Update Engine: runs every 5 minutes and updates price, volume, liquidity, order book, open interest, funding rate, inflows, and holder concentration.
- Pump DNA Engine: compares new candidates against historically successful signal patterns.
- Learning Engine integration: classifies outcomes as early entry, perfect entry, late entry, or false positive.

Scores:

- Pump Score: 0-100 probability that pump conditions exist.
- Confidence Score: 0-100 statistical confidence in the signal.

## GRVTBot Pro

GRVTBot Pro evolves the open-source GRVTBot into a professional grid-trading system for volatility capture, volume farming, virtual grids, reinvestment, paper trading, and backtesting.

It may operate autonomously, but only inside limits enforced by the Risk Engine. It can never withdraw funds, change risk limits, ignore kill-switch state, or operate outside user-defined boundaries.

## Shared Components

- Exchange Hub: unified CCXT-based exchange layer for Binance, Bitget, MEXC, Hyperliquid, and GRVT.
- Risk Engine: centralized protection for daily loss, drawdown, max position size, open-trade count, leverage, and kill switch.
- Notification Engine: dashboard, Telegram, email, Discord later, mobile app later.
- Learning Framework: signal -> action -> result -> learning.

