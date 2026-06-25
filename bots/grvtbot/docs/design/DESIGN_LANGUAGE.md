# GRVT Grid — Design Language

> **Source of truth.** This document fixes every visual and interaction decision for the GRVT Grid dashboard. Don't renegotiate during implementation. Any deviation from this doc requires updating this doc first.

> **Status**: v1.0 — 2026-04-07

---

## Executive summary

GRVT Grid is a self-hosted real-time dashboard for grid trading bots on the GRVT perpetual futures exchange. The visual language is **Modernist Trading Terminal**: a dark-first, information-dense, opinionated interface that takes the data density of a Bloomberg terminal and the restraint of Linear/Vercel/Hyperliquid. No glow effects. No glassmorphism. No emoji. No playful tone. Numbers in tabular monospace, surfaces in flat dark elevations, color used semantically (green = up, red = down, amber = at risk, sky = action), motion used to convey state changes — never decoration.

---

## 1. Style — Modernist Trading Terminal

**What it is**: A composition that blends three references:

| Reference | What we take |
|---|---|
| **Linear / Vercel dashboards** | Flat surfaces, minimal shadows, generous whitespace within dense regions, monochromatic primary palette |
| **Hyperliquid / dYdX trade UIs** | Dark-first crypto-native vocabulary, semantic green/red, mono numbers |
| **Bloomberg Terminal** | Maximum information per pixel, no decoration, tabular grids, status pills, real-time price ticks |

