# Yukti (युक्ति)

> *Sanskrit: strategy, skill, clever reasoning*

Autonomous NSE/BSE trading agent that reasons like a human trader.
**DhanHQ** for execution · **Claude Sonnet 4.6** as the AI brain · **React** web portal.

---

## Architecture

```
Data ingestion → Signal engine → Claude (Arjun) → Risk gates → DhanHQ execution
                                      ↑                              ↓
                                      └── Vector memory ← Journal ───┘
                                               ↕
                                       Web portal (React)
                                       FastAPI WebSocket
```

Seven layers: data · signals · Claude AI · risk management · DhanHQ execution ·
position monitor · learning loop (journal + vector memory)

---

## Tech stack

| Layer              | Technology                          | Purpose                              |
|--------------------|-------------------------------------|--------------------------------------|
| Agent runtime      | Python 3.11 + asyncio               | Concurrent event loop                |
| Schema validation  | pydantic v2                         | Typed Claude JSON output             |
| Package manager    | uv                                  | 100× faster than pip                 |
| AI reasoning       | Claude Sonnet 4.6 (Anthropic)       | The trader brain                     |
| Embeddings         | Voyage AI                           | Journal entry vectors                |
| Technical signals  | pandas-ta                           | RSI, MACD, ATR, VWAP, supertrend     |
| Broker + feed      | dhanhq Python SDK                   | Orders, GTT, positions, candles      |
| Hot state          | Redis 7                             | Live positions, cooldowns, P&L       |
| Warm store         | PostgreSQL 16 + TimescaleDB         | Trade log, journal, OHLCV history    |
| Vector memory      | pgvector (Postgres extension)       | Semantic search over past trades     |
| Web API            | FastAPI + uvicorn                   | REST + WebSocket                     |
| Web portal         | React 18 + Vite + TypeScript        | Dashboard, journal, kill switch      |
| Styling            | Tailwind CSS                        | Utility-first dark UI                |
| Charts             | Recharts                            | P&L equity curve, performance charts |
| Scheduler          | APScheduler                         | NSE cron jobs                        |
| Alerts + control   | Telegram (python-telegram-bot)      | Push alerts, /halt command           |
| Metrics            | Prometheus + Grafana                | Win rate, P&L, Claude cost           |
| Secrets            | Doppler                             | DhanHQ token, Anthropic key          |
| Deployment         | Docker + DigitalOcean Bangalore     | One-command spin-up                  |
| Process supervision| Supervisor                          | Auto-restart, crash alert            |
| Testing            | pytest + pytest-asyncio             | Unit + integration tests             |
| Linting            | ruff                                | lint + format in one tool            |

---

## Quick start

```bash
# 1. Clone and configure
git clone <repo> yukti && cd yukti
cp .env.example .env          # fill in DhanHQ, Anthropic, Telegram keys

# 2. Start infrastructure
docker compose up -d redis postgres

# 3. Bootstrap database (extensions + migrations + hypertable)
uv run python scripts/bootstrap.py

# 4. Load watchlist
uv run python scripts/universe_loader.py --sample

# 5. Build web portal (optional in dev — Vite proxies to FastAPI)
cd webapp && npm install && npm run build:fastapi && cd ..

# 6. Start in paper mode
uv run python -m yukti --mode paper
# → Web portal:  http://localhost:8000
# → Grafana:     http://localhost:3000
```

### Dev with hot-reload portal

```bash
# Terminal 1 — agent
uv run python -m yukti --mode paper

# Terminal 2 — Vite dev server (hot-reload, proxies /api/* to FastAPI)
cd webapp && npm run dev
# → http://localhost:5173
```

---

## Web portal pages

| Route        | Content                                                   |
|--------------|-----------------------------------------------------------|
| `/`          | Dashboard — P&L stats, 14-day equity chart, positions     |
| `/positions` | Live position cards with level bars, Arjun's reasoning    |
| `/trades`    | Full trade history table                                  |
| `/journal`   | Post-trade reflections written by Claude after each close |
| `/control`   | Kill switch, halt/resume, emergency squareoff             |

The portal connects to `/ws/live` on load. FastAPI pushes state every 5 seconds.
After `npm run build:fastapi`, the portal is served directly from FastAPI at port 8000.

---

## Operating modes

```bash
uv run python -m yukti --mode paper      # Live feed, simulated fills (default)
uv run python -m yukti --mode live       # Real DhanHQ orders ⚠ real money
uv run python -m yukti --mode backtest   # Historical candle replay
```

**Always run paper mode for 2–4 weeks before going live.**

---

## Project structure

```
yukti/
├── pyproject.toml             # uv dependencies
├── docker-compose.yml         # agent + redis + postgres + grafana
├── Dockerfile                 # multi-stage: webapp build → python agent
│
├── webapp/                    # React web portal (Vite + Tailwind + Recharts)
│   ├── src/
│   │   ├── pages/             # Dashboard, Positions, Trades, Journal, Control
│   │   ├── components/        # Layout, shared UI primitives
│   │   ├── hooks/             # useLive (WebSocket), useQuery (polling)
│   │   └── lib/api.ts         # FastAPI HTTP client
│   └── README.md              # Frontend setup guide
│
├── yukti/
│   ├── agents/                # Claude Arjun, journal writer, vector memory
│   ├── signals/               # Indicators, pattern detector, context builder
│   ├── risk/                  # Position sizing, SL/target, gates, cooldown
│   ├── execution/             # DhanHQ client, order state machine, monitor
│   ├── data/                  # SQLAlchemy models, Redis state, migrations
│   ├── api/                   # FastAPI app + routes (served at :8000)
│   ├── scheduler/             # APScheduler jobs, NSE calendar
│   ├── telegram/              # Bot, alerts, kill switch commands
│   ├── backtest/              # PaperBroker, BacktestEngine, BacktestReport
│   └── metrics.py             # Prometheus instrumentation
│
├── scripts/
│   ├── bootstrap.py           # First-time DB setup
│   ├── universe_loader.py     # Load watchlist into Redis
│   └── run_backtest.py        # CLI backtest runner
│
└── tests/
    ├── unit/                  # Risk, indicators
    └── integration/           # Full trade cycle with paper broker
```

---

## Safety rails

- Kill switch — `/halt` Telegram command + web portal button
- Daily loss limit — auto-halt at -2% of capital
- Morning reconciliation — halt if broker positions ≠ Redis state
- Max loss per trade — 1.5% of capital hard cap
- R:R gate — skip if below 1.8:1
- Conviction floor — skip if Claude scores below 5/10
- Cooldown — symbol blacklisted for 3 cycles after a trade
- Market hours — hard enforcement, no trades outside 9:15-15:10 IST
- Circuit detection — orders rejected on circuit-hit stocks

---

## Disclaimer

Trading involves real financial risk. Check current SEBI regulations on algo trading
before deploying live. Never deploy capital you cannot afford to lose.
