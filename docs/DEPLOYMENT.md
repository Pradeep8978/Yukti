# Yukti Deployment Guide

Deploy to DigitalOcean, AWS, or your own VM in 20 minutes.

## Prerequisites

- Fresh Ubuntu 22.04+ VM (1GB RAM minimum, 2GB+ recommended)
- 10-20GB disk space (mostly for PostgreSQL candle history)
- DhanHQ account with API credentials
- Gemini or Claude API key
- Telegram bot token + chat ID
- Git installed

## Option 1: DigitalOcean Droplet (recommended)

### 1a. Create a Droplet

```
$ doctl compute droplet create yukti \
    --region blr1 \                    # Bangalore (lowest NSE latency)
    --size s-1vcpu-1gb \                # 1 vCPU, 1GB RAM, $6/mo
    --image ubuntu-22-04-x64 \
    --enable-monitoring \
    --wait
```

Or use the web UI: https://cloud.digitalocean.com/droplets/new

Choose:
- **Image:** Ubuntu 22.04 x64
- **Region:** BLR1 (Bangalore — closest to NSE)
- **Size:** Basic, $6/month (1GB RAM, 1vCPU, 25GB SSD)
- **Monitoring:** Enable
- **Backups:** Enable ($1.20/month — worth it)

### 1b. SSH and bootstrap

```bash
# Get the IP from DigitalOcean console
ssh root@YOUR_DROPLET_IP

# Verify Ubuntu version
lsb_release -a

# Update
apt update && apt upgrade -y

# Install Docker and Docker Compose
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh

# Install git and other essentials
apt install -y git curl wget build-essential

# Verify
docker --version
docker compose --version
```

### 1c. Clone Yukti and configure

```bash
cd /opt
git clone https://github.com/YOUR_USERNAME/yukti.git
cd yukti

# Copy template env
cp .env.example .env

# Edit with your credentials
nano .env
```

Edit these lines:
```env
DHAN_CLIENT_ID=your_dhan_client_id
DHAN_ACCESS_TOKEN=your_dhan_api_token
GEMINI_API_KEY=your_gemini_api_key    # from https://aistudio.google.com/app/apikey
ANTHROPIC_API_KEY=sk-ant-xxx           # if using Claude
VOYAGE_API_KEY=your_voyage_key         # from https://www.voyageai.com
TELEGRAM_BOT_TOKEN=123456:AAxxxxx      # from @BotFather
TELEGRAM_CHAT_ID=your_chat_id          # from @userinfobot
POSTGRES_PASSWORD=choose_a_strong_password
MODE=paper                             # start in paper mode!
ACCOUNT_VALUE=500000
WATCHLIST=RELIANCE,HDFCBANK,INFY,TCS,ICICIBANK
```

### 1d. Start the stack

```bash
# Build and start all services
docker compose up -d

# Watch the logs
docker compose logs -f yukti
```

Expected output after ~30 seconds:
```
yukti          | ============================================================
yukti          | YUKTI (युक्ति) — Autonomous NSE Trading Agent
yukti          | Mode:           PAPER
yukti          | AI provider:    GEMINI
yukti          | Account:        ₹500,000
yukti          | Risk per trade: 1.0%
yukti          | Candle:         5 min
yukti          | ============================================================
```

### 1e. Access the dashboard

Open your browser:
- **Web portal:** http://YOUR_DROPLET_IP:8000
- **Grafana:** http://YOUR_DROPLET_IP:3000 (admin/admin by default)

If you can't see the pages, wait 10 seconds and refresh — the React build takes time on first startup.

---

## Option 2: Your own VM or laptop

```bash
# Prerequisites: Python 3.11+, Docker, Docker Compose
docker --version
docker compose --version

# Clone
git clone https://github.com/YOUR_USERNAME/yukti.git
cd yukti

# Configure
cp .env.example .env
# Edit .env with credentials
nano .env

# Start
docker compose up -d

# Tail logs
docker compose logs -f yukti
```

---

## Post-deployment setup

### Load the watchlist

```bash
# SSH into the container
docker compose exec yukti bash

# Download watchlist from NSE (or use sample)
python scripts/universe_loader.py --sample

# Verify
cat universe.json
exit
```

### (Optional) Backfill historical candles for backtesting

This takes 5-10 minutes and fetches 2 years of 5-minute candles.

```bash
docker compose exec yukti python scripts/backfill_candles.py --days 730
```

You'll see:
```
Backfilling 5 symbols × 730 days (5-minute candles)
  RELIANCE: fetching 2023-01-15 → 2023-02-04 — inserted 600 candles
  ...
Backfill complete — 180,000 total candles inserted
```

### Set up monitoring

Open Grafana at http://YOUR_IP:3000

1. Login with admin / admin
2. Add data source → Prometheus → URL = http://prometheus:9090
3. Import dashboard → upload deploy/grafana/dashboards/yukti.json
4. Watch the metrics as the agent trades

---

## Daily operations

### Check agent status

```bash
# All containers running?
docker compose ps

# Agent logs
docker compose logs yukti --tail 100

# Realtime
docker compose logs -f yukti

# Check specific time
docker compose logs yukti --since 10m
```

### Control the agent

Use the web portal at http://YOUR_IP:8000:

- **Halt:** Freeze all new trades (kill switch)
- **Resume:** Resume trading
- **Squareoff:** Close all open positions at market
- **Dashboard:** Live P&L, equity curve, positions

Or via Telegram (if bot is configured):