**What it explicitly is NOT**:
- Not cyberpunk / neon-overload
- Not glassmorphism (no blur, no transparent layered surfaces — they hurt readability of data)
- Not playful / friendly / emoji-laden
- Not "AI gradient" purple/pink (the GRVT brand is orange but our dashboard is its OWN tool — we use a different accent to differentiate)
- Not card-heavy (cards exist but they're flat containers, not visually elevated — elevation is a meaning, not a default)

**Five visual principles**:
1. **Density over decoration.** If a region can show a useful number, it should. Whitespace exists to *separate*, not to *fill*.
2. **Color is semantic, not decorative.** Each hex has a job: success/danger/warning/primary action. Charts use a fixed series palette in fixed order.
3. **Motion conveys state, never delight.** Price ticks crossfade. Fill events flash once. Drawdown warnings pulse only when actively dangerous. Reduced-motion = no motion.
4. **Tabular numbers everywhere.** A digit changing must never push neighbors. JetBrains Mono with `font-feature-settings: "tnum"`.
5. **Trust through restraint.** This is real money. No celebration emoji on profit. No animated confetti on a fill. The reward is the number going up.

---

## 2. Color palette (dark mode — default)

Base: a custom variation of the "Financial Dashboard" palette from the design system DB, adjusted for the GRVT Grid identity.

### Background hierarchy (3 levels of dark)

| Token | Hex | Use |
|---|---|---|
| `bg-base` | `#020617` | Body background. Deepest void. |
| `bg-surface` | `#0B1120` | Page sections, sidebar, header background. |
| `bg-elevated` | `#0F172A` | Cards, modals, dropdowns. The "lifted" surface. |
| `bg-muted` | `#1A2236` | Hover states on rows, secondary inputs, disabled fills. |

### Borders (subtle, multiple weights)

| Token | Hex | Use |
|---|---|---|
| `border-subtle` | `#1E293B` | Default card border, table row dividers, input border |
| `border-default` | `#334155` | Active borders, focused inputs, hovered cards |
| `border-strong` | `#475569` | High-emphasis dividers (header bottom, modal divider) |

### Text hierarchy (3 levels)

| Token | Hex | Use | Contrast on `bg-base` |
|---|---|---|---|
| `text-primary` | `#F8FAFC` | Headings, primary numbers, body text | 17.4:1 (AAA) |
| `text-secondary` | `#CBD5E1` | Labels, captions, table headers | 12.6:1 (AAA) |
| `text-muted` | `#94A3B8` | Tertiary metadata, timestamps, helper text | 7.2:1 (AAA) |
| `text-disabled` | `#64748B` | Disabled state, placeholder text | 4.5:1 (AA) |

### Semantic colors

| Token | Hex | Meaning | Contrast on `bg-base` |
|---|---|---|---|
| `success` | `#22C55E` | Buy orders, profit, equity up, healthy state | 10.4:1 |
| `success-strong` | `#16A34A` | Pressed/active state of success buttons | 7.7:1 |
| `success-soft` | `#022C16` | Subtle background for success badges | — |
| `danger` | `#EF4444` | Sell orders, loss, drawdown, destructive actions | 5.9:1 |
| `danger-strong` | `#DC2626` | Pressed state of danger buttons | 4.7:1 |
| `danger-soft` | `#2C0B0B` | Subtle background for danger badges | — |
| `warning` | `#F59E0B` | Pending replacement, at-risk states, near-liquidation | 11.2:1 |
| `warning-strong` | `#D97706` | Pressed state of warning buttons | 8.6:1 |
| `warning-soft` | `#2C1B05` | Subtle background for warning badges | — |
| `info` | `#38BDF8` | Informational badges, neutral status | 9.0:1 |

### Primary accent (CTA + active states)

The most-used non-semantic color in the app. **Sky-400** (`#38BDF8`).

| Token | Hex | Use |
|---|---|---|
| `primary` | `#38BDF8` | Primary CTAs, active nav item, focus rings, links, range selectors |
| `primary-strong` | `#0EA5E9` | Hover/pressed state |
| `primary-soft` | `#0C2A3A` | Subtle background of primary badges, selected row in table |

**Why sky and not purple/orange/green?**
- Green is taken (success).
- Red is taken (danger).
- Amber is taken (warning).
- Orange is the GRVT brand color — using it would conflate "the dashboard" with "the exchange we connect to". This dashboard is its own product.
- Purple/violet feels too "AI startup" (the rejected anti-pattern).
- Sky-400 is **distinct from all semantic colors**, has a 9:1 contrast on `bg-base`, and reads as "trustworthy tech" (Linear, Vercel, Stripe Atlas all use blues).

### Chart series palette

For multi-series charts (equity curves of multiple bots, comparison views, heatmaps). Fixed order, consistent across the app. Tested for colorblind-safety:

| # | Token | Hex |
|---|---|---|
| 1 | `chart-1` | `#38BDF8` (sky) |
| 2 | `chart-2` | `#22C55E` (green) |
| 3 | `chart-3` | `#F59E0B` (amber) |
| 4 | `chart-4` | `#A78BFA` (violet) |
| 5 | `chart-5` | `#F472B6` (pink) |
| 6 | `chart-6` | `#94A3B8` (gray) |

Plus the special candlestick colors:

| Token | Hex | Use |
|---|---|---|
| `candle-up` | `#22C55E` | Bullish candle body |
| `candle-down` | `#EF4444` | Bearish candle body |
| `candle-up-wick` | `#16A34A` | Bullish wick (slightly darker) |
| `candle-down-wick` | `#B91C1C` | Bearish wick (slightly darker) |
| `volume-up` | `#22C55E40` | Volume bar bullish (40% opacity) |
| `volume-down` | `#EF444440` | Volume bar bearish (40% opacity) |

### Light mode (optional, lower priority)

Light mode is offered for daytime use but the product is **dark-first**. The light palette inverts surfaces and uses slightly desaturated semantic colors to avoid retina burn:

| Token | Light hex |
|---|---|
| `bg-base` | `#F8FAFC` |
| `bg-surface` | `#FFFFFF` |
| `bg-elevated` | `#FFFFFF` |
| `bg-muted` | `#F1F5F9` |
| `border-subtle` | `#E2E8F0` |
| `border-default` | `#CBD5E1` |
| `text-primary` | `#0F172A` |
| `text-secondary` | `#334155` |
| `text-muted` | `#64748B` |
| `success` | `#16A34A` |
| `danger` | `#DC2626` |
| `warning` | `#D97706` |
| `primary` | `#0EA5E9` |

Semantic colors are NOT inverted — green stays green, red stays red. Only saturation changes.

---

## 3. Typography

**Two families. No more.**

### Sans (UI): Inter

The default for all UI text — labels, buttons, body copy, headings, navigation, tooltips. Inter is the gold standard for dense data interfaces (used by Linear, GitHub, Vercel, Coinbase, Stripe Dashboard). Free, open, served from Google Fonts. Excellent at all sizes including 11px.

```css
font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
```

### Mono (numbers, prices, addresses, IDs): JetBrains Mono

The ONE place where mono matters: any number that updates in real time, any wallet address, any order ID, any price, any percentage. JetBrains Mono ships with **tabular figures by default** (no extra config), looks identical at every digit width, and stays readable at 12px (which we'll use for table cells).

```css
font-family: 'JetBrains Mono', 'SF Mono', Monaco, Consolas, monospace;
font-feature-settings: "tnum" 1, "calt" 0;
```

`calt` (contextual alternates) is **disabled** for prices because we don't want the `=>` ligature appearing in numbers. Tabular numbers are guaranteed.

### Type scale (rem, base 16px)

Use this exact scale. No improvisation. Aligned to 4-pixel grid.

| Token | Size (rem / px) | Line height | Weight | Use |
|---|---|---|---|---|
| `text-2xs` | 0.6875 / 11 | 1.4 | 500 | Smallest labels (e.g. table header), badge text |
| `text-xs` | 0.75 / 12 | 1.5 | 400 | Captions, helper text, table cells |
| `text-sm` | 0.8125 / 13 | 1.5 | 400 | Default body, form inputs, button text |
| `text-base` | 0.875 / 14 | 1.5 | 400 | Card titles, section headers |
| `text-md` | 1.0 / 16 | 1.5 | 500 | Navigation, primary labels |
| `text-lg` | 1.125 / 18 | 1.4 | 500 | Subheadings, large stat labels |
| `text-xl` | 1.25 / 20 | 1.3 | 600 | Page section titles |
| `text-2xl` | 1.5 / 24 | 1.2 | 600 | Modal titles, "Big" stat values (in mono) |
| `text-3xl` | 1.875 / 30 | 1.15 | 700 | Page hero numbers (total equity, daily PnL) |
| `text-4xl` | 2.25 / 36 | 1.1 | 700 | Bot detail page hero |

**Rules**:
- Body text minimum 13px on desktop, 14px on mobile (iOS 16px-or-zoom rule applied to inputs only).
- Numbers from 12px and up: ALWAYS use mono (`text-xs` and above when displaying values).
- Number variations (e.g. `+1.23%`) align right, mono, with semantic color from §2.
- Headings: weight 600 (semibold). Hero numbers: weight 700 (bold). Body: weight 400 (regular). Labels: weight 500 (medium). No 800/900 weights anywhere.

---

## 4. Tailwind v4 config snippet

```css
/* packages/dashboard/src/styles/globals.css */
@import "tailwindcss";

@theme {
  /* Background hierarchy */
  --color-bg-base: #020617;
  --color-bg-surface: #0B1120;
  --color-bg-elevated: #0F172A;
  --color-bg-muted: #1A2236;

  /* Borders */
  --color-border-subtle: #1E293B;
  --color-border-default: #334155;
  --color-border-strong: #475569;

  /* Text */
  --color-text-primary: #F8FAFC;
  --color-text-secondary: #CBD5E1;
  --color-text-muted: #94A3B8;
  --color-text-disabled: #64748B;

  /* Semantic */
  --color-success: #22C55E;
  --color-success-strong: #16A34A;
  --color-success-soft: #022C16;
  --color-danger: #EF4444;
  --color-danger-strong: #DC2626;
  --color-danger-soft: #2C0B0B;
  --color-warning: #F59E0B;
  --color-warning-strong: #D97706;
  --color-warning-soft: #2C1B05;
  --color-info: #38BDF8;

  /* Primary accent */
  --color-primary: #38BDF8;
  --color-primary-strong: #0EA5E9;
  --color-primary-soft: #0C2A3A;

  /* Chart series */
  --color-chart-1: #38BDF8;
  --color-chart-2: #22C55E;
  --color-chart-3: #F59E0B;
  --color-chart-4: #A78BFA;
  --color-chart-5: #F472B6;
  --color-chart-6: #94A3B8;

  /* Candlestick */
  --color-candle-up: #22C55E;
  --color-candle-down: #EF4444;

  /* Fonts */
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', 'SF Mono', Monaco, Consolas, monospace;

  /* Spacing — 4px grid */
  --spacing-0_5: 0.125rem; /* 2px */
  --spacing-1: 0.25rem;    /* 4px */
  --spacing-1_5: 0.375rem; /* 6px */
  --spacing-2: 0.5rem;     /* 8px */
  --spacing-2_5: 0.625rem; /* 10px */
  --spacing-3: 0.75rem;    /* 12px */
  --spacing-4: 1rem;       /* 16px */
  --spacing-5: 1.25rem;    /* 20px */
  --spacing-6: 1.5rem;     /* 24px */
  --spacing-8: 2rem;       /* 32px */
  --spacing-10: 2.5rem;    /* 40px */
  --spacing-12: 3rem;      /* 48px */
  --spacing-16: 4rem;      /* 64px */

  /* Radii */
  --radius-sm: 0.25rem;  /* 4px — inputs, small badges */
  --radius-md: 0.5rem;   /* 8px — buttons, cards */
  --radius-lg: 0.75rem;  /* 12px — modals, large containers */
  --radius-xl: 1rem;     /* 16px — special hero containers */
  --radius-full: 9999px; /* pills, status badges */

  /* Shadows — minimal, dark-mode-aware */
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.5);
  --shadow-lg: 0 12px 32px rgba(0, 0, 0, 0.6);
  /* Used sparingly. Surfaces lift via background color, not shadow. */
}

/* Tabular figures globally on the .mono utility */
.mono, .font-mono, code, pre {
  font-feature-settings: "tnum" 1, "calt" 0;
}

/* Reduced motion */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
```

---

## 5. UX guidelines — top 5 (locked in)

These are the 5 rules from the 99-rule catalog that we promise to obey throughout the dashboard. If a PR violates one of these, it gets rejected.

### G1. Tabular numbers everywhere (`number-tabular`)

Every number that can update in real time MUST use mono with `font-feature-settings: "tnum"`. This is non-negotiable. A price ticking from `$2,103.45` to `$2,103.50` must not push the column. JetBrains Mono handles this by default; just use `.font-mono` and don't override `font-feature-settings`.

### G2. State clarity (`state-clarity`)

A bot exists in one of 4 states: `running`, `paused`, `stopped`, `error`. Each state has a fixed visual signature that NEVER varies:

| State | Pill background | Pill text | Icon | Card border |
|---|---|---|---|---|
| `running` | `success-soft` (#022C16) | `#22C55E` | ● (filled, pulsing 2s slow) | `border-subtle` |
| `paused` | `warning-soft` (#2C1B05) | `#F59E0B` | ⏸ (Lucide `Pause`) | `border-subtle` |
| `stopped` | `bg-muted` | `text-muted` | ⏹ (Lucide `Square`) | `border-subtle` |
| `error` | `danger-soft` (#2C0B0B) | `#EF4444` | ⚠ (Lucide `AlertCircle`, pulsing fast) | `border-default` w/ red tint |

These pills + icons are the SAME in BotCard, BotDetail header, system tray, table cells. One visual = one meaning, everywhere.

### G3. Destructive emphasis + confirmation (`destructive-emphasis` + `confirmation-dialogs`)

Any action that affects real money — pause bot, close bot, cancel orders, change leverage, change range, delete bot — MUST:
1. Use the danger color for its trigger button (or be a secondary action visually subordinate to a primary "safer" action)
2. Open a confirmation modal showing **the exact consequence in numbers** ("This will cancel 93 open orders worth $4,153 USDT and close your 2.0 ETH position at current mark price $2,077, realizing approximately $-52 PnL")
3. Require typing the bot name OR clicking a 3-second-delayed "I understand" button for the most destructive actions (close + delete)

The destructive button label is always **specific** ("Cancel 93 orders" not "Cancel"). Never lie to the user about scope.

### G4. Color is never the only signal (`color-not-only`)

PnL is green when positive AND has a `▲` icon. PnL is red when negative AND has a `▼` icon. Status pills use color AND a unique shape/icon. Drawdown warnings use color AND a pulse AND text. Charts use color AND distinct line styles when there are >2 series. This unblocks colorblind users and doubles as a robustness check (if you can't read the data with the page in grayscale, the design has failed).

### G5. Error clarity with recovery path (`error-clarity` + `error-recovery`)

When the GRVT API returns 401, the dashboard does NOT say "Error". It says: **"GRVT session expired — re-authenticating..."** with a spinner, and auto-retries. When it returns 7201 (price band): **"Order at $X.XX rejected by GRVT (price band ±5%). Will retry when market moves into range."** When margin is insufficient for a new order: **"Insufficient margin: $X.XX required, $Y.YY available. Free up margin by closing positions or reduce grid count."** Every error states **what happened**, **why**, and **what to do or what we're doing automatically**. No raw stack traces. No HTTP status codes alone.

---

## 6. Charts — concrete specs per chart type

### 6.1 GridChart (the hero — Bot Detail page)

**Library**: TradingView Lightweight Charts v5.x — chosen because it's the only canvas-based candle chart that handles 1000+ candles + overlays at 60fps in 14KB gzipped, MIT licensed, and is literally what TradingView itself uses.

**Layout**:
- Aspect ratio: 16:9 on desktop, 4:3 on mobile (less wide so candles aren't smushed)
- Background: `bg-elevated` (#0F172A)
- Grid lines: `border-subtle` (#1E293B), 1px, dashed-1, opacity 0.5
- Time axis: bottom, `text-muted`, `text-2xs` mono, ticks every 4h on 1h interval
- Price axis: right, `text-muted`, `text-2xs` mono, ticks every $20 (auto-scaled)
- Crosshair: `text-secondary` 1px solid

**Candle layer** (the underlying chart):
- 1H interval, last 7 days = 168 candles by default, pageable backward
- Up: filled body `#22C55E`, wick `#16A34A`
- Down: filled body `#EF4444`, wick `#B91C1C`
- Volume bars below: 30% height, opacity 40% with `volume-up` / `volume-down`

**Grid level overlay** (this is what makes it OUR chart, not just any candle chart):
- One horizontal `priceLine` per grid level (93 levels for the reference bot)
- Color depends on level state:
  | State | Line color | Width | Style |
  |---|---|---|---|
  | Buy active | `#22C55E` | 1px | solid |
  | Sell active | `#EF4444` | 1px | solid |
  | Filled (gap) | `#475569` | 1px | dashed-2 |
  | Pending replace | `#F59E0B` | 1.5px | solid (with subtle pulse — 2s cycle) |
- Each line has a label on the right showing `$price` (mono, 11px)
- Hover on any line shows tooltip: "Level 47 · BUY · 0.0173 ETH @ $2,083.50 · Filled 3 times in last 30 days"

**Special markers**:
- Mark price (current): horizontal line, `primary` (#38BDF8), 2px solid, label "MARK $2,077.95" left side
- Avg entry: horizontal line, `text-secondary`, 1.5px dashed-3, label "ENTRY $2,102.94" left side
- Liquidation warning band: `danger-soft` background fill from `bg-base` up to `liq_price + (mark_price - liq_price) * 0.20`, opacity 0.4. So if mark is $2077 and liq is $1522, the band fills from $1522 to ~$1633. If price enters the band, the band's opacity ramps to 0.7 and the bot card pulses.

**Interactions**:
- Scroll wheel = zoom horizontally
- Drag = pan
- Right-click on a level = context menu ("Cancel this order", "Move level...", "Hide from chart")
- Keyboard: arrow keys pan, +/- zoom, Esc resets, `H` toggles all grid lines, `P` toggles mark price marker, `E` toggles entry marker

**Live updates via WebSocket**:
- New candle every 5s (from polling for now, WS feed in v2)
- Price tick: smooth crossfade on the mark price line color (no jitter)
- Order fill event: the affected grid level briefly flashes to `#FFFFFF` then back to its color over 600ms (one shot, not looping)

### 6.2 Equity curve (Bot Detail — secondary chart, also Overview cards)

**Library**: Recharts (`<AreaChart>`)

**Layout**:
- Aspect ratio: 21:9 (wide and short, sits below the GridChart)
- Background: `bg-elevated`
- Two series: equity (filled area) + balance (line only)
- Grid: horizontal lines only at 5 ticks, `border-subtle`
- X axis: dates, mono, `text-muted`, `text-2xs`
- Y axis: USDT values, mono, right-aligned
- Equity area fill: gradient from `#38BDF8` 30% opacity at top to 0% at bottom
- Equity line: `#38BDF8` 2px
- Balance line: `#94A3B8` 1.5px dashed-2
- Hover: vertical crosshair `text-secondary`, tooltip showing date + both values + delta from previous day

### 6.3 Equity sparkline (BotCard mini chart)

**Library**: Recharts (`<LineChart>` minimal config) OR a hand-rolled SVG (smaller bundle, ~30 lines)

**Layout**:
- Width: 100% of card content area, height: 40px
- No axes, no grid, no labels
- Single line, 1.5px solid
- Color: `success` if 7-day delta is positive, `danger` if negative
- Filled area below at 15% opacity, same color
- Last point: 3px dot in line color

### 6.4 Drawdown / Risk gauge (Bot Detail stats panel)

**Library**: hand-rolled SVG arc (it's a single arc, no library overhead)

**Layout**:
- Semicircle gauge, 180° from -90° to +90°, radius 60px
- Track: `bg-muted`, 8px stroke
- Fill: rotating from 0° to angle = `(current_drawdown / max_acceptable_drawdown) * 180°`
- Fill color thresholds:
  | Drawdown | Color |
  |---|---|
  | 0–10% | `success` |
  | 10–15% | `warning` |
  | 15–20% | `danger` |
  | >20% | `danger` + pulse (max-loss safeguard threshold) |
- Center label: drawdown % in mono, `text-3xl`, weight 700
- Below: "Drawdown" label in `text-2xs text-muted uppercase`

### 6.5 Grid activity heatmap (Bot Detail — optional, P1)

**Library**: hand-rolled SVG grid

**Layout**:
- Y axis: hours (0-23 UTC)
- X axis: last 14 days
- Each cell colored by number of fills in that hour
- Color scale: `bg-muted` (0 fills) → `primary-soft` → `primary` → `success` (10+ fills)
- Cell hover: tooltip "Mar 30, 14:00 UTC · 4 fills · $0.48 profit"

---

## 7. Wireframes

### 7.1 Overview page (desktop ≥1024px)

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ [≡ GRVT GRID]  Total Equity $1,160.55 ▲+9.15%  ●GRVT  🔔3  ☀/🌙  ⚙           │
├─────────┬────────────────────────────────────────────────────────────────────────┤
│         │  ╔══════════════════════════════════════════════════════════════════╗  │
│ ▣       │  ║                                                                  ║  │
│ Overview│  ║                       OVERVIEW                                   ║  │
│         │  ║                                                                  ║  │
│ ⬢ Bots  │  ║   Active bots: 1     ●Running    Total PnL ▲+$424.39 (+9.1%)    ║  │
│         │  ╚══════════════════════════════════════════════════════════════════╝  │
│ ⌬ History│                                                                       │
│         │  ┌───────────────────────────────┐  ┌─────────────────────────────┐   │
│ ⚙ Setting│  │ ETH-USDT-Perp · LONG · 10x   │  │  + CREATE NEW BOT          │   │
│         │  │ ●running                      │  │                            │   │
│         │  │ ─────────────────────────────  │  │  Launch a new grid bot    │   │
│         │  │ Equity      $1,160.55 ▲+9.15% │  │  with the wizard           │   │
│         │  │ ╱╲╱╲╱╲╱╲╱╲╱╲ (sparkline)      │  │                            │   │
│         │  │ Position    2.0 ETH @ $2,103  │  │  [+ NEW BOT]               │   │
│         │  │ Range       $1,800 — $2,450   │  │                            │   │
│         │  │ Grid        93 levels         │  └─────────────────────────────┘   │
│         │  │ Realized    +$424.39          │                                    │
│         │  │ Unrealized  -$42.03  (▼-3.8%) │                                    │
│         │  │ ─────────────────────────────  │                                    │
│         │  │ [▶ Resume]  [⏸ Pause]  [⋯]    │                                    │
│         │  └───────────────────────────────┘                                    │
│         │                                                                       │
│         │  ╔════════════ EQUITY CURVE — ALL BOTS ═════════════════════════════╗ │
│         │  ║                                                                  ║ │
│         │  ║         ╱╲   ╱╲                                          ╱╲     ║ │
│         │  ║        ╱  ╲ ╱  ╲   ╱╲                                ╱╲╱  ╲    ║ │
│         │  ║       ╱    ╳    ╲ ╱  ╲      ╱╲                   ╱╲╱        ╲  ║ │
│         │  ║      ╱             ╲    ╲╱╲╱  ╲                                  ║ │
│         │  ║  ╱╲╱                                                              ║ │
│         │  ║                                                                  ║ │
│         │  ║  Mar 5    Mar 12    Mar 19    Mar 26    Apr 2    Apr 7         ║ │
│         │  ╚══════════════════════════════════════════════════════════════════╝ │
│         │                                                                       │
└─────────┴───────────────────────────────────────────────────────────────────────┘
```

**Layout grid**:
- Sidebar: 224px fixed
- Main: fills remaining
- BotCard grid: `grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4`
- Equity curve at bottom: full width

**Mobile (<768px)**: sidebar collapses to bottom nav (Overview, Bots, History, Settings — 4 items, ≤5 limit). Header total stays at top. BotCards stack 1 column. Equity curve simplified.

### 7.2 Bot detail page (desktop)

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ [≡ GRVT GRID]  Total Equity $1,160.55 ▲+9.15%  ●GRVT  🔔3  ☀/🌙  ⚙           │
├─────────┬────────────────────────────────────────────────────────────────────────┤
│         │  ← BOTS / Bot 42 · ETH-USDT-Perp · LONG · 10x         [▶][⏸][⋯ Menu] │
│ ▣ Over  │  ●running       Created Mar 5  ·  Uptime 33d 2h                       │
│ ⬢ Bots ▶│                                                                       │
│ ⌬ Hist  │  ┌────────┬────────┬────────┬────────┬────────┬────────┐             │
│ ⚙ Set   │  │EQUITY  │REALIZED│UNREALIZD│POSITION│LIQ PRICE│DRWDWN │             │
│         │  │$1160.55│+$424.39│  -$42.03│2.0 ETH │$1522    │ 4.2%  │             │
│         │  │▲+9.15% │        │   ▼-3.8%│@$2103  │ -27%    │ ⌬gauge │             │
│         │  └────────┴────────┴────────┴────────┴────────┴────────┘             │
│         │                                                                       │
│         │  ╔══════════════════ GRID CHART ════════════════════════════════════╗ │
│         │  ║ ─────────────────────────────────────────────────────── $2450  ║ │
│         │  ║ ──────  sells (red)                                     $2400  ║ │
│         │  ║ ────────                                                $2350  ║ │
│         │  ║                       │  candles                        $2300  ║ │
│         │  ║ ─────────             │      │                          $2250  ║ │
│         │  ║                       │      │  │                       $2200  ║ │
│         │  ║ ────────         │ │ │█│ │  │█││ │                      $2150  ║ │
│         │  ║ ──── ENTRY ────  │█│█│█│█│█│ │█│█│█│ ──────  $2102.94 ◀──     ║ │
│         │  ║ ════ MARK ═══════ │█│ │█│█│█│█│ │█│█│ ════════ $2077.95 ◀════ ║ │
│         │  ║ ──── (gap)        │█│ │█│ │█│ │█│ │ │                  $2050  ║ │
│         │  ║ ──── buys (green) │ │   │ │ │ │ │   │                  $2000  ║ │
│         │  ║ ────────                                                $1950  ║ │
│         │  ║ ───── (warning band: liq $1522 + 20%)                  $1900  ║ │
│         │  ║ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ $1850  ║ │
│         │  ║ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ $1800  ║ │
│         │  ╚══════════════════════════════════════════════════════════════════╝ │
│         │  Mar 31    Apr 1    Apr 2    Apr 3    Apr 4    Apr 5    Apr 6    Apr 7│
│         │                                                                       │
│         │  ┌────────────── EQUITY CURVE ─────────────────┐ ┌── STATS PANEL ──┐│
│         │  │ ╱╲╱╲╱╲╱╲╱╲ (filled area, sky blue)         │ │ Win rate    87% ││
│         │  │  Mar 5         Apr 7                        │ │ Round trips 314 ││
│         │  └─────────────────────────────────────────────┘ │ Fees     -$0.78 ││
│         │                                                  │ Funding +$10.79 ││
│         │                                                  │ Avg/day   $1.81 ││
│         │                                                  └─────────────────┘ │
│         │                                                                       │
│         │  [Fills] [Orders] [Funding] [Snapshots]                              │
│         │  ┌──────────────────────────────────────────────────────────────────┐│
│         │  │ TIME       SIDE  PRICE       SIZE     FEE     RT-PROFIT          ││
│         │  │ 16:32 UTC  BUY   $2,077.95   0.0173   $0.018  +$0.121            ││
│         │  │ 16:18 UTC  SELL  $2,084.94   0.0173   $0.018  +$0.121            ││
│         │  │ 16:01 UTC  BUY   $2,077.95   0.0173   $0.018  +$0.121            ││
│         │  │ ...                                                               ││
│         │  │                                       [↓ Export CSV]              ││
│         │  └──────────────────────────────────────────────────────────────────┘│
└─────────┴───────────────────────────────────────────────────────────────────────┘
```

### 7.3 Create Bot Wizard (modal, 4 steps)

```
┌──────────────────────────────────────────────────────────────────┐
│  CREATE NEW BOT                                              [×] │
│  ──────────────────────────────────────────────────────────────  │
│  ●─────●─────○─────○                                             │
│  Pair  Range Config Confirm                                      │
│  ──────────────────────────────────────────────────────────────  │
│                                                                  │
│  STEP 1 — SELECT PAIR                                            │
│                                                                  │
│  ┌──────────────────────┬──────────────────────┐                │
│  │ ETH_USDT_Perp        │ BTC_USDT_Perp        │                │
│  │ $2,077.95  ▼-1.2%    │ $68,432.10 ▲+0.8%    │                │
│  │ Funding +0.012%/8h   │ Funding -0.005%/8h   │                │
│  │ Max leverage 50x     │ Max leverage 50x     │                │
│  │ Min size 0.001 ETH   │ Min size 0.001 BTC   │                │
│  │ [SELECTED]           │                      │                │
│  └──────────────────────┴──────────────────────┘                │
│                                                                  │
│  ⓘ More pairs coming. For now ETH and BTC perp are supported.   │
│                                                                  │
│  ──────────────────────────────────────────────────────────────  │
│  [Cancel]                                          [Continue →]  │
└──────────────────────────────────────────────────────────────────┘
```

```
STEP 2 — RANGE PICKER
┌──────────────────────────────────────────────────────────────────┐
│  Drag the handles to set your grid range. Current price $2,077  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ $2,500 ─────────────────────●═════════ ◀ upper $2,450   │    │
│  │                            ─                              │    │
│  │           ─────              candles                       │    │
│  │              ──    ───  ───                                │    │
│  │   $2,200 ──── ────────────── ◀ MARK $2,077 (current)      │    │
│  │              ──── ──                                       │    │
│  │   $2,000 ──    ──                                          │    │
│  │                                                            │    │
│  │ $1,800 ●═══════════════════════════════ ◀ lower $1,800     │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ⓘ Range $1,800 – $2,450 (32% wide). Current price IS inside.   │
│  ⚠ At 5% from upper bound. Consider widening upper.             │
│                                                                  │
│  [← Back]                                          [Continue →]  │
└──────────────────────────────────────────────────────────────────┘
```

```
STEP 3 — CONFIG
┌──────────────────────────────────────────────────────────────────┐
│  CAPITAL                                                         │
│  $ [    1085.00    ] USDT       Available: $327.78               │
│                                                                  │
│  LEVERAGE                                                        │
│  ●═══════════════════ 10 ────────────  (1x ─ 50x)               │
│  Effective notional: $10,850                                     │
│                                                                  │
│  GRID COUNT                                                      │
│  ─────●──── 93 levels      (2 ─ 95)                              │
│                                                                  │
│  ─── PREVIEW ────────────────────────────────────────────────    │
│  Spacing             $6.99 per level                             │
│  Quantity per level  0.0173 ETH                                  │
│  Profit per round-trip  ~$0.121                                  │
│  Estimated liquidation  $1,522 (▼ 27% from current)              │
│                                                                  │
│  ⚠ At 10x leverage, a 9% drop liquidates your position.         │
│                                                                  │
│  [← Back]                                          [Continue →]  │
└──────────────────────────────────────────────────────────────────┘
```

```
STEP 4 — CONFIRM
┌──────────────────────────────────────────────────────────────────┐
│  Review and launch                                               │
│                                                                  │
│  Pair         ETH_USDT_Perp                                      │
│  Direction    LONG                                               │
│  Leverage     10x                                                │
│  Range        $1,800 — $2,450                                    │
│  Grids        93                                                 │
│  Capital      $1,085 USDT                                        │
│  Bot will start in PAUSED state. Press Resume to begin trading. │
│                                                                  │
│  ⚠ This bot will use real funds and place 93 limit orders on    │
│  GRVT immediately upon resume. Liquidation at $1,522.            │
│                                                                  │
│  [ ] I understand the risks and accept liquidation up to $1085   │
│                                                                  │
│  [← Back]                                  [Create bot (paused)] │
└──────────────────────────────────────────────────────────────────┘
```

---

## 8. Component specs (the building blocks)

### 8.1 BotCard

**Used in**: Overview page

**Spec**:
- Container: `bg-elevated` background, `border-subtle` 1px, `rounded-lg`, padding `p-5`
- Hover state: `border-default`, no shadow change, no scale
- Active/clicked state: `border-primary`
- Width: 100% of grid cell, height: auto (~280px)
- Click anywhere → navigate to BotDetail; click on action buttons → action without navigation (event.stopPropagation)

**Internal layout** (top to bottom):
1. Header row (24px tall):
   - Pair text in `text-base font-semibold` (`Inter`)
   - Direction + leverage in `text-xs text-muted` (e.g. "LONG · 10x")
   - Status pill (right-aligned) — see G2
2. Spacing `mt-3`
3. Equity row (32px tall):
   - Label "EQUITY" in `text-2xs text-muted uppercase`
   - Value in `text-2xl font-mono font-semibold text-primary` (e.g. `$1,160.55`)
   - Day delta inline (e.g. `▲ +9.15%`) in `text-sm text-success` mono
4. Sparkline 40px tall (see §6.3)
5. Spacing `mt-4`
6. Stats grid: 2 columns × 3 rows of label/value pairs:
   ```
   POSITION    2.0 ETH @ $2,103
   RANGE       $1,800 — $2,450
   GRID        93 levels
   REALIZED    +$424.39
   UNREALIZED  -$42.03 ▼-3.8%
   FEES        -$0.78
   ```
   Labels: `text-2xs uppercase text-muted`
   Values: `text-xs font-mono text-primary` (semantic color where applicable)
7. Divider `border-subtle`
8. Action row (40px tall):
   - Primary action button (e.g. "Resume" if paused, "Pause" if running)
   - Overflow menu `⋯` for less-common actions

### 8.2 StatCard

Used in BotDetail header strip.

```
┌────────────┐
│ EQUITY     │  ← text-2xs uppercase text-muted
│ $1,160.55  │  ← text-2xl font-mono font-semibold text-primary
│ ▲+9.15%    │  ← text-sm font-mono text-success (or danger)
└────────────┘
```

- Width: 1/6 of header strip on desktop, 1/2 on mobile (stacked 2-col)
- Padding: `p-4`
- Background: `bg-elevated`
- Border: only between cards, not around (use `divide-x divide-border-subtle` on parent)
- Each card identical structure: label / big value / delta — variations are in formatting only

### 8.3 DataTable row

Used in Fills, Orders, Funding, Snapshots tabs.

- Row height: 36px (dense)
- Hover background: `bg-muted`
- Selected row: `bg-primary-soft` border-left `primary` 2px
- Cell padding: `px-3 py-2`
- Header row: `text-2xs uppercase text-muted font-semibold`, `bg-surface` (slightly distinct from elevated)
- Cell text: `text-xs` for data, `font-mono` for numbers, `font-sans` for labels
- Sortable columns: arrow icon next to header, clickable header
- Alignment: numbers right-aligned, labels/timestamps left-aligned, status pills centered

### 8.4 Modal

- Backdrop: `rgba(2, 6, 23, 0.7)` (= `bg-base` 70% opacity), no blur (we said no glassmorphism)
- Modal container: `bg-elevated`, `border-default`, `rounded-lg`, `shadow-lg`, max-width: 560px (regular) / 720px (wide for wizards)
- Header: `text-xl font-semibold`, padding `p-6`, border-bottom `border-subtle`
- Body: `p-6`
- Footer: `border-t border-subtle`, `p-4`, flex justify-end gap-3

### 8.5 Form controls

- Input height: 40px (`text-sm`, `px-3`)
- Input height mobile: 44px (touch target)
- Background: `bg-surface`
- Border: `border-subtle` 1px (focused: `border-primary` 2px), no focus ring (the border IS the ring)
- Radius: `rounded-md`
- Label: `text-2xs uppercase text-muted font-medium`, `mb-1.5`
- Helper text: `text-2xs text-muted`, `mt-1.5`
- Error text: `text-2xs text-danger`, `mt-1.5`
- Number inputs: `font-mono text-right` automatically
- Slider: track `bg-muted` 4px tall, fill `bg-primary` 4px, thumb 16px circle `bg-primary` `border-2 border-bg-base`

### 8.6 Empty state

- Illustration: NONE. No empty SVG illustrations. We use a simple icon (Lucide, 48px, `text-muted`)
- Title: `text-base font-semibold text-secondary`
- Body: `text-sm text-muted` max-width 320px
- CTA button if applicable
- Vertical center in container, padding `py-12`

### 8.7 Loading skeleton

- Background: `bg-muted`
- Shimmer: subtle `animate-pulse` (Tailwind built-in, opacity oscillation 0.5–1.0 over 2s)
- NO sliding shimmer animations — just opacity pulse
- Use for: bot cards while loading, table rows, chart placeholder
- For components that show numbers, show a `▓▓▓▓` dummy bar of the right WIDTH so layout doesn't shift on data arrival

### 8.8 Toast (sonner)

- Position: bottom-right desktop, top-center mobile
- Background: `bg-elevated`
- Border: `border-default` 1px, plus 2px left border in semantic color (success/danger/warning/info)
- Width: 360px max
- Padding: `p-4`
- Icon: Lucide 16px in semantic color
- Title: `text-sm font-medium`
- Description: `text-xs text-muted`
- Auto-dismiss: 4 seconds for info/success, 6 seconds for warning, 8 seconds for danger (errors), persistent for "action required" toasts
- Click toast → optional action (e.g. "View bot") or dismiss
- Critical: `aria-live="polite"` for normal toasts, `role="alert"` for errors

---

## 9. Animation / motion guidelines

**Principle**: motion exists to make state changes legible. Never to entertain. Every animation must answer "what state did this just transition from / to?".

### Timing

| Type | Duration | Easing |
|---|---|---|
| Color crossfade (price tick, status change) | 200ms | `ease-out` |
| Layout reflow (modal open, panel expand) | 250ms | `cubic-bezier(0.4, 0, 0.2, 1)` (Material standard) |
| Hover state | 150ms | `ease-out` |
| Page transition (route change) | 300ms (out) / 200ms (in) | `ease-in-out` |
| Skeleton pulse | 2000ms loop | `ease-in-out` |
| Status pill running indicator | 2000ms loop | `ease-in-out` |
| Fill flash | 600ms one-shot | `ease-out` |
| Drawdown warning pulse (active state) | 1500ms loop | `ease-in-out` |
| Drag handle (range picker) | tracks input, no easing | — |

### When NOT to animate

- **Numbers ticking**: do NOT animate the digit transition. Just replace the value. The mono font + tabular figures means there's no layout shift, so animation would only add noise. (The COLOR can crossfade for 200ms when the delta changes sign — green→red → bg crossfade only.)
- **Routine table row updates**: no animation. The data just appears.
- **First load**: no entry animations. The page just renders. (Stagger animations on first load are pretty for marketing pages and annoying for traders waiting to see their PnL.)
- **Reduced-motion users**: every animation above is suppressed via the `@media (prefers-reduced-motion: reduce)` rule in §4. Status pills become static. Fill flashes become a 1-frame highlight.

### When to animate

- **State change**: bot goes from running → paused. The pill morphs (color + icon swap) over 200ms. Cards' play button morphs into pause.
- **Fill event**: the affected grid level on the chart flashes white-tinted (1.0 → 0 opacity overlay) over 600ms once. The fills table gets a new row that briefly highlights with `primary-soft` background fading to default over 1500ms.
- **Modal open**: scale from 0.96 → 1.0 + opacity 0 → 1, 250ms. Backdrop fades 0 → 0.7 opacity.
- **Drawdown warning**: when current drawdown crosses 15%, the gauge fill smoothly transitions to warning color over 400ms, the gauge ring acquires a slow 1500ms pulse. At 20%+ the pulse becomes 800ms and the bot card's left border turns danger.
- **Connection loss to GRVT**: the connection pill in the header smoothly transitions from green ● to amber ● + spinner over 200ms.

### Performance rules

- Animate only `transform` and `opacity`. Never `width`, `height`, `top`, `left`.
- Anything looping must be CSS, never JS — keep the main thread free for chart rendering and WebSocket processing.
- Charts have their OWN animation system (TradingView Lightweight Charts animates internally, Recharts has `isAnimationActive`). For real-time data updates, set `isAnimationActive={false}` to avoid double animation.

---

## 10. Mobile breakpoints

```
sm:  640px   (large phones in landscape)
md:  768px   (tablets portrait)
lg:  1024px  (tablets landscape, small laptops)
xl:  1280px  (desktops)
2xl: 1536px  (large desktops)
```

### What changes at each breakpoint

| Element | <640px | 640-768 | 768-1024 | 1024+ |
|---|---|---|---|---|
| **Layout** | single column | single col | single col | sidebar + main |
| **Sidebar** | hidden (use bottom nav) | hidden | hidden | 224px fixed |
| **Bottom nav** | shown | shown | shown | hidden |
| **Header** | compact (logo + equity + 1 icon) | compact + theme toggle | full | full |
| **BotCard grid** | 1 col | 1 col | 2 col | 2 col → 3 col at xl |
| **Stat strip** | 2x3 grid stacked | 2x3 grid | 3x2 grid | 6x1 row |
| **GridChart aspect** | 4:3 | 4:3 | 16:9 | 16:9 |
| **GridChart axes** | hide right axis labels (use tooltip) | hide | show | show |
| **Modal** | full-screen sheet | full-screen sheet | centered modal max-560px | centered |
| **Tables** | horizontal scroll OR card list | h-scroll | h-scroll | full table |
| **Form labels** | label above input | above | above | above (consistency) |
| **Inputs** | 44px tall | 44px | 40px | 40px |
| **Tap targets** | 44px min | 44px | 32px ok | 32px ok |
| **Font sizes** | base 14px (inputs 16px to avoid iOS zoom) | 14px | 14px | 14px |

### Mobile-specific patterns

- **Bottom nav**: 4 items max — Overview, Bots, History, Settings. Icons + labels.
- **Sheets instead of modals**: confirmation dialogs and the create wizard become bottom sheets that slide up. Swipe down to dismiss.
- **Long press on a BotCard** opens a context menu (start/pause/stop/duplicate/delete) instead of inline action buttons.
- **Grid chart pinch-zoom + drag**, no hover tooltip, tap a level → bottom sheet with that level's details.
- **Tables become card lists**: each row becomes a small card with the most-important fields visible and `Tap to expand` for the rest.
- **Pull-to-refresh** on Overview → refetch all bots from the API (with WebSocket also delivering updates, this is mostly a confidence gesture).
- **Safe areas**: respected via `env(safe-area-inset-*)` on the bottom nav and any fixed bars.

---

## 11. Accessibility notes

### Contrast (WCAG AA minimum, AAA where possible)

The dark palette in §2 was tested. All combinations meet AA minimum (4.5:1 for body text, 3:1 for large text/UI). Most meet AAA (7:1):

| Combination | Ratio | Grade |
|---|---|---|
| `text-primary` on `bg-base` | 17.4:1 | AAA |
| `text-secondary` on `bg-base` | 12.6:1 | AAA |
| `text-muted` on `bg-base` | 7.2:1 | AAA |
| `text-disabled` on `bg-base` | 4.5:1 | AA |
| `success` on `bg-base` | 10.4:1 | AAA |
| `danger` on `bg-base` | 5.9:1 | AA+ |
| `warning` on `bg-base` | 11.2:1 | AAA |
| `primary` on `bg-base` | 9.0:1 | AAA |
| `text-primary` on `bg-elevated` | 16.0:1 | AAA |

### Focus rings

Default browser focus rings are removed. Replaced with **`box-shadow: 0 0 0 2px var(--color-primary)`** on focus-visible. ALL interactive elements (buttons, links, inputs, sliders, cards if clickable, tabs, table rows if sortable) have a visible focus ring. The focus ring is the SAME color (primary sky) and the SAME width (2px) everywhere — consistency reduces cognitive load.

### Keyboard navigation patterns

The dashboard is **fully keyboard-navigable**. Traders use keyboard.

| Key | Action |
|---|---|
| `Tab` / `Shift+Tab` | Move between focusable elements in DOM order (which matches visual order) |
| `Enter` / `Space` | Activate the focused element |
| `Esc` | Close modal, dismiss tooltip, cancel current action |
| `g o` | Go to Overview |
| `g b` | Go to Bots list |
| `g h` | Go to History |
| `g s` | Go to Settings |
| `n b` | New bot (open wizard) |
| `?` | Show keyboard shortcut help modal |
| `t` | Toggle theme |
| `/` | Focus search (in bot list) |

**On the GridChart** (focused via Tab):
| Key | Action |
|---|---|
| `←` `→` | Pan the chart |
| `↑` `↓` | Zoom out / in |
| `Home` | Reset to default view |
| `g` | Toggle grid lines visibility |
| `m` | Toggle mark price marker |
| `e` | Toggle entry price marker |
| `Esc` | Unfocus chart (return to page tab order) |

### Screen reader support

- All charts have an `aria-label` summary (e.g. "Grid bot for ETH-USDT-Perp, 93 levels, current price $2,077.95, last fill 16:32 UTC")
- All charts have a "View as table" toggle that reveals a `<table>` with the same data
- Status pills have `aria-label` like "Bot 42 status: running"
- Form inputs have proper `<label for="">` associations
- Toast container has `aria-live="polite"` (and `role="alert"` for errors)
- Decorative icons (anything from Lucide that just illustrates) get `aria-hidden="true"`
- Numeric values that update get `aria-live="off"` by default to avoid spam — only critical alerts (e.g. drawdown > 15%) get `aria-live="assertive"`

### Don't break system features

- Pinch-zoom: NOT disabled (no `user-scalable=no` in viewport meta)
- Browser back button: works correctly via React Router state
- Browser refresh: re-fetches state, doesn't lose context
- Right-click: not hijacked (we have our own context menu on the chart but it coexists with browser default elsewhere)
- Browser zoom up to 200%: layout doesn't break (relative units, not fixed px containers)

---

## 12. Rejected alternatives (decision log)

This section exists so future-me doesn't second-guess. Each rejected option has its reasoning.

### Style alternatives considered

- **"Bitcoin DeFi Mobile" (the search result default)**: rejected because it leans into glassmorphism, blur, and "holographic" effects. The user explicitly said no neon-overload, and blur effects make data labels harder to read on price charts.
- **"Cyberpunk Mobile (Orbitron + JetBrains Mono)"**: rejected. Orbitron at H1 size with letterSpacing 4 is illegible in dense layouts. The user said "NOT cyberpunk".
- **Notion-style soft UI**: rejected. Too soft for trading. Lack of visual hierarchy in monochromatic gray makes critical PnL hard to spot.
- **Material Design 3 (Roboto)**: rejected. Material's elevation and ripple system don't match the flat, dense Bloomberg-terminal aesthetic. Roboto is also not ideal for tabular numbers.
- **Bootstrap-default look**: rejected. Generic. The user wants a product that LOOKS professional, not the default of every framework.

### Color alternatives considered

- **Pure black bg (#000000)**: rejected. Pure black on OLED looks great but on LCD shows banding artifacts and reduces depth perception. `#020617` (deep slate) reads as black to users while having actual color depth.
- **Hyperliquid green-on-black palette (where green IS the primary)**: rejected. Too much green causes confusion with the "buy / profit" semantic. We use sky as primary and reserve green for buy/profit.
- **Phantom purple primary**: rejected. Purple is the "AI startup" color and feels like a 2023 SaaS landing page. Not crypto-native trading.
- **GRVT brand orange (#F97316) as primary**: rejected. Orange would conflate the dashboard with the exchange. Also too close to amber (warning) — risk of meaning collision.

### Typography alternatives considered

- **Fira Code as the mono**: rejected (despite being a great font) because Fira Code defaults to ligatures (`=>`, `!=`, `>=`) which look weird in raw numbers. JetBrains Mono with `calt: 0` is cleaner.
- **Geist Mono**: considered. It's beautiful but adds another font family to load. We're already loading Inter + JetBrains Mono and don't need a 3rd. Maybe in v2.
- **System mono only (SF Mono / Cascadia Code)**: rejected because it's inconsistent across platforms and doesn't have guaranteed tabular figures.
- **Inter Display for headlines**: considered. Adds 30KB. Regular Inter at weight 700 is good enough for the few hero headings we have. Skipped.
- **Single font for everything (e.g. Inter only, no mono)**: rejected. Numeric tables MUST be mono. Layout shift on price ticks is a deal-breaker.

### Library alternatives considered (charts)

- **Chart.js for the GridChart**: rejected. Doesn't natively support price line overlays or candlesticks well; would require a plugin maze.
- **ApexCharts**: rejected. Heavier than Lightweight Charts (180KB vs 14KB) and slower with 1000+ candles.
- **Plotly.js**: rejected. 3MB. Insane.
- **D3 from scratch**: rejected. Would take 2 weeks to build a candle chart that works as well as Lightweight Charts in 2 hours.
- **ECharts**: rejected. 85KB and the candlestick component is awkward.
- **TradingView Advanced Chart Library (free for non-commercial)**: rejected because licensing is unclear for the "self-host + community" model and it requires their iframe widget which we can't fully style.

### Pattern alternatives considered

- **Single-page sticky scroll dashboard (everything on one page)**: rejected. Doesn't scale to multi-bot. Multi-page with router gives us deep linking and per-bot URLs which traders WILL share with each other.
- **No sidebar, only top nav**: rejected. Sidebar is more efficient for desktop traders who use the dashboard for hours.
- **Tabs instead of side nav**: rejected because we want bot detail to be a full route, not a tab inside Overview.

---

## Done. This is the law.

Every PR that touches the dashboard must verify: am I using a color from §2? A font from §3? Does this match a wireframe in §7? If not — update this doc first, then code.

Next steps from here:
1. Create the `packages/dashboard/` Vite project with the Tailwind config from §4
2. Build the design tokens as TypeScript constants in `packages/dashboard/src/design/tokens.ts`
3. Build the foundation components in `packages/dashboard/src/components/ui/` (Button, Badge, Card, Input, Modal, Toast, StatusPill)
4. Build the layout shell (Sidebar, BottomNav, Header)
5. Build the data components (BotCard, StatCard, DataTable)
6. Build the GridChart (the hero — its own day or two)
7. Build the wizard
8. Wire everything to the backend WebSocket and v2 REST endpoints
