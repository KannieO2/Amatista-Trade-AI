#!/usr/bin/env bash
# Put TradeOS behind HTTPS with Caddy + a free DuckDNS subdomain, so the login
# password never travels in clear text. Run AFTER oracle-setup.sh, once the VM
# has a public IP and you have created a DuckDNS subdomain pointing at it.
#
#   bash deploy/setup_https.sh <full-domain> [duckdns-subdomain] [duckdns-token]
#
# Examples:
#   # domain already points at this VM (you set the IP in the DuckDNS UI):
#   bash deploy/setup_https.sh sagrado-bot.duckdns.org
#   # ...or let this box keep the DuckDNS record fresh automatically:
#   bash deploy/setup_https.sh sagrado-bot.duckdns.org sagrado-bot 1234abcd-your-token
#
# After this runs, open  https://<full-domain>  (port 8000 is closed to the
# public; Caddy on 443 reverse-proxies to the bot on 127.0.0.1:8000).
set -euo pipefail

DOMAIN="${1:?usage: setup_https.sh <full-domain> [duckdns-subdomain] [duckdns-token]}"
DUCK_SUB="${2:-}"
DUCK_TOKEN="${3:-}"
APP_PORT="${APP_PORT:-8000}"

echo "==> [1/5] Install Caddy (official apt repo)"
if ! command -v caddy >/dev/null 2>&1; then
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y caddy
fi
echo "    caddy $(caddy version 2>/dev/null | head -n1)"

echo "==> [2/5] DuckDNS auto-refresh (keep the subdomain pointed at this VM)"
if [ -n "$DUCK_SUB" ] && [ -n "$DUCK_TOKEN" ]; then
  # Push once now, then every 5 min via cron. ip= empty => DuckDNS uses caller IP.
  curl -fsS "https://www.duckdns.org/update?domains=$DUCK_SUB&token=$DUCK_TOKEN&ip=" || true
  CRON_LINE="*/5 * * * * curl -fsS \"https://www.duckdns.org/update?domains=$DUCK_SUB&token=$DUCK_TOKEN&ip=\" >/dev/null 2>&1"
  ( crontab -l 2>/dev/null | grep -v "duckdns.org/update?domains=$DUCK_SUB"; echo "$CRON_LINE" ) | crontab -
  echo "    DuckDNS refresh cron installed for $DUCK_SUB"
else
  echo "    (no DuckDNS token given — make sure $DOMAIN already resolves to this VM's IP)"
fi

echo "==> [3/5] Caddyfile: HTTPS $DOMAIN -> 127.0.0.1:$APP_PORT"
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
$DOMAIN {
    encode gzip zstd
    reverse_proxy 127.0.0.1:$APP_PORT
}
CADDY

echo "==> [4/5] Firewall: open 80 + 443, close public $APP_PORT"
for p in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$p" -j ACCEPT 2>/dev/null \
    || sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$p" -j ACCEPT || true
done
# Drop the public rule for the app port (Caddy reaches it on localhost anyway).
sudo iptables -D INPUT -m state --state NEW -p tcp --dport "$APP_PORT" -j ACCEPT 2>/dev/null || true
if command -v netfilter-persistent >/dev/null 2>&1; then sudo netfilter-persistent save || true; fi

echo "==> [5/5] Start Caddy (auto Let's Encrypt cert on first request)"
sudo systemctl enable --now caddy
sudo systemctl reload caddy || sudo systemctl restart caddy
sleep 3
sudo systemctl --no-pager status caddy | head -n 8 || true

echo
echo "================================================================"
echo " HTTPS ready.  Open:  https://$DOMAIN"
echo " (first load may take ~15s while Let's Encrypt issues the cert)"
echo " Caddy logs:  sudo journalctl -u caddy -f"
echo "================================================================"
