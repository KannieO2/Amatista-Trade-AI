# GRVT Grid

Multi-user grid trading bot for the [GRVT](https://grvt.io) perpetual
futures exchange. Self-hostable, AGPL-licensed, with a real-time web
dashboard, Telegram alerts, and per-user encrypted API credentials.

There is also a hosted instance at **[grvtbot.com](https://grvtbot.com)**
— it runs the exact code in this repo. You don't have to host your own
to use it.

## Use it

**Option 1 — Use the hosted instance** at
[grvtbot.com](https://grvtbot.com). Create an account, paste your GRVT
API credentials (encrypted at rest with AES-256-GCM), configure a grid,
and the bot trades on your sub-account 24/7. The operator does not
touch your funds. See [SECURITY.md](SECURITY.md) for the exact threat
model — short version: the operator technically can decrypt your
credentials because the master key lives on the same server. If you
want zero third-party access to your keys, use Option 2.

**Option 2 — Self-host your own copy**. See
[docs/INSTALL.md](docs/INSTALL.md) for the full setup. Quick version:

```bash
git clone https://github.com/kmanus88/GRVTBot.git
cd GRVTBot
npm install
npm run build

# generate the encryption master key (32 random bytes, file 0600)
sudo mkdir -p /etc/grvt-grid
sudo sh -c 'head -c 32 /dev/urandom > /etc/grvt-grid/master.key'
sudo chmod 600 /etc/grvt-grid/master.key

# copy + fill the example env (you supply your own GRVT API creds, SMTP,
# JWT_SECRET, etc. — see packages/bot/.env.example for every field)
cp packages/bot/.env.example packages/bot/.env

# run the bot (production runs it under systemd — see docs/INSTALL.md)
node packages/bot/dist/dashboard/server.js
```

## What it does

- **Grid trading**: defines a price range and N levels, places buy/sell
  limit orders at every level, replaces fills automatically. Supports
  range update, compounding, stop-loss / take-profit, safeguard pauses,
  auto-shift, and backtesting.
- **Per-bot virtual grid**: configure a wider range than what fits in
  GRVT's 80-order-per-instrument limit; the bot keeps an active window
  of orders around the current price and shifts as price moves.
- **Multi-user, multi-bot**: every user signs up with their own GRVT
  credentials and runs their own bots in isolation. No data leaks
  between tenants.
- **Real-time dashboard**: equity curve, per-bot stats, fills, position,
  PnL, alerts. WebSocket-driven so updates appear without a refresh.
- **Telegram alerts** (optional): batched fills, drawdown warnings,
  liquidation-proximity warnings, daily summary.

## Architecture

```
packages/
  bot/        Engine + REST API + WebSocket server (Node, TypeScript)
  dashboard/  SPA frontend (Vite + React + Tailwind + Recharts)
  notifier/   Standalone Telegram alerts worker
scripts/      Backup + admin utilities
docs/         Install, rollback, operational notes
```

The bot process holds the GRVT WebSocket connection, runs the grid
engine, and serves the REST/WS API. The dashboard is a static SPA the
bot serves alongside the API. The notifier is an optional separate
process that reads the bot's DB (read-only) and pushes alerts.

Data lives in SQLite at `data/grid_bot.db` (a single file). User
passwords are bcrypt-hashed; GRVT credentials are AES-256-GCM encrypted
with a key on disk. See [SECURITY.md](SECURITY.md).

## Status

In production. The hosted instance at
[grvtbot.com](https://grvtbot.com) has been running real trades for
months. Issues + PRs welcome.

## Contributing

Run `npm test` from the root — currently 208 tests across bot,
dashboard, and notifier packages. PRs that add features should include
tests; PRs that fix bugs should include a regression test.

## License

[AGPL-3.0-or-later](LICENSE). In short: you're free to use, modify, and
self-host this code. **If you modify it AND run it as a network
service, you must publish your modifications under the same license.**
This is to keep forks and competing hosted instances in the open
source.

## Security

Vulnerability reports: **do not** open a public GitHub issue. See
[SECURITY.md](SECURITY.md) for the reporting process and full threat
model.
