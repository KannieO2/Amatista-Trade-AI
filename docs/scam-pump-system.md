# Scam Pump / Criminal Pump System

Source: KManuS88 tool walkthrough. This document maps that tool's behavior onto
TradeOS AI's module structure and the safety invariants in
[security-invariants.md](security-invariants.md). It is the design spec for the
Pump Reader product.

A "scam pump" / "criminal pump" here is the market phenomenon (coordinated
pump-and-dump manipulation). The system trades *around* detecting it early; it
does not create or coordinate one.

---

## Module map

```
Discovery Engine (daily)      -> builds & prunes the watchlist
Update Engine (every 5 min)   -> refreshes metrics, drives state machine
Analysis Engine               -> classifies entry type, scores, Pump DNA
Risk + Execution layer        -> [DECISION PENDING] alert vs gated auto-trade
Learning Engine               -> signal -> action -> result -> threshold tuning
Notification Engine           -> Telegram alert + dashboard
```

### Built today
- `scanner.py`: real Binance Spot USDT scan, altcoin filter, rule heuristics
  (volume spike, price run, orderbook imbalance, low-liquidity trap),
  classification. Covers part of Discovery + Update + Analysis.

### Not built yet
- Pair-age filter, watchlist persistence + state machine (drop on rule-fail).
- `Long Pump` vs `Classic` (short squeeze) classification.
- Inflow + holder-concentration signals (on-chain — not in public CCXT).
- 5-min scheduler, Telegram alerts.
- Execution layer, Learning loop.

---

## 1. Discovery Engine (daily scan)

Initial filters before a token can enter the watchlist:

| Filter            | Rule                                              |
|-------------------|---------------------------------------------------|
| Quote volume 24h  | between MIN and MAX (ignore dust + blue chips)    |
| Liquidity (depth) | resting notional within 2% of mid >= floor        |
| Pair age          | listed > N days (avoid brand-new illiquid listings)|
| Universe          | Spot /USDT, exclude majors / stables / leveraged  |

```
function discover():
    markets = exchange.load_markets()
    candidates = []
    for m in markets where is_altcoin(m):
        t = ticker(m)
        if not (MIN_VOL <= t.quote_volume <= MAX_VOL): continue
        if pair_age_days(m) < MIN_AGE: continue
        candidates.append(m)
    for c in candidates:
        watchlist.upsert(c, status="watching")
    # prune: anything in watchlist that no longer passes base filters
    for w in watchlist where status in ("watching","waiting_confirmation"):
        if not passes_base_filters(w): watchlist.set(w, status="expired")
```

## 2. Update Engine (poll every 5 min)

Refresh live metrics for the watchlist and run the state machine. This is the
"Update" loop the author runs continuously.

```
function update():
    for w in watchlist where status active:
        m = refresh_metrics(w)   # price, volume_spike, imbalance, liquidity,
                                 # inflow, holder_concentration, open_interest
        score, conf, cls, flags = analyze(m)
        w.update(score, conf, cls, flags)
        transition(w, score, conf)

function transition(w, score, conf):
    if score >= ENTER_THRESHOLD and conf >= CONF_THRESHOLD:
        w.status = "waiting_confirmation"   # alert + (optional) execute
    elif score < DROP_THRESHOLD:
        w.status = "watching"
```

## 3. Analysis Engine

### Entry classification
- **Long Pump**: buyer-driven impulse. Volume spike + price acceleration +
  bid-stacked orderbook + inflow spike. Bet = continuation.
- **Classic (short squeeze)**: rising open interest + funding rate flipping +
  price grinding into resting asks. Bet = forced short covering.

```
function classify_entry(m):
    if m.open_interest_rising and m.funding_negative and m.price_grind_up:
        return "classic_short_squeeze"
    if m.volume_spike >= 3 and m.price_change > 0 and m.imbalance >= 0.65:
        return "long_pump"
    return "none"
```

### Signals (data source matters)
| Signal                 | Source                | Status            |
|------------------------|-----------------------|-------------------|
| volume spike           | exchange OHLCV        | BUILT             |
| price change / accel   | exchange ticker/OHLCV | BUILT             |
| orderbook imbalance    | exchange order book   | BUILT             |
| liquidity / low-liq    | exchange order book   | BUILT             |
| open interest, funding | exchange (futures)    | pending           |
| inflow (to exchanges)  | on-chain provider     | needs data source |
| holder concentration   | on-chain provider     | needs data source |

`inflow` and `holder_concentration` cannot come from public CCXT. They require
an on-chain data provider. Until one is wired, those rules are disabled rather
than faked.

### Pump DNA
Compare current signal vector against historical successful pumps (cosine /
distance match). Feeds a `dna_match_pct`. Requires the Learning store to have
labeled history first.

## 4. Risk + Execution layer  — DECISION: gated auto-execution (paper default)

Chosen: option B. The system auto-buys/sells and splits capital across the
configured exchanges (MEXC/Bitget), like the source tool — but **only** through
the Risk Engine + kill switch, with capped position size. This supersedes the
old "Pump Reader must never auto-trade" invariant; see
[security-invariants.md](security-invariants.md).

Hard safety rules that still hold, no matter what:
- Default mode is **paper** (`PUMP_EXEC_MODE=paper`). Live trading is opt-in and
  requires user-supplied per-exchange API keys.
- API keys with withdrawal permission are rejected.
- Every order passes the Risk Engine: position size, leverage, daily loss,
  drawdown, open-trade count.
- Kill switch stops all new orders.
- If the Risk Engine is unreachable, live orders fail closed.

Built today (`app/executor.py`, `app/risk.py`): paper broker, capital split,
SL/TP, RiskGuard gating, kill switch. Live broker is scaffolded but refuses to
run until explicitly enabled. Endpoints: `POST /act/{symbol}`, `GET /positions`,
`POST /risk/kill-switch`.

```
function on_entry_signal(w):
    notify_telegram(w)                       # always
    if mode == ALERT_ONLY:
        prepare_order(w); await human_approval(w)
    elif mode == AUTO and risk_engine.allow(order(w)):
        split = allocate_capital(w, exchanges=[mexc, bitget])
        for leg in split:
            place_order(leg, type=MARKET_or_LIMIT,
                        stop_loss=sl(w), take_profit=tp(w))
```

## 5. Learning Engine (feedback loop)

```
record(signal, action, result)
label = classify_outcome(result)   # early / perfect / late / false_positive
adjust_thresholds(label):          # lightweight RL / contextual bandit
    if label == false_positive: raise ENTER_THRESHOLD slightly
    if label == late_entry:     lower confirmation latency
    if label in (early, perfect): reinforce current vector in Pump DNA
```

State machine for outcomes drives threshold nudges. Start with a simple bandit
over threshold sets; full RL later once enough labeled history exists.

## 6. Notification Engine

- Telegram bot API: push on `waiting_confirmation` with symbol, score, type,
  flags, suggested entry. (Author's early version was Telegram-only.)
- Dashboard: live candidate table consuming `/candidates` + `/scan`.
- Polling cadence: Update every 5 min, Discovery daily.
