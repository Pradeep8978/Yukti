# Signal Quality Upgrade — Design Spec

**Date:** 2026-04-22
**Status:** Draft
**Approach:** B — Layered Analysis (clean separation, extend existing architecture)

## Overview

Upgrade Yukti's signal generation pipeline to produce higher-quality, higher-volume trade opportunities through three pillars:

1. **Dynamic Stock Discovery Engine** — replace the hardcoded 5-symbol universe with a real-time discovery system
2. **Multi-Timeframe Analysis** — add daily chart context alongside 5-min candles
3. **New Strategy Patterns** — Opening Range Breakout (ORB) + VWAP Bounce

**Goal:** More trades, but only because the system finds more high-quality setups — not by lowering the bar.

---

## 1. Dynamic Stock Discovery Engine

### New File
`services/universe_scanner_service.py`

### Schedule
- **08:45 IST** — Pre-market scan (primary universe selection)
- **10:00 IST** — Intraday refresh (add midday movers)
- **12:00 IST** — Intraday refresh (add afternoon movers)

### Discovery Sources

#### Source 1: Volume Explosions
- Use DhanHQ `get_candles()` for Nifty 50/Next 50 previous day data, compute volume vs 20-day SMA
- If DhanHQ lacks a bulk "market movers" endpoint, fetch previous day candles for the Nifty 100 universe (one-time daily batch, ~100 API calls at 08:45 — well within rate limits)
- Pull top 30 stocks by volume surge (today's volume vs 20-day average)
- Threshold: stocks trading 2x+ their average volume qualify

#### Source 2: Volatility Breakouts
- Compute from same daily candle batch: stocks with ±2% or more close-to-close change
- Alternatively, if DhanHQ provides a screener/movers endpoint, use that directly
- Fallback: scrape NSE "top gainers/losers" page (public data, no auth needed)
- High ATR expansion = directional opportunity

#### Source 3: News & Events
- Fetch headlines from existing news API (reuse `macro_context_service.py` infrastructure)
- Filter for actionable catalysts: earnings, results, upgrades/downgrades, block deals, bulk deals, board meetings
- Map headlines to stock symbols
- Stocks with fresh catalysts get priority

#### Source 4: Sector Momentum
- Track Nifty sectoral indices (Bank Nifty, IT, Pharma, Auto, Metal, Energy)
- If a sector moves ±1.5%, pull top 5 liquid stocks from that sector
- Sector rotation is one of the most reliable intraday signals

### Scoring (0–100)

| Signal | Weight | Formula |
|--------|--------|---------|
| Volume surge ratio | 25 | `min(vol_ratio / 5, 1.0) × 25` — caps at 5x avg |
| Price move magnitude | 25 | `min(abs(change_pct) / 4, 1.0) × 25` — caps at 4% |
| News catalyst present | 20 | Binary: 20 if catalyst, 0 if not |
| Sector in play | 15 | 15 if parent sector moving ±1.5% |
| Liquidity floor | 15 | `min(avg_daily_turnover / 50cr, 1.0) × 15` |

### Selection Rules
- Minimum liquidity: reject anything below ₹10 crore avg daily turnover
- No duplicate inflation: a stock found by multiple sources gets its highest score, not summed
- Always include stocks with existing open positions
- Pick top 10–15 (configurable via `scanner_pick_count`)

### Intraday Refresh Behavior
- At 10:00 and 12:00, re-run discovery
- **Add** new movers to the universe
- **Never remove** a stock mid-day if already being scanned (continuity for pattern detection)

### Output
- Write to Redis key `yukti:universe` (existing mechanism — `market_scan_service.py` reads this seamlessly)
- Log full scored results to `DecisionLog` for audit

### Fallback Chain
1. If all sources fail → use previous session's universe from Redis
2. If Redis is empty → fetch Nifty 50 constituents from DhanHQ as emergency baseline

### New Config
```python
scanner_pick_count = 15          # how many stocks to trade
min_turnover_cr = 10             # ₹10cr minimum daily turnover
volume_surge_threshold = 2.0     # 2x avg volume to qualify
price_move_threshold = 1.5       # ±1.5% move to qualify
intraday_refresh_times = ["10:00", "12:00"]
```

### Files Affected
- **New:** `services/universe_scanner_service.py`
- **Modified:** `scheduler/jobs.py` (add 08:45, 10:00, 12:00 jobs)
- **Modified:** `config.py` (add scanner config options)

---

## 2. Multi-Timeframe Analysis (Daily + 5-min)

### Core Concept
Before Arjun looks at a 5-min candle, he already knows the daily picture — trend direction, key support/resistance, and whether today's move is with or against the bigger flow.

### Data Layer Changes

#### `signals/indicators.py`
- Parameterize `compute_indicators()` with a `timeframe` parameter:
  ```python
  def compute_indicators(candles, timeframe="5m"):
  ```
- Same existing indicator math works for both timeframes
- **Additional daily-only indicators:**
  - **ADX(14)** — trend strength (noisy on 5-min, highly useful on daily)
  - **Daily Support/Resistance** — swing highs/lows from last 20 sessions (institutional reference levels)

#### `services/market_scan_service.py`
- For each symbol in scan cycle:
  1. Fetch **60-day daily candles** (once per day, cached until next session)
  2. Fetch **3-day 5-min candles** (every cycle, existing behavior)
  3. Compute indicators on both independently

#### Caching Strategy
- Daily candles fetched once at 08:50 IST (after universe scanner runs)
- Cached in Redis with 8-hour TTL (one trading session)
- No repeated API calls for daily data during the day

### Context Layer Changes

#### `signals/context.py`
Build a two-layer context string:

```
=== DAILY TIMEFRAME (Big Picture) ===
Trend: UPTREND (EMA20 > EMA50, ADX 32 = strong trend)
Key Resistance: ₹2,847 (swing high, 3 days ago)
Key Support: ₹2,712 (swing low, 8 days ago)
RSI(14): 58 (neutral-bullish)
Price Position: Trading in upper half of 20-day range
Supertrend: BULLISH since 5 sessions

=== 5-MIN TIMEFRAME (Entry Timing) ===
[existing context — indicators, patterns, VWAP, etc.]
```

#### Daily-Intraday Alignment Signal
Injected into context as explicit label:
- **ALIGNED** — Daily trend matches 5-min setup direction
- **COUNTER-TREND** — 5-min setup goes against daily trend
- **NEUTRAL** — Daily is sideways

### Arjun Prompt Changes

#### `agents/arjun.py`
Add Step 1.5 between Market Bias and Stock Analysis:

```
Step 1.5 — DAILY TIMEFRAME CHECK:
- If daily trend is STRONG (ADX > 25): only trade WITH the trend unless conviction ≥ 9
- If daily is at major resistance: don't go LONG unless breakout confirmed on daily close
- If daily is at major support: don't go SHORT unless breakdown confirmed
- If daily RSI > 75: stock is extended, reduce conviction by 1
- If daily RSI < 25: stock is washed out, reduce conviction by 1
- ALIGNED setups: +1 conviction bonus
- COUNTER-TREND setups: -2 conviction penalty (must still meet minimum)
```

### What This Prevents
- Buying a 5-min breakout while daily is in a downtrend at resistance (trap)
- Shorting a 5-min reversal while daily is at strong support with RSI 28 (bounce incoming)
- Taking momentum trade in a stock already extended 15% in 5 days (late entry)

### New Config
```python
daily_candle_history = 60      # days of daily candles
daily_cache_ttl = 3600 * 8     # cache for 8 hours (one trading session)
```

### Files Affected
- **Modified:** `signals/indicators.py` (add timeframe param, ADX, daily S/R)
- **Modified:** `signals/context.py` (two-layer context builder)
- **Modified:** `services/market_scan_service.py` (fetch + cache daily candles)
- **Modified:** `agents/arjun.py` (Step 1.5 in system prompt)
- **Modified:** `config.py` (daily candle config)

---

## 3. New Strategy Patterns — ORB + VWAP Bounce

Both added to `signals/patterns.py` alongside existing 7 patterns. Same interface: returns `(pattern_name, direction, strength)`.

### Pattern 8: Opening Range Breakout (ORB)

#### Concept
The first 15 minutes (09:15–09:30) define the day's opening range. A breakout above/below that range with volume sets the session's direction.

#### Opening Range Definition
```
OR_High = max(high of first 3 × 5-min candles: 09:15–09:30)
OR_Low  = min(low of first 3 × 5-min candles: 09:15–09:30)
OR_Mid  = (OR_High + OR_Low) / 2
```

#### ORB Long Trigger
- Current candle closes above OR_High
- Volume ratio ≥ 1.5
- RSI between 50–70 (momentum present, not overbought)
- Daily trend is not DOWNTREND
- Time window: **09:30–11:00 only**

#### ORB Short Trigger
- Current candle closes below OR_Low
- Volume ratio ≥ 1.5
- RSI between 30–50
- Daily trend is not UPTREND
- Time window: **09:30–11:00 only**

#### Strength Scoring (0–1)
- Base: 0.5
- +0.15 if volume ratio > 2.0 (strong commitment)
- +0.15 if daily trend is aligned
- +0.10 if opening range is narrow (< 1× ATR) — tight ranges produce explosive breakouts
- +0.10 if price retested OR_High/Low before breaking (confirmation)

#### Suggested Entry/Stop/Target
- Entry: breakout candle close
- Stop Loss: OR_Mid (tight) or opposite end of range (wider)
- Target 1: 1× opening range width from breakout level
- Target 2: 2× opening range width

#### Time Gate
ORB is **invalid after 11:00 IST**. Pattern detector returns nothing after cutoff.

---

### Pattern 9: VWAP Bounce

#### Concept
VWAP acts as an institutional magnet — algos buy below and sell above. A bounce off VWAP in a trending stock is a high-probability entry alongside institutional flow.

#### VWAP Bounce Long
- Price touched or crossed below VWAP in last 2–3 candles
- Current candle closes back above VWAP (the bounce)
- Trend is up (EMA20 > EMA50 on 5-min)
- RSI between 40–60
- Volume on bounce candle > average
- MACD histogram improving

#### VWAP Bounce Short (Rejection)
- Price touched or crossed above VWAP in last 2–3 candles
- Current candle closes back below VWAP
- Trend is down (EMA20 < EMA50)
- RSI between 40–60
- Volume on rejection candle > average
- MACD histogram declining

#### Strength Scoring (0–1)
- Base: 0.5
- +0.15 if daily trend is aligned
- +0.15 if supertrend confirms direction
- +0.10 if bounce was clean (wick touched VWAP, body stayed on correct side)
- +0.10 if Bollinger Band supports (near lower band for long, upper for short)

#### Suggested Entry/Stop/Target
- Entry: bounce candle close
- Stop Loss: VWAP minus 0.5× ATR (for long) — if VWAP can't hold, thesis is broken
- Target 1: nearest swing high/low
- Target 2: 2× stop distance

#### Time Gate
VWAP Bounce is **invalid before 09:45** (VWAP needs volume to stabilize) and **after 14:40** (EOD flow distorts VWAP).

---

### Pattern Integration

- `detect_patterns()` signature updated: `detect_patterns(candles, indicators_5m, indicators_daily=None, current_time=None)`
  - `indicators_daily` passed through from scan cycle (already computed in multi-timeframe step)
  - `current_time` used for ORB/VWAP time-gating
  - When `indicators_daily` is None (e.g., backtest without daily data), ORB/VWAP skip the daily trend filter
- Returns the single best pattern (highest strength) — unchanged behavior
- ORB and VWAP Bounce compete on equal footing with existing patterns
- Time-gating ensures they only activate during valid windows

### New Context for Arjun
Added to 5-min context block regardless of triggered pattern:
```
Opening Range: ₹2,814 – ₹2,832 (range: ₹18, 0.6%)
VWAP: ₹2,821.50 | Price vs VWAP: +0.3% (above)
```

### Arjun Prompt Additions
```
ORB Rules:
- Only valid 09:30–11:00. After 11:00, ignore opening range.
- Narrow opening range (< 1× ATR) breakouts are higher probability.
- If ORB fails (reverses back into range), it becomes a TRAP — do not re-enter same direction.

VWAP Bounce Rules:
- Only valid 09:45–14:40.
- VWAP is where institutions trade. Bounces off VWAP in trending stock are high-probability.
- If VWAP breaks and holds on other side for 2+ candles, trend may be reversing.
```

### Files Affected
- **Modified:** `signals/patterns.py` (add ORB + VWAP Bounce detectors)
- **Modified:** `signals/context.py` (add ORB levels + VWAP to context)
- **Modified:** `agents/arjun.py` (add ORB + VWAP rules to prompt)

---

## Summary of All Changes

| Component | Change Type | Files |
|-----------|------------|-------|
| Discovery Engine | New service | `services/universe_scanner_service.py` |
| Scheduler | Add jobs | `scheduler/jobs.py` |
| Config | New options | `config.py` |
| Indicators | Parameterize timeframe, add ADX + daily S/R | `signals/indicators.py` |
| Context | Two-layer builder, ORB/VWAP data | `signals/context.py` |
| Scan Service | Fetch + cache daily candles | `services/market_scan_service.py` |
| Patterns | ORB + VWAP Bounce | `signals/patterns.py` |
| Arjun | Step 1.5 + ORB/VWAP rules | `agents/arjun.py` |

### What Does NOT Change
- Order state machine (`execution/order_sm.py`) — untouched
- Risk gates (`risk/__init__.py`) — untouched
- Broker abstraction (`execution/broker_factory.py`) — untouched
- DhanHQ client (`execution/dhan_client.py`) — untouched
- Data models (`data/models.py`) — untouched
- Telegram bot, API server, watchdog — untouched
- Crash recovery, reconciliation — untouched

### Risk Assessment
- **Low risk:** All changes are in signal generation / upstream of risk gates
- **Existing safety nets stay intact:** Conviction filtering, position limits, daily loss halt, GTT arming
- **More signals in → same risk gates filter out bad ones → more quality trades out**
