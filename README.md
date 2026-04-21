# Yukti (युक्ति) — Autonomous NSE Trading Agent

> *Sanskrit: strategy, skill, clever reasoning*

A production-ready, AI-powered trading agent for the Indian stock market (NSE/BSE).
Reasons like a human trader, executes with DhanHQ, learns from its own trades.

**Status:** Beta — Core paper/shadow loop is stable and ready for validation. Not yet recommended for unsupervised live trading.

---

## 🎯 Current Status

The end-to-end paper trading loop is **complete and stable**. All critical bugs have been fixed. The agent can be run in paper or shadow mode for multi-week validation before promoting to live.

### Feature Status

#### Core Agent
- **Multi-AI support** — ✅ Claude Sonnet 4.6, Gemini 2.0 Flash, A/B test mode
- **Order management** — ✅ Crash-safe state machine with GTTs, partial fill handling, startup reconciliation
- **Risk sizing** — ✅ Conviction-based position sizing with 8 hard gates (incl. NSE circuit-breaker)
- **Signal filtering** — ✅ 7 technical patterns pre-filter ~80% of candles to save API costs
- **Learning memory** — ✅ Voyage AI embeddings → pgvector similarity → past trades injected as context
- **Macro context** — ✅ India VIX, FII/DII net flows, live market headlines injected per cycle

#### Operations
- **Crash recovery** — ✅ Auto-detects and re-arms stuck positions on startup
- **Dead man's switch** — ✅ Watchdog auto-halts if signal loop goes silent
- **Observability** — ✅ Prometheus metrics, Grafana dashboards, structured logging
- **Web portal** — ✅ React 18 SPA, real-time WebSocket, kill switch, journal browser
- **Telegram alerts** — ✅ Trade notifications, crash alerts, daily summary, `/halt` command

#### Scheduler & Control Plane

- The control plane (`ControlPlaneService`) now starts the application's cron-style scheduler. When the control plane is started it calls `build_scheduler()` (from `yukti/scheduler/jobs.py`) and starts it; on shutdown the service calls `scheduler.shutdown(wait=False)` to stop jobs cleanly.
- Key scheduler jobs are defined in `yukti/scheduler/jobs.py`: `job_morning_prep`, `job_eod_squareoff`, `job_daily_reset`, and `job_daily_report`. These perform tasks such as end-of-day square-off, daily counter resets, and journal writing.
- Files changed: `yukti/services/control_plane_service.py` (starts/shuts down scheduler), `yukti/scheduler/jobs.py` (job definitions). See those files for exact behavior and cron timings.
- Notes:
    - The scheduler is started automatically when the `ControlPlaneService` runs (used in live/shadow modes). In `paper` mode the agent runs a single scan and the control plane (and scheduler) is not started by default.
    - To disable scheduled jobs for testing/CI, run with `MODE=paper` or avoid starting the `ControlPlaneService`.
    - Ensure the database migration that creates the `positions` table (e.g., `yukti/data/migrations/versions/003_positions.py`) is applied before running the control plane.

#### Infrastructure
- **Database** — ✅ PostgreSQL 16 + TimescaleDB + pgvector, Redis 7
- **Async architecture** — ✅ 100% async-first with asyncio, graceful shutdown
- **Docker** — ✅ Single-command `docker compose up`
- **Testing** — ✅ Unit tests (risk, signals, AI schema); integration test for full trade cycle
- **Deployment** — ✅ Supervisor config, Grafana dashboards, Prometheus scraping

#### Modes
- **Paper trading** — ✅ Simulated fills, full agent logic
- **Shadow mode** — ✅ Live market data, orders logged but never placed (zero-risk parallel validation)
- **Live trading** — ✅ Real DhanHQ orders (validate with paper/shadow first)
- **Backtest** — ✅ Historical candle replay with PaperBroker

---

## 🎯 Why Yukti?

