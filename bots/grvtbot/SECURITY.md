# Security Policy

This document is for both users of the hosted instance at
[grvtbot.com](https://grvtbot.com) and developers self-hosting their own copy.

## What this software does with your data

When you create an account on a GRVT Grid instance and connect your GRVT
API credentials, the bot stores:

- Your email address.
- A **bcrypt** hash (cost factor 12) of your password — the plaintext is
  never written to disk, never logged, never recoverable. Even with full
  database access, an attacker would have to brute-force each password
  individually at ~10 hashes/sec/core.
- Your GRVT API key, API secret, trading address, account ID and
  sub-account ID, **encrypted at rest with AES-256-GCM**. Each row has a
  fresh random 12-byte IV, and the GCM auth tag is verified on every
  decrypt — silent tampering is detected and aborts.

The encryption uses a 32-byte master key stored on disk at
`/etc/grvt-grid/master.key` (or `$MASTER_KEY_PATH`), owned by the bot
process user with file permissions `0600`.

## What the encryption protects against

- **Database theft**: a stolen copy of `grid_bot.db` is useless without
  `master.key`.
- **Backup leaks**: same — backups are only at-rest ciphertext.
- **Casual snooping**: nobody can `sqlite3` into the file and read your
  credentials in plain text.
- **Memory tampering**: GCM auth tag prevents an attacker from flipping
  bits in the ciphertext to forge a valid decrypt.

## What the encryption does NOT protect against

- **A compromised or malicious operator.** The operator of any hosted
  instance has root access to the server, which means they have read
  access to `master.key`, which means they can decrypt any stored
  credential at any time. **This is a fundamental limit of
  server-side multi-tenant hosting**, not a flaw in the implementation.
- **Government or legal compulsion** against the operator.
- **Full root compromise** of the host.

If you do not want any third party to have technical access to your GRVT
credentials, **self-host your own instance**. The bot is AGPL-3.0
licensed (see [LICENSE](LICENSE)). The setup is straightforward — see
[docs/INSTALL.md](docs/INSTALL.md).

## Threat model summary

| Threat | Mitigated by | Residual risk |
|---|---|---|
| Stolen DB backup | AES-256-GCM at rest | None if master key not also stolen |
| Brute-force password guessing | bcrypt cost 12 | Slow but not infinite |
| Brute-force login API | `express-rate-limit` (5/15min per IP) | Only mass-distributed attacks |
| Credential stuffing of leaked passwords | Same rate limit | Same |
| SQL injection | Parameterized queries throughout | None known |
| XSS in dashboard | React's default escaping + helmet CSP | Audit-level only |
| Clickjacking | `X-Frame-Options: SAMEORIGIN` + COOP | None |
| Host-header / reset-link spoofing | `APP_BASE_URL` required, no Host fallback | None |
| Cross-tenant data leak (alerts, WS) | Per-user filter in router + WS ownership gate | None known |
| Operator reading user credentials | **NOT mitigated** by design | Mitigate by self-hosting |
| Lost master key | None — backups are useless without it | Operator must back up `master.key` offline |

## Auth & session

- Passwords: bcrypt cost 12, minimum 8 chars (the bot rejects shorter).
- Sessions: HS256 JWT signed with `JWT_SECRET` (≥32 chars enforced),
  24h expiry, issuer-pinned, algorithm-pinned. No refresh tokens — users
  re-login after 24h.
- Password reset: SHA-256-hashed tokens stored in the DB, 1h TTL,
  single-use, any new request invalidates older open tokens.
- The `/auth/forgot-password` endpoint is enumeration-safe: it returns
  the same `{"ok":true}` whether the email is registered or not.

## Network hardening

- All connections served behind TLS via Let's Encrypt (managed by a
  Caddy reverse proxy in the reference deployment).
