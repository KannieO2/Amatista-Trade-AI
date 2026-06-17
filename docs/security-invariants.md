# Security Invariants

These rules are product requirements, not implementation preferences.

## Global Rules

- Capital protection has priority over profitability.
- No strategy, AI model, automation, or service may bypass the Risk Engine.
- AI modules may interpret, classify, rank, and recommend, but they cannot modify safety limits.
- All critical actions must be auditable.
- API keys with withdrawal permission must be rejected automatically.

## Pump Reader

> Updated: auto-execution is enabled for the scam-pump system behind the Risk
> Engine (see [scam-pump-system.md](scam-pump-system.md) §4). The original
> "never auto-trade" rule is superseded by "auto-trade only through the Risk
> Engine + kill switch, default paper mode, opt-in live".

Pump Reader must never:

- Withdraw funds.
- Use API keys that carry withdrawal permission.
- Modify critical risk parameters.
- Place any order — paper or live — that the Risk Engine has not approved.
- Trade live without explicit user opt-in (`PUMP_EXEC_MODE=live`) and
  user-supplied API keys.

Pump Reader must always:

- Default to paper mode.
- Pass every order through the Risk Engine and respect the kill switch.
- Show analysis and recommendations.
- Register every action.

## GRVTBot Pro

GRVTBot Pro may:

- Trade automatically.
- Manage grids.
- Reinvest profits.

GRVTBot Pro must never:

- Withdraw funds.
- Modify risk limits.
- Ignore the Risk Engine.
- Operate outside user-defined limits.

## Kill Switch

The kill switch must stop:

- New orders.
- New positions.

Activation triggers include:

- Excessive drawdown.
- Critical errors.
- Exchange failures.
- Security violations.

