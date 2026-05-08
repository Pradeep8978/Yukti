# Yukti (युक्ति) — Autonomous NSE Trading Agent

> *Sanskrit: strategy, skill, clever reasoning*

Yukti is an AI-powered intraday trading agent for the Indian stock market (NSE). It thinks like a disciplined human trader, sizes positions based on conviction, learns from every trade it closes, and never places a live order without passing 8 deterministic risk checks.

**Status:** Beta — paper and shadow modes are stable. Validate for 4+ weeks before promoting to live.

---

## How a trading day flows

Yukti runs on a fixed schedule, gated so nothing fires on weekends, NSE holidays, or outside market hours.

```
07:00  NSE holiday calendar refreshed from NSE API (every Monday)
       ─────────────────────────────────── Pre-market ───
08:00  Catalyst refresh    — NSE announcements, earnings calendar
08:15  Exclusions refresh  — F&O ban list, ASM/GSM stocks
08:30  Candidate pool      — NIFTY 500 filtered by turnover + volatility
08:45  Universe scan       — Score and rank the candidate pool
09:00  Morning prep        — Reconcile any open positions, send Telegram check-in
09:10  Gap snapshot        — Record pre-market gaps for each candidate
       ─────────────────────────────────── Market open ─ 09:15 IST ───
09:15  Signal loop starts  — Scans active universe every 5 minutes
10:00  Universe refresh    — Pull in new movers, keep the list live
12:00  Universe refresh    — Midday refresh
       ─────────────────────────────────── Market close ─ 15:10 IST ───
15:10  EOD square-off      — Force-close any open intraday positions at market
       ─────────────────────────────────── Post-market ───
16:00  Daily reset         — Reset counters, write trade journals
16:05  Meta-lessons        — Aggregate key lessons across all closed trades
16:30  Daily report        — Telegram summary: P&L, win rate, streak
02:00  Learning loop       — Embed new journals into vector memory (optional)
03:00  Self-learning       — Retrain + evaluate adapter if enough data (optional)
```

> Everything from 08:00 to 16:30 is skipped automatically on weekends and NSE holidays.
> The NSE holiday list is pulled live from the NSE API and cached locally for 30 days.

---

## How Yukti decides to trade

Every 5 minutes, for each symbol in the active universe:

```
1. OHLCV candle arrives
        ↓
2. Technical pre-filter (7 patterns)
   — Skips ~80% of candles with no setup
   — Saves API cost
        ↓  [only if a pattern fires]
3. AI brain (Arjun — Claude or Gemini)
   — Reads: price action, indicators, macro context (VIX, FII/DII), past 3 similar trades
   — Outputs: TradeDecision JSON with direction, entry, stop-loss, target, conviction (1–10)
        ↓
4. Risk gates (8 deterministic checks)
   — Daily loss limit, position count, min R:R, conviction floor,
     NSE circuit breaker, cooldown, duplicate position, kill switch
        ↓  [only if all 8 pass]
5. Order placed via DhanHQ
   — Entry market order + GTT stop-loss + GTT target, atomically
        ↓
6. Monitor watches the position
   — On close: writes journal, embeds it in vector memory
```

---

## How the AI thinks (the Arjun persona)

The AI operates as **Arjun** — a disciplined NSE trader persona baked into the system prompt. Key rules:

- **Wait more than you act.** Conviction 7+ required to open; 5–6 only in excellent setups.
- **Risk first.** Every trade has a hard stop at a technical swing level — not a percentage.
- **Size by conviction.** Conviction 9–10 → 1.5× base size. 7–8 → 1.0×. 5–6 → 0.5×.
- **Learn from history.** The 3 most similar past trades (by pattern + symbol type) are injected as context before every decision.

The AI outputs structured JSON that is schema-validated before any order is considered.

---

## How Yukti learns

Every time a trade closes, Yukti runs a learning loop:

```
Closed trade
    ↓
Journal writer (AI) writes a 4-sentence reflection:
  • What the setup was
  • What happened
  • Why it worked or failed
  • One concrete lesson for next time
    ↓
Voyage AI embeds the journal (1024-dim vector)
    ↓
Stored in PostgreSQL (pgvector)
    ↓
Next time a similar setup appears →
  top-3 past entries injected into the AI's context
```

The agent improves over time without any manual retraining — it just reads its own history.

---

## Risk and safety features

| Gate | What it does |
|---|---|
| Daily loss limit | Auto-halts all new trades if account drops 2% in a day |
| Max positions | No more than 5 concurrent open positions |
| Conviction floor | Skips if AI conviction < 5 (on a 1–10 scale) |
| Risk:reward minimum | Skips if R:R < 1.8 (risk more than you can win) |
| Symbol cooldown | Blacklists a symbol for 3 scan cycles after closing a trade |
| NSE circuit breaker | Halts new entries if Nifty drops ≥ 5% intraday |
| Kill switch | `/halt` Telegram command stops all new entries immediately |
| Crash recovery | On restart, re-arms unprotected open positions or exits them at market |
| Watchdog | Detects if the signal loop goes silent (deadlock), triggers auto-halt |

---

## Trading modes