- HTTP→HTTPS redirect, HSTS with 1-year `max-age` and `includeSubdomains`.
- Security headers via `helmet`: `X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`, Cross-Origin-Opener/Resource policy.
- Prometheus `/api/v2/metrics` is gated either by `METRICS_TOKEN` header
  or localhost-only — never exposed to the public internet.
- The bot itself only listens on `localhost:3848` in the reference
  deployment; the proxy bridges to it.

## Reporting a vulnerability

If you find a security issue, **do not open a public GitHub issue**.

Email: `security@grvtbot.com` (or fall back to opening a *private*
security advisory via GitHub's "Report a vulnerability" UI on the repo's
Security tab).

We aim to acknowledge within 48h. If the issue is critical and affects
the hosted instance, we will patch and re-deploy first, then publish a
postmortem after users are safe.

## Out of scope

- Issues that require physical access to the server.
- Issues that require the user to install malicious browser extensions.
- DoS by overwhelming the application layer (the reverse proxy handles
  network-layer DoS).
- Issues in third-party services (GRVT itself, the SMTP provider, the
  hosting provider).

## Disclosed advisories

We disclose security-impacting fixes here after the hosted instance is
patched, so self-hosters can decide whether their deployment is affected
and pull the fix. Severity reflects worst-case impact on the hosted
instance at grvtbot.com; self-hosted instances may face different
exposure depending on their topology.

### 2026-05-28 — Dashboard API key exposed in client bundle (Critical)

**Commit:** [`4631ba9`](https://github.com/kmanus88/GRVTBot/commit/4631ba9).
**Scope:** hosted instance affected; self-hosters affected if they kept
the (now-removed) `VITE_DASHBOARD_API_KEY` env var in their dashboard
build env.

The `VITE_DASHBOARD_API_KEY` variable was inlined into the production
JavaScript bundle and served publicly under `/dashboard/assets/*.js`.
Any browser visiting the site could extract the key from the bundle
and reach every `/api/v2/*` endpoint authenticated as the operator
account (`user_id = 1`, admin), bypassing per-tenant scoping.

Encrypted GRVT credentials remained safe — no endpoint returns plaintext
api-secrets — but an attacker could read every user's bot data and
trigger bot lifecycle actions (start / pause / close / update-range)
against the operator's GRVT sub-account.

**Fix:** the legacy `X-Api-Key` fallback was removed from the dashboard.
Browser auth is now JWT-only; the WebSocket also moved to `?token=<jwt>`.
The server still accepts `X-Api-Key` for operator scripts (curl, admin
tooling) — that side is unchanged. The shared key on the production VPS
was rotated in place. The compromised value never appeared in git
history.

Reported by [@ijromeo](https://instagram.com/ijromeo) (Instagram DM,
responsible disclosure). Thank you.

### 2026-05-28 — Bot close can leave orders + position open on GRVT (High)

**Commit:** [`6331317`](https://github.com/kmanus88/GRVTBot/commit/6331317).
**Scope:** all instances; impact is financial (drift on the user's own
GRVT account), not a tenant-isolation break.

`pauseBot()` and `closeBot()` in the grid engine cancelled open orders
through the in-memory bot instance. If that instance was missing
(engine restart race, previously-paused bot, or any path that removed
it early), the cancel was skipped silently and the DB was still updated
to `paused` / `stopped`. Surviving limit orders kept matching against
price moves, drifting the position for hours before a user noticed.

`closeBot()` additionally placed a single 0.5%-aggressive GTC limit to
close any open position and never verified the fill, so a fast price
move could leave the position partially or fully open with the DB
already marked `stopped`.

**Fix:** both functions now always cancel via the owner's GRVT client
against the pair (independent of the in-memory map), and the position
close retries with escalating slippage (0.5% / 2% / 5%) up to three
attempts, re-reading the live position each time. A final
cancel-all sweep clears any unfilled close-order tail.

Found via live incident review on 2026-05-28 (no external reporter).
Production residue was cleaned up before the patch shipped.
