# Deploy: Oracle Cloud (free) + Supabase

Host the Pump Reader bot 24/7 on an Oracle Cloud **Always Free** VM, with
Supabase (free Postgres) as the database. Market data stays real (public CCXT);
real-money trading and real balance need your own exchange keys.

## 0. What is real vs. what needs your keys

| Data | Source | Needs your keys? |
|------|--------|------------------|
| Prices, volume, orderbook, OHLCV, sparklines | public CCXT (Binance/MEXC/Bitget) | No |
| Pump score, clusters, classification | computed from the above | No |
| Volume-acceleration trigger | public CCXT | No |
| FDV / Market cap / supply | CoinGecko (free) | No |
| Candidate Timeline / depth / inflows | public CCXT | No |
| **Your account balance** | exchange private API | **Yes — read-only key** |
| **Real-money buy/sell** | exchange private API | **Yes — spot key, no withdrawal** |
| Holder concentration / CEX deposit flows | on-chain provider | provider key (not wired) |

Paper mode needs nothing. Everything below is for going live + persistent.

## 1. Supabase (database)

1. Create a project at supabase.com (free tier).
2. Open **SQL Editor**, paste `infrastructure/supabase/schema.sql`, run it.
   Re-running is safe (idempotent).
3. **Settings → API**: copy `Project URL` and the **`service_role`** key.
4. Put them in `.env`:
   ```
   SUPABASE_URL=https://xxxx.supabase.co
   SUPABASE_SERVICE_KEY=eyJ... (service_role — keep secret)
   ```
The bot writes with the service key (bypasses RLS). The browser never touches
Supabase, so the tables stay locked to the backend. Without these vars the bot
runs fine in-memory.

## 2. Oracle Cloud Always Free VM

1. Create an **Always Free** compute instance (Ubuntu 22.04, Ampere A1 is fine).
2. Open the firewall for the app port (default 8000):
   - Oracle console: VCN → Security List → add Ingress `0.0.0.0/0` TCP 8000.
   - On the VM: `sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT && sudo netfilter-persistent save`
3. Install Python + clone:
   ```bash
   sudo apt update && sudo apt install -y python3-venv git
   git clone <your-repo> tradeos && cd tradeos/apps/pump-reader
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   ```
4. Create `.env` from `.env.example` (at repo root or export the vars), fill
   Supabase (+ exchange keys only when you choose to go live).

## 3. Run it as a service (stays up on reboot)

`/etc/systemd/system/pumpreader.service`:
```ini
[Unit]
Description=TradeOS Pump Reader
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/tradeos/apps/pump-reader
EnvironmentFile=/home/ubuntu/tradeos/.env
ExecStart=/home/ubuntu/tradeos/apps/pump-reader/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
User=ubuntu

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pumpreader
sudo systemctl status pumpreader
```
Open `http://<vm-public-ip>:8000`. Put it behind Caddy/Nginx + HTTPS before
exposing publicly.

## 4. Going live with real money (only when ready)

1. On each exchange create an API key: **spot trading enabled, withdrawal
   DISABLED**, optionally IP-restricted to the VM.
2. In `.env` set the keys (`MEXC_API_KEY`/`MEXC_SECRET`, `BITGET_*`, …). The
   dashboard balance turns real immediately (read-only) — verify it matches.
3. Only after that, set `PUMP_EXEC_MODE=live`. Start tiny (`PUMP_AUTO_ENTRY_USD`
   small). The kill switch (Settings tab) and RiskGuard still apply.

KManuS88 himself says the signal is unvalidated — paper-test 7–30 days first.