| Mode | What it does | When to use |
|---|---|---|
| **paper** | Simulated fills, full AI logic, no real money | Start here — run 4+ weeks |
| **shadow** | Live market data, orders logged but never placed | Run alongside live trading to validate signal quality |
| **live** | Real DhanHQ orders, real ₹ | Only after paper + shadow validation |
| **backtest** | Replay historical candles | Measure multi-year expectancy before deploying capital |

**Recommended progression:** paper (4 weeks) → shadow (2 weeks) → live at 10% of intended size.

---

## What to monitor

After running paper mode for 2 weeks, run the decision quality report:

```bash
uv run python -m yukti.agents.quality --days 14
```

Key things to check:

- **Skip rate** — should be 70–85%. If lower, the pre-filter isn't working. If higher, the watchlist is too quiet.
- **Win rate by conviction** — conviction 8–10 should outperform 5–6. If not, the AI prompt needs work.
- **R:R achieved** — average actual reward:risk should be ≥ 1.8.
- **Conviction signal** — the report labels this "strong_predictive", "no_signal", or "inverted". Don't go live with "inverted".

---

## Setup

### Prerequisites

- Python 3.11+
- PostgreSQL 16 with TimescaleDB and pgvector extensions
- Redis 7
- DhanHQ broker account (free — [dhan.co](https://dhan.co))
- AI key: Gemini (free tier works) or Claude (paid, better reasoning)
- Telegram bot (free — for alerts and the kill switch)
- Docker (strongly recommended for infrastructure)

### Getting started

```bash
# Clone and install
git clone https://github.com/Pradeep8978/Yukti.git
cd Yukti
uv sync

# Copy and fill in the config
cp .env.example .env
# → Edit .env with your DhanHQ token, AI key, Telegram bot token

# Start databases
docker compose up -d redis postgres

# Set up the database schema
uv run python scripts/bootstrap.py
uv run alembic upgrade head

# Load the trading universe (fetches NIFTY 500 symbols dynamically)
uv run python scripts/universe_loader.py --dynamic

# Run in paper mode
uv run python -m yukti --mode paper
```

The web portal is at **http://localhost:8000** — live P&L, open positions, trade log, journal browser, and the kill switch.

### Key config options (`.env`)

```env
# Broker
DHAN_CLIENT_ID=your_client_id
DHAN_ACCESS_TOKEN=your_token

# AI (pick one or use ab_test to run both)
AI_PROVIDER=gemini               # gemini | claude | ab_test
GEMINI_API_KEY=your_key
ANTHROPIC_API_KEY=your_key

# Account
ACCOUNT_VALUE=500000             # ₹ — used for position sizing
RISK_PCT=0.01                    # 1% of account at risk per trade
MODE=paper                       # paper | shadow | live | backtest

# Universe
CANDLE_INTERVAL=5                # minutes
CANDIDATE_POOL_INDEX=NIFTY 500   # index for candidate filtering

# Alerts
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

**Production deployments:** use Doppler instead of `.env` so no secrets touch disk:
```bash
doppler run -- uv run python -m yukti
```

---

## Observability

- **Web portal** — real-time WebSocket, P&L chart, position cards, journal browser, kill switch
- **Telegram** — trade-open/close alerts, crash notifications, daily summary, `/halt` command
- **Prometheus + Grafana** — 16 metrics including signal latency, skip rate breakdown, API cost per scan
- **Structured logs** — every decision logged with symbol, pattern, conviction, and outcome

---

## What works well vs. what breaks it

**Works well:**
- Trending days with clear breakout or pullback setups
- Volatile mid-caps with high turnover (better R:R opportunities)
- Multi-technical confluence (pattern + VWAP + volume)

**Known failure modes:**
- Overnight gaps > 5% — stop-loss fill is not guaranteed
- Sudden news events — AI reacts to technicals, not breaking news
- Illiquid scrips with high impact cost — slippage eats the edge

**Realistic expectations (paper, 5-min intraday, NIFTY 500):**
- Win rate: 45–55%
- Average R:R achieved: 2.0–3.0
- Monthly expectancy: 0.5–1.5% (compounded on account value)

---

## Roadmap

- [ ] Trailing stop to breakeven after T1 hit, partial exit at T1
- [ ] Multi-timeframe confluence (1m + 5m + 15m)
- [ ] Opening Range Breakout (ORB) pattern — 09:15–09:30 IST
- [ ] Slippage tracking (fill price vs. intended entry per trade)
- [ ] F&O support (futures and options)
- [ ] Tax reporting in ITR-3 format
- [ ] Automated weekly conviction-vs-outcomes alert

---

## Development

```bash
# Tests
pytest tests/unit -v

# Lint + format
ruff check . --fix && ruff format .

# Backtest a date range
uv run python -m yukti --mode backtest --bt-start 2023-01-01

# Shadow mode (live data, no orders)
MODE=shadow uv run python -m yukti

# Decision quality report
uv run python -m yukti.agents.quality --days 30

# Embed pending journal entries manually
uv run python -m yukti.services.learning_loop_service
```

---

## Disclaimer

Trading involves real financial risk. Understand India's SEBI regulations on algorithmic trading before deploying live capital. Never trade with money you cannot afford to lose.

Yukti is a tool, not financial advice.

---

## License

Apache 2.0 — use freely, modify, deploy. No warranty. See [LICENSE](LICENSE).

---

**Built for retail traders who believe in reasoning, not rules.**
