# Yukti (युक्ति) — Autonomous NSE Trading Agent

> *Sanskrit: strategy, skill, clever reasoning*

A production-ready, AI-powered trading agent for the Indian stock market (NSE/BSE).
Reasons like a human trader, executes with DhanHQ, learns from its own trades.

**Status:** Beta — Architecture stable. Currently in validation hardening phase (cost accounting, slippage modeling, walk-forward backtests, closed-loop agent learning). Live trading gated on 90-day shadow Sharpe > 1.0 with calibration error < 15%. See [Roadmap](#-roadmap).

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
- **Learn from history** — outcome-filtered, recency-weighted retrieval; weekly conviction recalibration injected into prompt; meta-reflection every 7 days (see Agent Learning tiers in [Roadmap](#-roadmap))

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

> **Validation gate:** No live capital until 90 days of shadow trading shows net Sharpe > 1.0 with conviction calibration error < 15%.

### ✅ Phase 0 — Signal Quality Upgrade (shipped Apr 2026)
- [x] Dynamic universe scanner (50+ symbols vs static 5)
- [x] Multi-timeframe (5m + daily) with ADX & daily S/R
- [x] Opening Range Breakout (ORB) pattern (9:15–9:30 IST, valid till 11:00)
- [x] VWAP Bounce pattern
- [x] Two-layer context (macro → daily → intraday alignment) in Arjun prompt

### 🚧 Phase 1 — Validation Infrastructure (BLOCKER for live)
- [ ] **1.1 Cost-aware P&L** — `yukti/execution/costs.py` (STT, exchange txn, SEBI, stamp, GST, brokerage); migration `004_trade_costs.py` adds `gross_pnl` / `charges` / `net_pnl` to Trade; `quality.py` reports **net** expectancy
- [ ] **1.2 Slippage modeling** — `yukti/execution/slippage.py` with formula `bps = base + (order_value/turnover) * impact`; calibrated per liquidity bucket (large 5bps / mid 15bps / small 30bps); paper broker applies, live broker records `expected_price` vs `fill_price`; Prometheus histogram
- [ ] **1.3 Walk-forward backtest** — `yukti/backtest/walk_forward.py`; rolling 60-day train / 20-day test, step 20; outputs per-window Sharpe, max DD, equity curve to `reports/wf_<ts>/`
- [ ] **1.4 Decision quality v2** — extend `agents/quality.py` with calibration plot, Brier score, edge-decay (rolling 20-trade Sharpe), A/B Claude-vs-Gemini report; weekly cron → Telegram
- [ ] **1.5 Agent Learning Tier 1 — Smarter retrieval** — `agents/memory.py`: filter by outcome (winners for entries, losers for skips), recency weighting (exp decay, 30-day half-life), regime-match filter, prune journals > 6 months OR negative conviction-outcome correlation
- [ ] **1.6 Agent Learning Tier 2 — Closed-loop conviction calibration** — new `agents/calibration.py` maintains `(setup_type, conviction) → empirical_win_rate` table refreshed weekly; injected into Arjun prompt (e.g., *"Your conviction-7 ORB historically won 38%, not 60%"*); auto-raises risk-gate floor for setups with negative net expectancy
- [ ] **1.7 Agent Learning Tier 5 — Weekly meta-reflection** — new `agents/meta_journal.py`; Claude reads last 7 days of journals + quality report, writes meta-reflection stored in `meta_journals` table; latest entry injected into Arjun system prompt (human-readable, auditable, reversible)

### Phase 2 — Execution Quality
- [ ] **2.1 Trailing SL + partial T1 exit** — extend `execution/order_sm.py` with `PARTIAL_EXITED_T1`, `TRAILING` states; T1 hit → market sell 50% + move SL to breakeven on remainder; trail to last swing low per 5m candle; migration `005_position_milestones.py`
- [ ] **2.2 Correlation gate (9th risk gate)** — new `risk/correlation.py`; reject if same-sector open positions ≥ 2 OR 60-day correlation with basket > 0.8; configs `MAX_SECTOR_CONCURRENT`, `MAX_CORRELATION`
- [ ] **2.3 Regime detector** — new `signals/regime.py`; daily nifty_adx + atr_pct + vix + breadth → `TRENDING_UP/DOWN/CHOPPY/VOLATILE`; cached in Redis `regime:current`; wired into Arjun prompt + risk gates (disable mean-reversion in trends; tighten conviction floor in choppy)
- [ ] **2.4 Agent Learning Tier 3 — Pattern weights** — track per-pattern rolling 60-trade net expectancy; auto-disable patterns with Wilson lower bound < 0 (30-day cooldown); Prometheus alert on edge decay; manual re-enable via control plane endpoint

### Phase 3 — Observability & Reporting
- [ ] **3.1 Live equity curve** — `GET /api/equity-curve?days=N`; web portal panel with rolling Sharpe + max DD; daily JSON snapshot to `reports/equity/YYYY-MM-DD.json` (transparent track record)
- [ ] **3.2 Tax-aware reporting** — `yukti/reports/tax.py`; categorizes intraday (speculative) vs delivery (STCG/LTCG); ITR-3 compatible CSV; CLI `uv run python -m yukti.reports.tax --fy 2026-27`
- [ ] **3.3 Per-decision AI cost ledger** — `ai_calls` table for monthly cost transparency

### Phase 4 — Strategic (post-validation only)
- [ ] **4.1 F&O support** — broker abstraction for options chains; patterns `iv_crush_short_strangle`, `directional_long_call/put`; risk gates for max margin & max delta exposure (DO NOT START until Phase 1 proves equity edge)
- [ ] **4.2 Stop-loss types** — ATR-based SL (currently swing-only); time-stop (exit if not at T1 within N candles)
- [ ] **4.3 News-event awareness** — catalyst calendar (earnings / RBI / FOMC); block entries 1hr before/after; wire into `services/macro_context_service.py`
- [ ] **4.4 Agent Learning Tier 4 — Fine-tuning** — collect 1000+ labeled `(context, decision, R_multiple)` triples; fine-tune Gemini Flash or Llama 3.1 8B; replaces prompt-engineered Arjun with a model that has actually learned (only after 6+ months of clean data)

### Phase 5 — Honest README Refresh (after Phase 1 results)
- [ ] Replace `Expected performance` section with **measured** Sharpe + max DD from shadow data
- [ ] Publish equity curve in repo (transparent track record)
- [ ] Add caveat: "Results from N days paper/shadow. Live performance will differ."

### Suggested sequencing (12 weeks)

| Week | Work |
|---|---|
| 1-2 | 1.1 + 1.2 (costs + slippage) |
| 3 | 1.3 (walk-forward) |
| 4 | 1.4 + 1.5 (quality v2 + smarter retrieval) |
| 5 | 1.6 + 1.7 (calibration + meta-reflection) |
| 6 | 2.2 (correlation gate) |
| 7 | 2.3 (regime detector) |
| 8-9 | 2.1 (trailing/partial T1) |
| 10 | 2.4 (pattern weights) |
| 11 | 3.x (observability + tax) |
| 12+ | 90-day shadow validation → Phase 4 only if numbers justify |

### Agent Learning — 5-tier model at a glance

| Tier | What | Phase | Effort |
|---|---|---|---|
| 1 | Outcome-filtered, recency-weighted, regime-matched memory retrieval | 1.5 | S |
| 2 | Closed-loop conviction calibration injected into prompt | 1.6 | M |
| 3 | Auto-disable patterns with negative net expectancy (Wilson bound) | 2.4 | M |
| 4 | Fine-tune small model on labeled decision corpus | 4.4 | XL |
| 5 | Weekly meta-reflection becomes part of system prompt | 1.7 | S |

### Open design questions
1. **Pattern cooldown threshold (Tier 3)** — Wilson lower bound at 95% or 90% confidence?
2. **Meta-journal model (Tier 5)** — Claude (better reasoning, ~₹2/week) or Gemini (free, less nuanced)?
3. **Calibration table scope (Tier 2)** — global, per-setup, or per-(setup, regime)? Last is most powerful but needs more data.

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
