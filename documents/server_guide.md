# Server Guide — Aliyun Singapore

## What's running on the server

Think of the server as a Mac that never sleeps, sitting in Singapore, running your trading bot 24/7 even when your laptop is closed.

**Three things are running permanently:**
- **Trading engine** — the Python bot watching BTC/ETH prices and placing orders on OKX demo
- **Dashboard** — a web page showing your positions and PnL, accessible from any browser
- **nginx** — a gatekeeper that puts the dashboard on port 80 and asks for a password

**What's different from your Mac:**
- On your Mac, closing the terminal kills the bot. On the server, it runs as a system service (like how WiFi keeps working after you close Settings)
- Orders go to OKX via the same `okx` CLI, same demo account, same credentials — just from Singapore instead of your Mac
- The code lives in `/opt/quant_trade_competition/` on the server, mirrored from GitHub

---

## Access from any machine

```bash
ssh -i ~/.ssh/okx-trade-challange.pem root@43.98.248.255
```

If you're on a different machine without the `.pem` file, copy it over first:
```bash
scp ~/.ssh/okx-trade-challange.pem user@other-machine:~/.ssh/
```

---

## Check status

```bash
# Is the engine running?
systemctl status trading-engine

# Live log stream (Ctrl+C to stop)
tail -f /opt/quant_trade_competition/engine/logs/elite_flow.log

# Current position & PnL snapshot
cat /opt/quant_trade_competition/engine/logs/summary.json

# Dashboard status
systemctl status trading-dashboard
```

---

## Start / Stop engine

```bash
# Stop
systemctl stop trading-engine

# Start
systemctl start trading-engine

# Restart (after config changes)
systemctl restart trading-engine
```

---

## Make code changes (the right workflow)

**On your Mac** — edit files, then:
```bash
cd /Users/lucaslee/quant_trade_competition
git add <specific file>
git commit -m "what you changed"
git push
```

**On the server** — pull and restart:
```bash
cd /opt/quant_trade_competition
git pull
systemctl restart trading-engine
```

---

## Change strategy or settings

The only file that controls what the engine runs is:
```
deploy/trading-engine.service
```

Edit it on your Mac, push, pull on server, restart. That's it.

Current config: `elite_flow` strategy, hourly profile, 300 USDT capital, demo mode.

---

## Dashboard from phone or browser

URL: `http://43.98.248.255`
Login: `trader` / `p0ssword!23`

---

## Key file locations on server

| What | Where |
|---|---|
| Code | `/opt/quant_trade_competition/` |
| Engine logs | `/opt/quant_trade_competition/engine/logs/` |
| System logs | `/var/log/trading/engine.log` |
| OKX credentials | `/root/.okx/config.toml` |
| systemd service | `/etc/systemd/system/trading-engine.service` |
| nginx config | `/etc/nginx/sites-enabled/trading-dashboard` |