Most retail trading bots are:
- **Rule-based** — brittle, don't adapt, can't handle edge cases
- **Backtested to death** — overfitted, fail in live markets
- **A black box** — no way to debug why a trade was (or wasn't) taken

Yukti flips this:
- **Reasoning engine** — Claude or Gemini *thinks* about each setup, writes a conviction score, explains the trade
- **Risk first** — deterministic 8-gate risk filter after every AI decision
- **Learning loop** — journals every closed trade, embeds it in vector memory, injects lessons into future decisions
- **Crash-safe** — recovers from process crashes without losing state or exposing positions
- **Multi-provider** — switches between Claude (best reasoning) and Gemini (free tier) — even A/B tests both in parallel

---

## 📊 Architecture

```
Market (NSE/BSE)
    ↓ [DhanHQ WebSocket + REST]
Ingestion (OHLCV + perf state)
    ↓
Signals (indicators + patterns)
    ↓ [pre-filter: skip 80% of candles]
AI Brain (Claude or Gemini)
    ↓ [TradeDecision JSON]
Risk Gates (8 deterministic checks)
    ↓
Execution (DhanHQ orders → GTTs)
    ↓
Learning Loop (journal + vector embeddings)
    ↓ [stored in PostgreSQL]
Web Portal (React 18, real-time WebSocket)
```

**100% async-first. Paper → Shadow → Live progression baked in.**

---

## 🚀 Quick start

### Prerequisites
- Python 3.11+
- PostgreSQL 16 + TimescaleDB
- Redis 7
- DhanHQ broker account (free)
- AI API key (Gemini free, or Claude)
- Docker (recommended for deployment)

### Setup (5 minutes)

```bash
# Clone + install
git clone https://github.com/pradeeprlck/Yukti.git
cd yukti
uv sync

# Copy config
cp .env.example .env
# Edit .env with your DhanHQ token, Gemini/Claude key, Telegram bot token

# Start infrastructure
docker compose up -d redis postgres

# Bootstrap database
uv run python scripts/bootstrap.py

# Load trading universe (fetches Nifty50 symbols + DhanHQ security IDs dynamically)
uv run python scripts/universe_loader.py --dynamic
# Or use a specific index:
# uv run python scripts/universe_loader.py --dynamic --index "NIFTY 100"

# Run in paper mode (work in progress — expect partial functionality)
uv run python -m yukti --mode paper
```

**Web portal:** http://localhost:8000 (live stats, positions, trades, journal, kill switch)

---

## 📋 What's included

### Core agent
- **Multi-AI support** — Claude Sonnet 4.6, Gemini 2.0 Flash, A/B test mode
- **Order management** — crash-safe state machine with GTTs and partial fill handling
- **Risk sizing** — conviction-based position sizing with 8 hard gates (incl. NSE circuit-breaker)
- **Signal filtering** — 7 technical patterns, pre-filters 80% of candles to save API costs
- **Learning memory** — Voyage AI embeddings, pgvector similarity search, past trades as context

### Operations
- **Crash recovery** — auto-detects and re-arms stuck positions on startup
- **Dead man's switch** — watchdog detects if signal loop stops (deadlock detection)
- **Shadow mode** — run in parallel with live market data but no real orders (zero-risk validation)
- **Backtest engine** — replay historical candles, measure expectancy before deployment

### Observability
- **Web portal** — React SPA with real-time WebSocket, P&L chart, position cards, journal browser, kill switch
- **Prometheus + Grafana** — 16 metrics, latency histograms, cost tracking, signal skip breakdown
- **Telegram alerts** — trade notifications, crash alerts, daily summary, emergency `/halt` command
- **Decision quality report** — validates that conviction scores predict outcomes

### Development
- **Unit tests** — risk math, indicator computation, schema validation
- **Docker + Supervisor** — one-command deployment with auto-restart on crash
- **Doppler integration** — secrets management (no plaintext on disk)
- **Alembic migrations** — version-controlled database schema

---

## 📈 Modes

| Mode | Broker | Market Data | Risk | Use case |
|---|---|---|---|---|
| **paper** | PaperBroker (simulated) | Live | None | Validate decision quality (2-4 weeks) |
| **shadow** | ShadowBroker (logged) | Live | None | Run in parallel with live, validate signal quality |
| **live** | Real DhanHQ | Live | Real ₹ | Execute real trades (start at 10% of intended size) |
| **backtest** | PaperBroker | Historical | None | Measure 2-year expectancy before any live capital |

```bash
MODE=paper    uv run python -m yukti              # Default: paper mode
MODE=shadow   uv run python -m yukti              # Logs what would happen
MODE=live     uv run python -m yukti              # Real money — respect this
MODE=backtest uv run python -m yukti --bt-start 2023-01-01
```

---

## 🧠 The AI brain

**System prompt:** Arjun, an experienced NSE trader with disciplined rules:
- **Wait more than you act** — conviction scores 5-10, skip marginal setups
- **Risk first** — every trade has a hard stop loss at a swing level
- **Conviction-based sizing** — 9-10 → 1.5×, 7-8 → 1.0×, 5-6 → 0.5× position size
- **Learn from history** — past 3 similar setups injected as context

**Output:** Deterministic JSON schema validated before any order placed.

**Cost (with pattern pre-filter):**
- Gemini 2.0 Flash: **₹0/month** (free tier covers retail volume)
- Claude Sonnet 4.6: **₹5-15/month** (if used live)

---

## ⚙️ Configuration

All settings in `.env`:

```env
# Broker
DHAN_CLIENT_ID=xxx
DHAN_ACCESS_TOKEN=xxx

# AI (choose one or both)
AI_PROVIDER=gemini                    # or: claude, ab_test
GEMINI_API_KEY=xxx
ANTHROPIC_API_KEY=xxx

# Performance
ACCOUNT_VALUE=500000                  # ₹
RISK_PCT=0.01                         # 1% per trade
MODE=paper                            # paper | live | shadow | backtest

# Candles
CANDLE_INTERVAL=5                     # minutes
WATCHLIST=RELIANCE,HDFCBANK,INFY,TCS

# Notifications
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=xxx
```

**Production:** Use Doppler instead of `.env`:
```bash
doppler run -- uv run python -m yukti
```

---

## 📊 Decision quality validation

After 2 weeks of paper trading:

```bash
uv run python -m yukti.agents.quality --days 14
```

Output shows:
- Skip rate (% of candles that became SKIP decisions)
- Win rate per conviction bucket (1-10)
- Setup type performance breakdown
- Signal: is conviction actually predictive? ("strong_predictive", "no_signal", or "inverted")

If conviction doesn't predict outcomes, the prompt needs work before live trading.

---

## 🔄 Learning loop

Every closed trade triggers:

1. **Journal writer** (Claude) writes 4-sentence reflection:
   - What the setup was
   - What happened
   - Why it worked or failed
   - One concrete lesson

2. **Voyage AI** embeds the journal (1024-dim vector)

3. **pgvector** stores it; on next similar setup, top-3 past entries injected into Claude's context

Result: The agent learns from its own history without retraining.

---

## 🛡️ Safety features

- **Kill switch** — `/halt` Telegram command stops all new trades immediately
- **Daily loss limit** — auto-halt at -2% of account
- **Max positions** — concurrent limit of 5
- **Conviction floor** — skip if conviction < 5
- **R:R minimum** — skip if risk:reward < 1.8
- **Cooldown** — symbol blacklisted for 3 cycles after a trade
- **Watchdog** — detects if signal loop stops (deadlock), auto-halts
- **NSE circuit breaker** — halts all new entries when Nifty drops ≥ 5% intraday
- **Crash recovery** — on restart, re-arms unprotected filled positions or exits them at market

---

## 📈 Expected performance

**Realistic targets (NSE mid-caps, 5-min intraday):**
- Win rate: 45-55% (quality matters more than frequency)
- Average R:R: 2.0-3.0
- Monthly expectancy: 0.5-1.5% (compound, reinvested)

**What breaks Yukti:**
- Gaps > 5% (no fill on SL)
- Sudden news events (before AI reacts)
- High-slippage illiquid scrips

**What it handles well:**
- Trending days (breakout/pullback setups)
- Volatile mid-caps (higher risk = higher reward)
- Multiple timeframe confluence (structured SL/target)

---

## 🚨 Disclaimer

Trading involves real financial risk. Check current SEBI regulations on algorithmic trading
before deploying live. Never deploy capital you cannot afford to lose.

This is a tool, not financial advice. Use it responsibly.

---

## 🛣️ Roadmap

- [ ] Trailing SL to breakeven after T1 hit + partial position exit at T1
- [ ] Multi-timeframe scanning (1m + 5m + 15m confluence)
- [ ] Opening Range Breakout (ORB) pattern (9:15–9:30 IST)
- [ ] Slippage tracking (fill price vs entry price per trade)
- [ ] F&O (futures and options) support
- [ ] Tax-aware reporting (India ITR-3 format)
- [ ] Automated weekly decision-quality alerts (conviction vs outcomes)
- [ ] Portfolio backtester (risk-adjusted returns)

---

## 📚 Architecture docs

- [End-to-end system diagram](yukti_architecture.html) — click components to see implementation details
- [Multi-provider AI system](yukti/agents/arjun.py) — Claude, Gemini, A/B test
- [Crash-safe order state machine](yukti/execution/order_intent.py) — how intents persist
- [Risk gates](yukti/risk/__init__.py) — 7 deterministic checks
- [Signal patterns](yukti/signals/patterns.py) — 7 technical pattern detectors

---

## 👨‍💻 Development

```bash
# Install dev dependencies
uv sync

# Run tests
pytest tests/unit -v

# Lint + format
ruff check . --fix
ruff format .

# Run backtest
uv run python -m yukti --mode backtest --bt-start 2023-01-01

# Shadow mode (validate before live)
MODE=shadow uv run python -m yukti

# Decision quality report
uv run python -m yukti.agents.quality --days 30
```

---

## 📝 License

Apache 2.0 — use freely, modify, deploy. No warranty. See [LICENSE](LICENSE) for details.

---

## 🤝 Contributing

Issues, pull requests, and forks welcome. Current gaps:
- Integration tests (need live DhanHQ sandbox or recorded fixtures)
- Trailing SL / partial T1 exit implementation
- Multi-timeframe confluence signals
- Opening Range Breakout (ORB) pattern
- Slippage and execution quality tracking

---

## ✋ Support

- **Issues:** GitHub issues for bugs and feature requests
- **Discussions:** GitHub discussions for strategy questions
- **Security:** Found a bug? Email security@example.com (responsible disclosure)

---

**Made with ❤️ for retail traders who believe in reasoning, not rules.**

Last updated: April 18, 2026
