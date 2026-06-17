# TradeOS AI

TradeOS AI is a quantitative crypto trading ecosystem composed of two independent products:

- **Pump Reader**: early detection and decision-support for pump, liquidity-trap, whale-accumulation, and micro-cap manipulation signals. It never executes trades automatically.
- **GRVTBot Pro**: automated grid trading and volatility capture, always constrained by the Risk Engine.

Capital protection has priority over profitability. No strategy, AI model, automation, or service may bypass security, risk management, or auditability.

## Repository Layout

```text
apps/
  dashboard/          Next.js operator dashboard
  pump-reader/        Pump Reader API and engines
  grvtbot-pro/        GRVTBot Pro placeholder service
services/
  exchange-hub/       Unified exchange access layer
  learning-engine/    Global learning event service
  notification-engine/Notification service
  risk-engine/        Centralized safety and risk service
packages/
  shared-types/       Shared TypeScript contracts
  database/           Database models and migrations
infrastructure/
  docker/             Docker Compose and runtime files
docs/                 Product, architecture, and safety docs
```

## Quick Start

Copy the environment template:

```powershell
Copy-Item .env.example .env
```

Start the base stack:

```powershell
docker compose -f infrastructure/docker/docker-compose.yml up --build
```

Local service URLs:

- Dashboard: `http://localhost:3000`
- Pump Reader API: `http://localhost:8001/docs`
- Risk Engine: `http://localhost:8002/docs`
- Exchange Hub: `http://localhost:8003/docs`
- Notification Engine: `http://localhost:8004/docs`
- Learning Engine: `http://localhost:8005/docs`

## Development Priorities

1. Base infrastructure: Docker, PostgreSQL, FastAPI, Next.js.
2. Pump Reader MVP: discover engine, update engine, dashboard, alerts.
3. GRVTBot audit: architecture, security, persistence, risk boundaries.
4. GRVTBot Pro: dashboard integration and autonomous grid execution under Risk Engine control.
5. Learning Engine: historical outcomes, Pump DNA, parameter optimization.
6. Multi-exchange expansion: Binance, Bitget, MEXC, Hyperliquid, GRVT.

