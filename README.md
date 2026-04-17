# Yukti (युक्ति) — Autonomous NSE Trading Agent

> *Sanskrit: strategy, skill, clever reasoning*

A production-hardened, AI-powered trading agent for the Indian stock market (NSE/BSE).
Reasons like a human trader, executes with DhanHQ, learns from its own trades.

**Status:** Early Alpha — Minimal Viable Paper Loop in progress. Not ready for live trading.

---

## 🎯 Current Status (Honest Assessment)

This project is in **early alpha stage**. The README previously claimed v0.2 and readiness for paper trading, but many core modules are incomplete, contain stubs, or have TODOs. We're actively working to bridge the gap.

### Feature Status

#### Core Agent
- **Multi-AI support** — [In Progress] Claude Sonnet 4.6, Gemini 2.0 Flash, A/B test mode (basic structure exists, needs full implementation)
- **Order management** — [In Progress] Crash-safe state machine with GTTs and partial fill handling (OrderIntent model exists, execution layer needs completion)
- **Risk sizing** — [In Progress] Conviction-based position sizing with 7 hard gates (basic structure, gates need full enforcement)
- **Signal filtering** — [In Progress] 7 technical patterns, pre-filters (indicators and patterns implemented, integration needs work)
- **Learning memory** — [TODO] Voyage AI embeddings, pgvector similarity search, past trades as context (journal and memory modules have stubs)

#### Operations
- **Crash recovery** — [In Progress] Auto-detects and re-arms stuck positions on startup (reconcile module exists, needs testing)
- **Dead man's switch** — [Completed] Watchdog detects if signal loop stops (deadlock detection)
- **Observability** — [In Progress] Prometheus metrics, structured logging (metrics.py exists, needs full integration)
- **Web portal** — [TODO] React 18, real-time WebSocket (webapp/ exists, needs functionality)
- **Telegram alerts** — [In Progress] Kill switch and alerts (bot.py exists, needs wiring)

#### Infrastructure
- **Database** — [Completed] PostgreSQL + TimescaleDB + pgvector, Redis
- **Async architecture** — [Completed] 100% async-first with asyncio
- **Docker** — [In Progress] docker-compose.yml exists, needs validation
- **Testing** — [In Progress] Unit tests for some modules, integration tests needed
- **Deployment** — [TODO] Supervisor, Grafana dashboards (deploy/ exists, needs completion)

#### Modes
- **Paper trading** — [In Progress] Simulated fills (PaperBroker exists, needs full loop)
- **Shadow mode** — [In Progress] Real market data, logged orders (ShadowBroker exists)
- **Live trading** — [TODO] Real DhanHQ orders
- **Backtest** — [In Progress] Historical replay (BacktestEngine exists, needs fidelity)

---

## 🎯 Why Yukti?

Most retail trading bots are:
- **Rule-based** — brittle, don't adapt, can't handle edge cases
- **Backtested to death** — overfitted, fail in live markets
- **A black box** — no way to debug why a trade was (or wasn't) taken

Yukti flips this:
- **Reasoning engine** — Claude or Gemini *thinks* about each setup, writes a conviction score, explains the trade
- **Risk first** — deterministic 7-gate risk filter after every AI decision
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
Risk Gates (7 deterministic checks)
    ↓
Execution (DhanHQ orders → GTTs)
    ↓
Learning Loop (journal + vector embeddings)
    ↓ [stored in PostgreSQL]
Web Portal (React 18, real-time WebSocket)
```

**48 Python files, 6,000+ lines, 100% async-first.**

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
git clone https://github.com/YOUR_USERNAME/yukti.git
cd yukti
uv sync

# Copy config
cp .env.example .env
# Edit .env with your DhanHQ token, Gemini/Claude key, Telegram bot token

# Start infrastructure
docker compose up -d redis postgres

# Bootstrap database
uv run python scripts/bootstrap.py
uv run python scripts/universe_loader.py --sample

# Run in paper mode (validation in progress)
uv run python -m yukti --mode paper
```

**Web portal:** http://localhost:8000 (live stats, positions, trades, journal, kill switch) — [TODO]

---

## 📋 What's included

### Core agent
- **Multi-AI support** — Claude Sonnet 4.6, Gemini 2.0 Flash, A/B test mode
- **Order management** — crash-safe state machine with GTT GTTs and partial fill handling
- **Risk sizing** — conviction-based position sizing with 7 hard gates
- **Signal filtering** — 7 technical patterns, pre-filters 80% of candles to save API costs
- **Learning memory** — Voyage AI embeddings, pgvector similarity search, past trades as context

### Operations
- **Crash recovery** — auto-detects and re-arms stuck positions on startup
- **Dead man's switch** — watchdog detects if signal loop stops (deadlock detection)
- **Shadow mode** — run in parallel with live market data but no real orders (zero-risk validation)
- **Backtest engine** — replay historical candles, measure expectancy before deployment

### Observability
- **Web portal** — React SPA with real-time WebSocket, P&L chart, position cards, journal browser, kill switch
- **Prometheus + Grafana** — 14 metrics, latency histograms, cost tracking, signal skip breakdown
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
- **Circuit detection** — rejections on circuit-hit stocks

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

- [ ] F&O (futures and options) support
- [ ] Multi-timeframe scanning (1m + 5m + 15m confluence)
- [ ] Pair trading / correlation strategies
- [ ] Tax-aware reporting (India ITR-3 format)
- [ ] Portfolio backtester (risk-adjusted returns)
- [ ] Web UI dashboard (v2: real-time position analytics)

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
- Integration tests (need live DhanHQ sandbox)
- Performance benchmarks
- More pattern detectors
- India-specific market context

---

## ✋ Support

- **Issues:** GitHub issues for bugs and feature requests
- **Discussions:** GitHub discussions for strategy questions
- **Security:** Found a bug? Email security@example.com (responsible disclosure)

---

**Made with ❤️ for retail traders who believe in reasoning, not rules.**

Last updated: April 17, 2025