```
/halt
/resume
/squareoff
/status
/positions
/pnl
```

### Update code from GitHub

```bash
cd /opt/yukti

# Pull latest code
git pull origin main

# Rebuild and restart
docker compose down
docker compose up -d

# Watch startup
docker compose logs -f yukti
```

### Inspect databases

#### PostgreSQL (trades, journal, candles)

```bash
docker compose exec postgres psql -U yukti -d yukti -c "\dt"

# List tables
docker compose exec postgres psql -U yukti -d yukti -c "SELECT * FROM trades ORDER BY opened_at DESC LIMIT 5;"

# Check candle coverage
docker compose exec postgres psql -U yukti -d yukti -c "
  SELECT symbol, COUNT(*) as candles, MIN(time), MAX(time)
  FROM candles
  GROUP BY symbol;
"
```

#### Redis (live positions, halt flag, cooldowns)

```bash
docker compose exec redis redis-cli

# List all keys
> KEYS *

# Check halt flag
> GET yukti:halt

# Check daily P&L
> GET yukti:pnl:daily

# Exit
> EXIT
```

### Analyze decision quality

After 1-2 weeks of paper trading:

```bash
docker compose exec yukti python -m yukti.agents.quality --days 14
```

Output:
```
╔══ YUKTI DECISION QUALITY REPORT ══════════════════════════════╗
  Period             : last 14 days
  Total decisions    : 450
  Total closed trades: 28
  Skip rate          : 93.3%

  ── Conviction → outcomes ──
  conv  trades  win%     avg_P&L%
  ──────────────────────────────
     5       1   0.0%      -1.20%
     6       2  50.0%      +0.85%
     7       4  75.0%      +1.30%
     8       8  62.5%      +0.95%
     9       9  77.8%      +1.65%
    10       4 100.0%      +2.10%

  ── Signal quality ──
  Low conv (5-6) win rate : 33.3%
  High conv (9-10) win rate: 88.9%
  Verdict                 : STRONG_PREDICTIVE  ✅
```

If you see `INVERTED` or `NO_SIGNAL` — the system prompt needs work before going live.

### Generate A/B test report (if using dual models)

```bash
docker compose exec yukti python scripts/ab_report.py --days 7
```

---

## Troubleshooting

### "Connection refused" errors

The agent might be starting. Wait 15 seconds and check again:

```bash
docker compose logs yukti | tail -20
```

### "Candle fetch failed" repeatedly

DhanHQ API might be down or rate-limited:

```bash
# Check DhanHQ status
docker compose logs yukti | grep -i "dhan"

# Rate limiter issue? Check we're not calling too fast
docker compose logs yukti | grep -i "rate"

# If stuck, restart the agent
docker compose restart yukti
```

### "UNSAFE: [symbol] filled but GTTs failed"

A trade filled but SL GTT registration failed. The agent market-exited immediately. Check:

```bash
docker compose logs yukti | grep UNSAFE

# Review the specific trade in Grafana
```

This is extremely rare in paper mode. If it happens in live, the kill-switch worked correctly.

### "Reconciliation FAILED — halting"

Broker positions don't match Redis state. Likely a rare edge case. Restart:

```bash
docker compose restart yukti
```

It will re-run crash recovery and retry. Check logs.

### Postgres disk full

TimescaleDB storing too many candles. Check size:

```bash
docker compose exec postgres du -sh /var/lib/postgresql/data

# Trim old candles (keep last 2 years)
docker compose exec postgres psql -U yukti -d yukti -c "
  DELETE FROM candles WHERE time < NOW() - INTERVAL '730 days';
  VACUUM candles;
"
```

---

## Cost estimate (DigitalOcean)

- **Droplet (BLR1 1GB):** $6/month
- **Backups:** $1.20/month
- **Monitoring:** Free
- **Bandwidth:** Free (5TB/month)
- **Total:** ~$7/month

Cloud spend: $0 (unless you add load balancer / extra storage).

API spend:
- **Gemini 2.0 Flash:** ₹0 on free tier (~$0)
- **Claude Sonnet 4.6:** ~₹400-1,700/month (~$5-20) if used live
- **Voyage AI:** ₹0 (50M tokens free tier)
- **DhanHQ:** ₹0 (data feed is free)
- **Telegram:** ₹0
- **Total:** ~₹0-1,700/month

---

## Scaling

After 2-3 months of stable paper trading with positive backtest results:

1. **Go live with 10% of intended capital** (not 100%)
2. Scale up to 50% after 30 days of positive live results
3. Scale to 100% only after 60+ days of 2%+ monthly returns
4. Consider a second VM for a parallel strategy (using different symbols or conviction thresholds)

---

## Disaster recovery

Your persistent data lives in PostgreSQL volumes and Redis.

```bash
# Backup PostgreSQL
docker compose exec postgres pg_dump -U yukti yukti > yukti_backup.sql

# Restore
docker compose exec postgres psql -U yukti yukti < yukti_backup.sql

# Backup Redis
docker compose exec redis redis-cli --rdb /tmp/dump.rdb
docker cp CONTAINER_ID:/tmp/dump.rdb ./redis_backup.rdb
```

---

## Next steps

1. ✅ Deploy on DigitalOcean (or your VM)
2. ✅ Run paper mode for 4+ weeks
3. ✅ Check decision quality (`uv run python -m yukti.agents.quality`)
4. ✅ Run backtest on 2 years of candles
5. ✅ Generate A/B report if using both models
6. ✅ Only then: go live with 10% capital

Good luck! 🚀
