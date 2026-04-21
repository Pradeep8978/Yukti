# Signal Quality Upgrade Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` agent (recommended) or `executing-plans` agent to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade Yukti's signal generation pipeline with dynamic stock discovery, multi-timeframe analysis (daily + 5-min), and two new patterns (ORB + VWAP Bounce) to produce higher-quality, higher-volume trade opportunities.

**Architecture:** Three layered pillars: (1) A new `universe_scanner_service.py` discovers stocks via volume/volatility/news/sector signals and writes to Redis. (2) `indicators.py` gains a `timeframe` parameter, ADX, and daily S/R; `context.py` builds a two-layer context string; `arjun.py` gets Step 1.5 for daily checks. (3) Two new patterns in `patterns.py` with time-gating, integrated into the scan pipeline via `market_scan_service.py`.

**Tech Stack:** Python 3.11+, pandas/pandas_ta, Redis, DhanHQ SDK, APScheduler, pydantic-settings, pytest/pytest-asyncio

---

## File Structure

### Modified files
| File | Responsibility |
|------|---------------|
| `yukti/config.py` | Add scanner config (`scanner_pick_count`, `min_turnover_cr`, etc.) and daily candle config (`daily_candle_history`, `daily_cache_ttl`) |
| `yukti/signals/indicators.py` | Add `timeframe` param to `compute()`, ADX(14), daily S/R. Extend `IndicatorSnapshot` with `adx`, `daily_support`, `daily_resistance` |
| `yukti/signals/patterns.py` | Add `orb_breakout()`, `vwap_bounce()`. Update `scan_all()`/`best_pattern()` to accept `indicators_daily`, `current_time`, `candles` |
| `yukti/signals/context.py` | Two-layer context builder (daily + 5-min sections), alignment signal, ORB levels, VWAP position |
| `yukti/agents/arjun.py` | Add Step 1.5 (daily timeframe check), ORB/VWAP rules in `SYSTEM_PROMPT`. Update `setup_type` enum |
| `yukti/services/market_scan_service.py` | Fetch + cache daily candles, compute daily indicators, pass through pipeline |
| `yukti/scheduler/jobs.py` | Add 08:45, 10:00, 12:00 scanner jobs to `build_scheduler()` |

### New files
| File | Responsibility |
|------|---------------|
| `yukti/services/universe_scanner_service.py` | Discovery engine: 4 sources, scoring, selection, Redis write, fallback chain |
| `tests/unit/test_indicators_daily.py` | Tests for ADX, daily S/R, timeframe parameterization |
| `tests/unit/test_patterns_orb_vwap.py` | Tests for ORB breakout and VWAP bounce patterns |
| `tests/unit/test_universe_scanner.py` | Tests for scanner scoring logic and selection rules |
| `tests/unit/test_context_two_layer.py` | Tests for two-layer context builder and alignment signal |

---

## Task 1: Config — Add New Settings

**Files:**
- Modify: `yukti/config.py:14-107`
- Test: `tests/unit/test_config.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_config.py`:

```python
"""tests/unit/test_config.py — verify new config fields exist with correct defaults."""
from __future__ import annotations

import pytest
from yukti.config import Settings


class TestScannerConfig:
    def test_scanner_pick_count_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.scanner_pick_count == 15

    def test_min_turnover_cr_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.min_turnover_cr == 10

    def test_volume_surge_threshold_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.volume_surge_threshold == 2.0

    def test_price_move_threshold_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.price_move_threshold == 1.5

    def test_intraday_refresh_times_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.intraday_refresh_times == ["10:00", "12:00"]


class TestDailyCandleConfig:
    def test_daily_candle_history_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.daily_candle_history == 60

    def test_daily_cache_ttl_default(self):
        s = Settings(dhan_client_id="x", dhan_access_token="x")
        assert s.daily_cache_ttl == 3600 * 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd d:\yukti && uv run pytest tests/unit/test_config.py -v`
Expected: FAIL with `AttributeError` — fields don't exist yet.

- [ ] **Step 3: Add new config fields to Settings**

In `yukti/config.py`, add these fields inside the `Settings` class, after the `# ── Scheduler times (IST)` block (around line 93) and before the `# ── DhanHQ constants` block:

```python
    # ── Universe scanner ──────────────────────────────
    scanner_pick_count: int = Field(default=15, ge=5, le=50)
    min_turnover_cr: float = Field(default=10, gt=0)
    volume_surge_threshold: float = Field(default=2.0, gt=0)
    price_move_threshold: float = Field(default=1.5, gt=0)
    intraday_refresh_times: list[str] = Field(default_factory=lambda: ["10:00", "12:00"])

    # ── Daily candle (multi-timeframe) ────────────────
    daily_candle_history: int = Field(default=60, ge=20, le=200)
    daily_cache_ttl: int = Field(default=3600 * 8, ge=3600)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd d:\yukti && uv run pytest tests/unit/test_config.py -v`
Expected: PASS — all 7 tests green.

- [ ] **Step 5: Commit**

```bash
cd d:\yukti
git add yukti/config.py tests/unit/test_config.py
git commit -m "feat(config): add scanner and daily candle settings"
```

---

## Task 2: Indicators — Parameterize Timeframe, Add ADX and Daily S/R

**Files:**
- Modify: `yukti/signals/indicators.py:1-160`
- Test: `tests/unit/test_indicators_daily.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_indicators_daily.py`:

```python
"""tests/unit/test_indicators_daily.py — tests for timeframe param, ADX, daily S/R."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from yukti.signals.indicators import compute, IndicatorSnapshot


def _make_daily_ohlcv(n: int = 80, start: float = 1000.0, trend: float = 0.002) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    closes = start * np.cumprod(1 + trend + rng.normal(0, 0.01, n))
    highs = closes * (1 + abs(rng.normal(0, 0.008, n)))
    lows = closes * (1 - abs(rng.normal(0, 0.008, n)))
    opens = np.roll(closes, 1); opens[0] = start
    vols = rng.normal(5_000_000, 1_000_000, n).clip(1)
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols})
    df.index = pd.bdate_range("2024-01-02", periods=n)
    return df


class TestTimeframeParam:
    def test_compute_accepts_timeframe_5m(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="5m")
        assert isinstance(snap, IndicatorSnapshot)

    def test_compute_accepts_timeframe_daily(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="daily")
        assert isinstance(snap, IndicatorSnapshot)

    def test_daily_timeframe_has_adx(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="daily")
        assert hasattr(snap, "adx")
        assert snap.adx is not None
        assert 0 <= snap.adx <= 100

    def test_5m_timeframe_adx_is_none(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="5m")
        assert snap.adx is None

    def test_daily_timeframe_has_support_resistance(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="daily")
        assert hasattr(snap, "daily_support")
        assert hasattr(snap, "daily_resistance")
        assert snap.daily_support is not None
        assert snap.daily_resistance is not None
        assert snap.daily_support < snap.daily_resistance

    def test_5m_timeframe_sr_is_none(self):
        df = _make_daily_ohlcv(80)
        snap = compute(df, timeframe="5m")
        assert snap.daily_support is None
        assert snap.daily_resistance is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd d:\yukti && uv run pytest tests/unit/test_indicators_daily.py -v`
Expected: FAIL — `compute()` doesn't accept `timeframe`, `IndicatorSnapshot` missing `adx`/`daily_support`/`daily_resistance`.

- [ ] **Step 3: Add new fields to IndicatorSnapshot**

In `yukti/signals/indicators.py`, add three new fields at the end of the `IndicatorSnapshot` dataclass, after `candle_change_pct`:

```python
    # Daily-only (None when timeframe="5m")
    adx:              float | None = None
    daily_support:    float | None = None
    daily_resistance: float | None = None
```

- [ ] **Step 4: Add timeframe parameter to compute()**

Update the `compute()` function signature from:

```python
def compute(df: pd.DataFrame, swing_lookback: int = 20) -> IndicatorSnapshot:
```

to:

```python
def compute(df: pd.DataFrame, swing_lookback: int = 20, timeframe: str = "5m") -> IndicatorSnapshot:
```

- [ ] **Step 5: Add ADX computation for daily timeframe**

After the Bollinger Bands section and before the Volume SMA section in `compute()`, add:

```python
    # ── ADX (daily timeframe only — noisy on 5-min) ─────────────────
    adx_value = None
    daily_support_val = None
    daily_resistance_val = None
    if timeframe == "daily":
        adx_series = ta.adx(df["high"], df["low"], df["close"], length=14)
        if adx_series is not None and "ADX_14" in adx_series.columns:
            adx_val = adx_series["ADX_14"].iloc[-1]
            adx_value = float(adx_val) if pd.notna(adx_val) else 0.0

        # Daily S/R — swing highs/lows from last 20 sessions
        sr_window = df.tail(20)
        daily_resistance_val = float(sr_window["high"].nlargest(3).mean())
        daily_support_val = float(sr_window["low"].nsmallest(3).mean())
```

- [ ] **Step 6: Pass new fields in the return statement**

Update the `return IndicatorSnapshot(...)` at the end of `compute()` to include the three new fields:

```python
        adx              = adx_value,
        daily_support    = daily_support_val,
        daily_resistance = daily_resistance_val,
```

These three lines go at the end of the `IndicatorSnapshot(...)` constructor call, after `candle_change_pct`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd d:\yukti && uv run pytest tests/unit/test_indicators_daily.py tests/unit/test_signals.py -v`
Expected: All tests PASS (new tests + existing tests still green).

- [ ] **Step 8: Commit**

```bash
cd d:\yukti
git add yukti/signals/indicators.py tests/unit/test_indicators_daily.py
git commit -m "feat(indicators): add timeframe param, ADX, daily S/R"
```

---

## Task 3: Patterns — Add ORB Breakout

**Files:**
- Modify: `yukti/signals/patterns.py:1-180`
- Test: `tests/unit/test_patterns_orb_vwap.py` (new)

- [ ] **Step 1: Write the failing tests for ORB**

Create `tests/unit/test_patterns_orb_vwap.py`:

```python
"""tests/unit/test_patterns_orb_vwap.py — tests for ORB and VWAP Bounce patterns."""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd
import pytest

from yukti.signals.indicators import compute, IndicatorSnapshot
from yukti.signals.patterns import orb_breakout, PatternSignal


def _make_snap(**overrides) -> IndicatorSnapshot:
    """Build an IndicatorSnapshot with controllable values."""
    defaults = dict(
        close=1020.0, high=1025.0, low=1010.0, open=1015.0, volume=600_000,
        ema20=1010.0, ema50=1000.0, vwap=1012.0,
        supertrend=1005.0, supertrend_bull=True,
        rsi=58.0, macd=0.5, macd_sig=0.3, macd_hist=0.2, macd_bull=True,
        atr=15.0, bb_upper=1030.0, bb_mid=1010.0, bb_lower=990.0,
        volume_sma20=400_000, volume_ratio=1.5,
        trend="UPTREND", nearest_swing_high=1025.0, nearest_swing_low=990.0,
        prev_close=1010.0, candle_change_pct=1.0,
        adx=None, daily_support=None, daily_resistance=None,
    )
    defaults.update(overrides)
    return IndicatorSnapshot(**defaults)


def _make_orb_candles() -> pd.DataFrame:
    """Create candles with a clear opening range (first 3 candles)."""
    # 3 opening range candles (09:15-09:30) + 5 post-opening candles
    data = {
        "open":   [1000, 1005, 1003, 1010, 1015, 1020, 1018, 1025],
        "high":   [1008, 1012, 1010, 1018, 1022, 1028, 1025, 1030],
        "low":    [ 995,  998,  997, 1005, 1010, 1015, 1012, 1020],
        "close":  [1005, 1003, 1008, 1015, 1020, 1025, 1022, 1028],
        "volume": [500000]*8,
    }
    times = pd.date_range("2024-01-02 09:15", periods=8, freq="5min")
    return pd.DataFrame(data, index=times)


class TestORBBreakout:
    def test_orb_detected_above_range(self):
        candles = _make_orb_candles()
        snap = _make_snap(
            close=1028.0, rsi=60.0, volume_ratio=1.8,
        )
        result = orb_breakout(snap, candles, current_time=time(9, 45))
        assert result.detected is True
        assert result.pattern_type == "orb_breakout"
        assert result.strength > 0

    def test_orb_not_detected_inside_range(self):
        candles = _make_orb_candles()
        snap = _make_snap(close=1005.0, rsi=55.0, volume_ratio=1.0)
        result = orb_breakout(snap, candles, current_time=time(9, 45))
        assert result.detected is False

    def test_orb_rejected_after_1100(self):
        candles = _make_orb_candles()
        snap = _make_snap(close=1028.0, rsi=60.0, volume_ratio=1.8)
        result = orb_breakout(snap, candles, current_time=time(11, 15))
        assert result.detected is False

    def test_orb_rejected_before_0930(self):
        candles = _make_orb_candles()
        snap = _make_snap(close=1028.0, rsi=60.0, volume_ratio=1.8)
        result = orb_breakout(snap, candles, current_time=time(9, 20))
        assert result.detected is False

    def test_orb_short_below_range(self):
        candles = _make_orb_candles()
        # OR_Low is min(low of first 3) = 995
        snap = _make_snap(
            close=990.0, rsi=40.0, volume_ratio=1.8,
            trend="DOWNTREND", supertrend_bull=False, macd_bull=False,
        )
        result = orb_breakout(snap, candles, current_time=time(9, 45))
        assert result.detected is True
        assert "SHORT" in result.notes or "short" in result.notes.lower() or result.strength > 0

    def test_orb_strength_range(self):
        candles = _make_orb_candles()
        snap = _make_snap(close=1028.0, rsi=60.0, volume_ratio=2.5)
        result = orb_breakout(snap, candles, current_time=time(9, 45))
        if result.detected:
            assert 0.0 < result.strength <= 1.0

    def test_orb_returns_valid_signal(self):
        candles = _make_orb_candles()
        snap = _make_snap(close=1005.0)
        result = orb_breakout(snap, candles, current_time=time(10, 0))
        assert isinstance(result, PatternSignal)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd d:\yukti && uv run pytest tests/unit/test_patterns_orb_vwap.py::TestORBBreakout -v`
Expected: FAIL — `orb_breakout` not found in `yukti.signals.patterns`.

- [ ] **Step 3: Implement orb_breakout()**

Add to `yukti/signals/patterns.py`, after the `momentum_long()` function and before `scan_all()`:

```python
def orb_breakout(
    snap: IndicatorSnapshot,
    candles: pd.DataFrame,
    current_time: "time | None" = None,
    indicators_daily: IndicatorSnapshot | None = None,
) -> PatternSignal:
    """
    Opening Range Breakout: first 15 min (3 × 5-min candles) define the range.
    Breakout above OR_High → LONG. Breakdown below OR_Low → SHORT.
    Valid 09:30–11:00 only.
    """
    from datetime import time as dt_time

    # Time gate
    if current_time is None or current_time < dt_time(9, 30) or current_time > dt_time(11, 0):
        return PatternSignal(False, "orb_breakout", 0.0, "")

    # Need at least 3 candles for opening range
    if candles is None or len(candles) < 3:
        return PatternSignal(False, "orb_breakout", 0.0, "")

    # Compute opening range from first 3 candles
    or_candles = candles.iloc[:3]
    or_high = float(or_candles["high"].max())
    or_low = float(or_candles["low"].min())
    or_mid = (or_high + or_low) / 2

    # Determine direction
    long_break = snap.close > or_high
    short_break = snap.close < or_low

    if not long_break and not short_break:
        return PatternSignal(False, "orb_breakout", 0.0, "")

    # Daily trend filter (skip if daily data unavailable)
    daily_trend = indicators_daily.trend if indicators_daily else "SIDEWAYS"

    if long_break:
        vol_ok = snap.volume_ratio >= 1.5
        rsi_ok = 50 <= snap.rsi <= 70
        daily_ok = daily_trend != "DOWNTREND"
        if not vol_ok or not rsi_ok or not daily_ok:
            return PatternSignal(False, "orb_breakout", 0.0, "")

        # Strength scoring
        strength = 0.5
        if snap.volume_ratio > 2.0:
            strength += 0.15
        if daily_trend == "UPTREND":
            strength += 0.15
        or_range = or_high - or_low
        if snap.atr > 0 and or_range < snap.atr:
            strength += 0.10
        # Retest bonus: price was near OR_High before breaking
        if snap.prev_close <= or_high * 1.002:
            strength += 0.10

        notes = (
            f"ORB LONG: close ₹{snap.close:.2f} > OR_High ₹{or_high:.2f} "
            f"| range ₹{or_range:.2f} | vol {snap.volume_ratio:.1f}×"
            f"{' | daily aligned' if daily_trend == 'UPTREND' else ''}"
        )
        return PatternSignal(True, "orb_breakout", round(min(strength, 1.0), 2), notes)

    if short_break:
        vol_ok = snap.volume_ratio >= 1.5
        rsi_ok = 30 <= snap.rsi <= 50
        daily_ok = daily_trend != "UPTREND"
        if not vol_ok or not rsi_ok or not daily_ok:
            return PatternSignal(False, "orb_breakout", 0.0, "")

        strength = 0.5
        if snap.volume_ratio > 2.0:
            strength += 0.15
        if daily_trend == "DOWNTREND":
            strength += 0.15
        or_range = or_high - or_low
        if snap.atr > 0 and or_range < snap.atr:
            strength += 0.10
        if snap.prev_close >= or_low * 0.998:
            strength += 0.10

        notes = (
            f"ORB SHORT: close ₹{snap.close:.2f} < OR_Low ₹{or_low:.2f} "
            f"| range ₹{or_range:.2f} | vol {snap.volume_ratio:.1f}×"
            f"{' | daily aligned' if daily_trend == 'DOWNTREND' else ''}"
        )
        return PatternSignal(True, "orb_breakout", round(min(strength, 1.0), 2), notes)

    return PatternSignal(False, "orb_breakout", 0.0, "")
```

Also add the `import pandas as pd` at the top of the file (after the existing imports) and add `from datetime import time` to the module-level imports or keep it inline as shown.

Add `import pandas as pd` after `from yukti.signals.indicators import IndicatorSnapshot`:

```python
import pandas as pd
```

- [ ] **Step 4: Run ORB tests to verify they pass**

Run: `cd d:\yukti && uv run pytest tests/unit/test_patterns_orb_vwap.py::TestORBBreakout -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd d:\yukti
git add yukti/signals/patterns.py tests/unit/test_patterns_orb_vwap.py
git commit -m "feat(patterns): add Opening Range Breakout (ORB) pattern"
```

---

## Task 4: Patterns — Add VWAP Bounce

**Files:**
- Modify: `yukti/signals/patterns.py`
- Modify: `tests/unit/test_patterns_orb_vwap.py`

- [ ] **Step 1: Write the failing tests for VWAP Bounce**

Append to `tests/unit/test_patterns_orb_vwap.py`:

```python
from yukti.signals.patterns import vwap_bounce


def _make_vwap_candles_long() -> pd.DataFrame:
    """Candles where price dips below VWAP then bounces back above."""
    data = {
        "open":   [1010, 1008, 1003, 1000, 1005, 1010],
        "high":   [1015, 1012, 1008, 1006, 1012, 1018],
        "low":    [1005, 1002,  998,  996, 1002, 1008],
        "close":  [1008, 1003, 1000, 1005, 1010, 1015],
        "volume": [400000, 350000, 450000, 500000, 550000, 600000],
    }
    times = pd.date_range("2024-01-02 10:00", periods=6, freq="5min")
    return pd.DataFrame(data, index=times)


class TestVWAPBounce:
    def test_vwap_long_detected(self):
        candles = _make_vwap_candles_long()
        snap = _make_snap(
            close=1015.0, vwap=1005.0, rsi=52.0, volume_ratio=1.3,
            ema20=1010.0, ema50=1000.0, macd_bull=True, macd_hist=0.2,
        )
        result = vwap_bounce(snap, candles, current_time=time(10, 30))
        assert result.detected is True
        assert result.pattern_type == "vwap_bounce"
        assert result.strength > 0

    def test_vwap_not_detected_before_0945(self):
        candles = _make_vwap_candles_long()
        snap = _make_snap(close=1015.0, vwap=1005.0, rsi=52.0)
        result = vwap_bounce(snap, candles, current_time=time(9, 30))
        assert result.detected is False

    def test_vwap_not_detected_after_1440(self):
        candles = _make_vwap_candles_long()
        snap = _make_snap(close=1015.0, vwap=1005.0, rsi=52.0)
        result = vwap_bounce(snap, candles, current_time=time(14, 50))
        assert result.detected is False

    def test_vwap_short_rejection(self):
        candles = _make_vwap_candles_long()
        snap = _make_snap(
            close=995.0, vwap=1005.0, rsi=48.0, volume_ratio=1.3,
            ema20=998.0, ema50=1010.0, trend="DOWNTREND",
            supertrend_bull=False, macd_bull=False, macd_hist=-0.3,
        )
        result = vwap_bounce(snap, candles, current_time=time(10, 30))
        assert result.detected is True
        assert "SHORT" in result.notes or "short" in result.notes.lower() or "rejection" in result.notes.lower()

    def test_vwap_returns_valid_signal(self):
        candles = _make_vwap_candles_long()
        snap = _make_snap(close=1005.0, vwap=1005.0, rsi=50.0)
        result = vwap_bounce(snap, candles, current_time=time(10, 30))
        assert isinstance(result, PatternSignal)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd d:\yukti && uv run pytest tests/unit/test_patterns_orb_vwap.py::TestVWAPBounce -v`
Expected: FAIL — `vwap_bounce` not found.

- [ ] **Step 3: Implement vwap_bounce()**

Add to `yukti/signals/patterns.py`, after `orb_breakout()` and before `scan_all()`:

```python
def vwap_bounce(
    snap: IndicatorSnapshot,
    candles: pd.DataFrame,
    current_time: "time | None" = None,
    indicators_daily: IndicatorSnapshot | None = None,
) -> PatternSignal:
    """
    VWAP Bounce: price touches/crosses VWAP then bounces back.
    Institutional magnet — high probability in trending stocks.
    Valid 09:45–14:40 only.
    """
    from datetime import time as dt_time

    # Time gate
    if current_time is None or current_time < dt_time(9, 45) or current_time > dt_time(14, 40):
        return PatternSignal(False, "vwap_bounce", 0.0, "")

    if candles is None or len(candles) < 3:
        return PatternSignal(False, "vwap_bounce", 0.0, "")

    vwap = snap.vwap
    daily_trend = indicators_daily.trend if indicators_daily else "SIDEWAYS"

    # Check if price touched VWAP in recent candles (last 2-3)
    recent = candles.iloc[-3:] if len(candles) >= 3 else candles
    recent_lows = recent["low"].values
    recent_highs = recent["high"].values

    touched_below = any(float(lo) <= vwap for lo in recent_lows)
    touched_above = any(float(hi) >= vwap for hi in recent_highs)

    # VWAP Bounce Long
    if snap.close > vwap and touched_below:
        uptrend = snap.ema20 > snap.ema50
        rsi_ok = 40 <= snap.rsi <= 60
        vol_ok = snap.volume_ratio > 1.0
        macd_improving = snap.macd_hist > 0 or snap.macd_bull

        if not uptrend or not rsi_ok or not vol_ok or not macd_improving:
            # Check short side
            pass
        else:
            strength = 0.5
            if daily_trend == "UPTREND":
                strength += 0.15
            if snap.supertrend_bull:
                strength += 0.15
            # Clean bounce: wick touched VWAP, body stayed above
            bounce_candle = candles.iloc[-1]
            if float(bounce_candle["low"]) <= vwap <= float(bounce_candle["open"]):
                strength += 0.10
            if snap.close < snap.bb_mid:
                strength += 0.10

            notes = (
                f"VWAP Bounce LONG: close ₹{snap.close:.2f} > VWAP ₹{vwap:.2f} "
                f"| RSI {snap.rsi:.1f} | vol {snap.volume_ratio:.1f}×"
                f"{' | daily aligned' if daily_trend == 'UPTREND' else ''}"
            )
            return PatternSignal(True, "vwap_bounce", round(min(strength, 1.0), 2), notes)

    # VWAP Bounce Short (Rejection)
    if snap.close < vwap and touched_above:
        downtrend = snap.ema20 < snap.ema50
        rsi_ok = 40 <= snap.rsi <= 60
        vol_ok = snap.volume_ratio > 1.0
        macd_declining = snap.macd_hist < 0 or not snap.macd_bull

        if downtrend and rsi_ok and vol_ok and macd_declining:
            strength = 0.5
            if daily_trend == "DOWNTREND":
                strength += 0.15
            if not snap.supertrend_bull:
                strength += 0.15
            bounce_candle = candles.iloc[-1]
            if float(bounce_candle["high"]) >= vwap >= float(bounce_candle["open"]):
                strength += 0.10
            if snap.close > snap.bb_mid:
                strength += 0.10

            notes = (
                f"VWAP Bounce SHORT (rejection): close ₹{snap.close:.2f} < VWAP ₹{vwap:.2f} "
                f"| RSI {snap.rsi:.1f} | vol {snap.volume_ratio:.1f}×"
                f"{' | daily aligned' if daily_trend == 'DOWNTREND' else ''}"
            )
            return PatternSignal(True, "vwap_bounce", round(min(strength, 1.0), 2), notes)

    return PatternSignal(False, "vwap_bounce", 0.0, "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd d:\yukti && uv run pytest tests/unit/test_patterns_orb_vwap.py -v`
Expected: All ORB + VWAP tests PASS.

- [ ] **Step 5: Commit**

```bash
cd d:\yukti
git add yukti/signals/patterns.py tests/unit/test_patterns_orb_vwap.py
git commit -m "feat(patterns): add VWAP Bounce pattern"
```

---

## Task 5: Patterns — Update scan_all() and best_pattern() Signatures

**Files:**
- Modify: `yukti/signals/patterns.py`
- Modify: `tests/unit/test_signals.py` (existing tests must still pass)

- [ ] **Step 1: Write a failing test for the new signatures**

Add to the bottom of `tests/unit/test_patterns_orb_vwap.py`:

```python
from yukti.signals.patterns import scan_all, best_pattern


def _make_ohlcv_for_scan(n: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    closes = 1000 * np.cumprod(1 + 0.001 + rng.normal(0, 0.005, n))
    highs = closes * (1 + abs(rng.normal(0, 0.003, n)))
    lows = closes * (1 - abs(rng.normal(0, 0.003, n)))
    opens = np.roll(closes, 1); opens[0] = 1000.0
    vols = rng.normal(500_000, 100_000, n).clip(1)
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols})
    df.index = pd.date_range("2024-01-02 09:15", periods=n, freq="5min")
    return df


class TestScanAllUpdated:
    def test_scan_all_accepts_new_params(self):
        snap = _make_snap()
        candles = _make_ohlcv_for_scan()
        results = scan_all(snap, candles=candles, indicators_daily=None, current_time=time(10, 0))
        assert isinstance(results, list)

    def test_scan_all_backward_compatible(self):
        """Calling scan_all with just snap still works."""
        snap = _make_snap()
        results = scan_all(snap)
        assert isinstance(results, list)

    def test_best_pattern_accepts_new_params(self):
        snap = _make_snap()
        candles = _make_ohlcv_for_scan()
        result = best_pattern(snap, candles=candles, indicators_daily=None, current_time=time(10, 0))
        # Result is PatternSignal or None
        assert result is None or isinstance(result, PatternSignal)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd d:\yukti && uv run pytest tests/unit/test_patterns_orb_vwap.py::TestScanAllUpdated -v`
Expected: FAIL — `scan_all()` doesn't accept new parameters.

- [ ] **Step 3: Update scan_all() and best_pattern()**

Replace the existing `scan_all()` and `best_pattern()` functions in `yukti/signals/patterns.py` with:

```python
def scan_all(
    snap: IndicatorSnapshot,
    candles: pd.DataFrame | None = None,
    indicators_daily: IndicatorSnapshot | None = None,
    current_time: "time | None" = None,
) -> list[PatternSignal]:
    """
    Run all pattern detectors and return detected signals sorted by strength.
    """
    all_patterns = [
        breakout(snap),
        breakdown(snap),
        trend_pullback_long(snap),
        trend_pullback_short(snap),
        reversal_long(snap),
        reversal_short(snap),
        momentum_long(snap),
        orb_breakout(snap, candles, current_time, indicators_daily),
        vwap_bounce(snap, candles, current_time, indicators_daily),
    ]
    detected = [p for p in all_patterns if p.detected]
    return sorted(detected, key=lambda p: p.strength, reverse=True)


def best_pattern(
    snap: IndicatorSnapshot,
    candles: pd.DataFrame | None = None,
    indicators_daily: IndicatorSnapshot | None = None,
    current_time: "time | None" = None,
) -> PatternSignal | None:
    """Return the single highest-strength detected pattern, or None."""
    found = scan_all(snap, candles, indicators_daily, current_time)
    return found[0] if found else None
```

- [ ] **Step 4: Run all pattern tests (new + existing)**

Run: `cd d:\yukti && uv run pytest tests/unit/test_patterns_orb_vwap.py tests/unit/test_signals.py -v`
Expected: ALL PASS. Existing `test_signals.py` tests still work because `scan_all(snap)` and `best_pattern(snap)` are backward compatible (new params default to `None`).

- [ ] **Step 5: Commit**

```bash
cd d:\yukti
git add yukti/signals/patterns.py tests/unit/test_patterns_orb_vwap.py
git commit -m "feat(patterns): update scan_all/best_pattern for ORB+VWAP integration"
```

---

## Task 6: Context — Two-Layer Context Builder with Alignment Signal

**Files:**
- Modify: `yukti/signals/context.py:1-100`
- Test: `tests/unit/test_context_two_layer.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_context_two_layer.py`:

```python
"""tests/unit/test_context_two_layer.py — tests for two-layer context + alignment."""
from __future__ import annotations

import pytest

from yukti.signals.indicators import IndicatorSnapshot


def _make_snap(**overrides) -> IndicatorSnapshot:
    defaults = dict(
        close=1020.0, high=1025.0, low=1010.0, open=1015.0, volume=600_000,
        ema20=1010.0, ema50=1000.0, vwap=1012.0,
        supertrend=1005.0, supertrend_bull=True,
        rsi=58.0, macd=0.5, macd_sig=0.3, macd_hist=0.2, macd_bull=True,
        atr=15.0, bb_upper=1030.0, bb_mid=1010.0, bb_lower=990.0,
        volume_sma20=400_000, volume_ratio=1.5,
        trend="UPTREND", nearest_swing_high=1025.0, nearest_swing_low=990.0,
        prev_close=1010.0, candle_change_pct=1.0,
        adx=None, daily_support=None, daily_resistance=None,
    )
    defaults.update(overrides)
    return IndicatorSnapshot(**defaults)


class TestAlignmentSignal:
    def test_compute_alignment_aligned(self):
        from yukti.signals.context import compute_alignment
        daily = _make_snap(trend="UPTREND")
        assert compute_alignment(daily, "LONG") == "ALIGNED"

    def test_compute_alignment_counter_trend(self):
        from yukti.signals.context import compute_alignment
        daily = _make_snap(trend="UPTREND")
        assert compute_alignment(daily, "SHORT") == "COUNTER-TREND"

    def test_compute_alignment_neutral(self):
        from yukti.signals.context import compute_alignment
        daily = _make_snap(trend="SIDEWAYS")
        assert compute_alignment(daily, "LONG") == "NEUTRAL"


class TestTwoLayerContext:
    def test_daily_section_present(self):
        from yukti.signals.context import build_context
        from unittest.mock import MagicMock

        snap_5m = _make_snap()
        snap_daily = _make_snap(
            trend="UPTREND", adx=32.0,
            daily_support=980.0, daily_resistance=1050.0,
        )
        macro = MagicMock()
        macro.nifty_chg_pct = 0.5
        macro.nifty_trend = "UP"
        macro.vix_label = "15.0 (moderate)"
        macro.fii_label = "+₹500 Cr (buying)"
        macro.dii_label = "+₹300 Cr (buying)"
        macro.headlines_text = "  None available"
        perf = {
            "consecutive_losses": 0, "daily_pnl_pct": 0.0,
            "win_rate_last_10": 0.6, "trades_today": 0,
        }
        ctx = build_context("RELIANCE", snap_5m, macro, perf, indicators_daily=snap_daily)
        assert "DAILY TIMEFRAME" in ctx
        assert "ADX" in ctx
        assert "Alignment" in ctx or "ALIGNED" in ctx

    def test_orb_vwap_section_present(self):
        from yukti.signals.context import build_context
        from unittest.mock import MagicMock

        snap_5m = _make_snap()
        macro = MagicMock()
        macro.nifty_chg_pct = 0.5
        macro.nifty_trend = "UP"
        macro.vix_label = "15.0"
        macro.fii_label = "N/A"
        macro.dii_label = "N/A"
        macro.headlines_text = "  None"
        perf = {
            "consecutive_losses": 0, "daily_pnl_pct": 0.0,
            "win_rate_last_10": 0.6, "trades_today": 0,
        }
        ctx = build_context(
            "RELIANCE", snap_5m, macro, perf,
            or_high=1020.0, or_low=1000.0,
        )
        assert "Opening Range" in ctx
        assert "VWAP" in ctx

    def test_backward_compatible_no_daily(self):
        from yukti.signals.context import build_context
        from unittest.mock import MagicMock

        snap_5m = _make_snap()
        macro = MagicMock()
        macro.nifty_chg_pct = 0.0
        macro.nifty_trend = "SIDEWAYS"
        macro.vix_label = "N/A"
        macro.fii_label = "N/A"
        macro.dii_label = "N/A"
        macro.headlines_text = "  None"
        perf = {
            "consecutive_losses": 0, "daily_pnl_pct": 0.0,
            "win_rate_last_10": 0.5, "trades_today": 0,
        }
        # No daily indicators, no ORB data — should still work
        ctx = build_context("RELIANCE", snap_5m, macro, perf)
        assert "STOCK: RELIANCE" in ctx
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd d:\yukti && uv run pytest tests/unit/test_context_two_layer.py -v`
Expected: FAIL — `compute_alignment` not found; `build_context` doesn't accept `indicators_daily`, `or_high`, `or_low`.

- [ ] **Step 3: Add compute_alignment() function**

Add at the top of `yukti/signals/context.py`, after the imports and before `build_context`:

```python
def compute_alignment(
    indicators_daily: "IndicatorSnapshot | None",
    direction: str,
) -> str:
    """
    Compute alignment between daily trend and trade direction.
    Returns: ALIGNED | COUNTER-TREND | NEUTRAL
    """
    if indicators_daily is None:
        return "NEUTRAL"

    daily_trend = indicators_daily.trend
    if daily_trend == "SIDEWAYS":
        return "NEUTRAL"

    if direction == "LONG" and daily_trend == "UPTREND":
        return "ALIGNED"
    if direction == "SHORT" and daily_trend == "DOWNTREND":
        return "ALIGNED"
    if direction == "LONG" and daily_trend == "DOWNTREND":
        return "COUNTER-TREND"
    if direction == "SHORT" and daily_trend == "UPTREND":
        return "COUNTER-TREND"

    return "NEUTRAL"
```

- [ ] **Step 4: Update build_context() signature and body**

Update the `build_context()` function signature to add new optional parameters:

```python
def build_context(
    symbol: str,
    snap: IndicatorSnapshot,
    macro: "MacroContext",
    perf: dict,
    past_journal: str = "",
    symbol_headlines: list[str] | None = None,
    indicators_daily: "IndicatorSnapshot | None" = None,
    or_high: float | None = None,
    or_low: float | None = None,
) -> str:
```

Inside `build_context()`, after the `loss_note` block and before the main `return f"""` string, add a helper to build the daily section:

```python
    # ── Daily timeframe section ──────────────────────────────────────
    daily_section = ""
    if indicators_daily is not None:
        adx_str = f"{indicators_daily.adx:.0f}" if indicators_daily.adx is not None else "N/A"
        adx_label = ""
        if indicators_daily.adx is not None:
            if indicators_daily.adx > 25:
                adx_label = " = strong trend"
            elif indicators_daily.adx > 20:
                adx_label = " = moderate trend"
            else:
                adx_label = " = weak/no trend"

        sup_str = f"₹{indicators_daily.daily_support:.2f}" if indicators_daily.daily_support else "N/A"
        res_str = f"₹{indicators_daily.daily_resistance:.2f}" if indicators_daily.daily_resistance else "N/A"

        st_daily = "BULLISH" if indicators_daily.supertrend_bull else "BEARISH"

        daily_section = f"""
╔══ DAILY TIMEFRAME (Big Picture) ═══════════════════════════════╗
  Trend            : {indicators_daily.trend} (EMA20 {'>' if indicators_daily.ema20 > indicators_daily.ema50 else '<'} EMA50, ADX {adx_str}{adx_label})
  Key Resistance   : {res_str}
  Key Support      : {sup_str}
  RSI(14)          : {indicators_daily.rsi:.1f}
  Supertrend       : {st_daily} since recent sessions
  Alignment        : {{alignment}}
╚════════════════════════════════════════════════════════════════╝
"""

    # ── ORB + VWAP section ───────────────────────────────────────────
    orb_vwap_section = ""
    or_parts = []
    if or_high is not None and or_low is not None:
        or_range = or_high - or_low
        or_pct = (or_range / or_low * 100) if or_low > 0 else 0
        or_parts.append(f"  Opening Range    : ₹{or_low:.2f} – ₹{or_high:.2f} (range: ₹{or_range:.2f}, {or_pct:.1f}%)")
    vwap_vs = ((snap.close - snap.vwap) / snap.vwap * 100) if snap.vwap > 0 else 0
    vwap_side = "above" if snap.close > snap.vwap else "below"
    or_parts.append(f"  VWAP             : ₹{snap.vwap:.2f} | Price vs VWAP: {vwap_vs:+.1f}% ({vwap_side})")
    orb_vwap_section = "\n".join(or_parts)
```

Then in the main `return f"""` block, insert the daily section right after the `MARKET CONTEXT` and `PERFORMANCE STATE` boxes and before the `STOCK:` box. Insert the ORB/VWAP section inside the `STOCK:` box after the `Market structure` section.

The `{alignment}` placeholder in the daily section gets filled using `.format(alignment=...)` — or use an f-string variable. Since the alignment depends on the direction (which is unknown at context build time), use a generic label. Replace `{{alignment}}` with a pre-computed value:

Before the daily_section block, compute a default alignment:

```python
    # Alignment is direction-dependent; inject both possible values for Arjun to evaluate
    # Default: compute based on 5-min trend as a proxy
    alignment_label = "NEUTRAL"
    if indicators_daily is not None:
        snap_dir = "LONG" if snap.trend == "UPTREND" else "SHORT" if snap.trend == "DOWNTREND" else ""
        if snap_dir:
            alignment_label = compute_alignment(indicators_daily, snap_dir)
```

And use `{alignment_label}` directly in the daily_section f-string instead of `{{alignment}}`.

The full updated return block should include `{daily_section}` before the STOCK box and `{orb_vwap_section}` inside it. Here is the updated return statement — replace the entire existing return block with this:

```python
    return f"""
╔══ MARKET CONTEXT ══════════════════════════════════════════════╗
  Nifty50 change   : {macro.nifty_chg_pct:+.2f}%
  Nifty trend      : {macro.nifty_trend}
  India VIX        : {macro.vix_label}
  FII flows today  : {macro.fii_label}
  DII flows today  : {macro.dii_label}
  Time (IST)       : {datetime.now().strftime("%H:%M")}
  Headlines        :
{macro.headlines_text}
╚════════════════════════════════════════════════════════════════╝

╔══ YOUR PERFORMANCE STATE ══════════════════════════════════════╗
  Consecutive losses : {perf["consecutive_losses"]}
  Today P&L          : {perf["daily_pnl_pct"]:+.2f}%
  Win rate (last 10) : {perf["win_rate_last_10"]:.0%}
  Trades today       : {perf["trades_today"]}
  {loss_note}
╚════════════════════════════════════════════════════════════════╝
{daily_section}
╔══ STOCK: {symbol} ═══════════════════════════════════════════════╗
  Price            : ₹{snap.close:.2f}
  Candle change    : {snap.candle_change_pct:+.2f}%
  Volume vs 20 avg : {snap.volume_ratio:.1f}× {vol_label}
  Primary trend    : {snap.trend}

  ── Indicators ──
  RSI(14)          : {snap.rsi:.1f}  {rsi_note}
  MACD / Signal    : {snap.macd:.3f} / {snap.macd_sig:.3f}  {macd_note}
  MACD histogram   : {snap.macd_hist:.3f}
  ATR(14)          : ₹{snap.atr:.2f}
  Supertrend       : {"BULLISH" if snap.supertrend_bull else "BEARISH"} (₹{snap.supertrend:.2f})
  BB               : upper ₹{snap.bb_upper:.2f} | mid ₹{snap.bb_mid:.2f} | lower ₹{snap.bb_lower:.2f}

  ── Price vs levels ──
  vs VWAP  ₹{snap.vwap:.2f}  : {"ABOVE ✅" if snap.above_vwap()  else "BELOW ❌"}
  vs EMA20 ₹{snap.ema20:.2f} : {"ABOVE ✅" if snap.above_ema20() else "BELOW ❌"}
  vs EMA50 ₹{snap.ema50:.2f} : {"ABOVE ✅" if snap.above_ema50() else "BELOW ❌"}

  ── Market structure ──
  Nearest swing high : ₹{snap.nearest_swing_high:.2f}
  Nearest swing low  : ₹{snap.nearest_swing_low:.2f}
  Distance to s.high : {(snap.nearest_swing_high - snap.close) / snap.close * 100:+.2f}%
  Distance to s.low  : {(snap.nearest_swing_low  - snap.close) / snap.close * 100:+.2f}%

  ── ORB / VWAP ──
{orb_vwap_section}
╚════════════════════════════════════════════════════════════════╝

╔══ STOCK-SPECIFIC NEWS ═════════════════════════════════════════╗
{chr(10).join(f'    • {{h}}' for h in symbol_headlines) if symbol_headlines else '  No relevant news found for this symbol.'}
╚════════════════════════════════════════════════════════════════╝

╔══ PAST SIMILAR SETUP ══════════════════════════════════════════╗
{past_journal if past_journal else "  No similar past setup found in memory."}
╚════════════════════════════════════════════════════════════════╝

Think step by step. Assess the market first. Then the stock.
Decide: LONG, SHORT, or SKIP. Be honest about your conviction.
Output ONLY valid JSON — no prose, no markdown, no explanation.
""".strip()
```

**Important:** The headlines f-string uses `{h}` inside a list comprehension — use `'    • ' + h` to avoid brace-escaping issues. Replace:
```python
{chr(10).join(f'    • {{h}}' for h in symbol_headlines) if symbol_headlines else '  No relevant news found for this symbol.'}
```
with:
```python
{chr(10).join('    • ' + h for h in symbol_headlines) if symbol_headlines else '  No relevant news found for this symbol.'}
```

- [ ] **Step 5: Run tests**

Run: `cd d:\yukti && uv run pytest tests/unit/test_context_two_layer.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd d:\yukti
git add yukti/signals/context.py tests/unit/test_context_two_layer.py
git commit -m "feat(context): two-layer daily+5m context, alignment signal, ORB/VWAP data"
```

---

## Task 7: Arjun — Add Step 1.5 and ORB/VWAP Rules to System Prompt

**Files:**
- Modify: `yukti/agents/arjun.py:90-170` (SYSTEM_PROMPT)
- Modify: `yukti/agents/arjun.py` (TradeDecision.setup_type, GeminiProvider schema)

- [ ] **Step 1: Update setup_type options in TradeDecision**

In `yukti/agents/arjun.py`, update the `setup_type` field's docstring and the Gemini JSON schema to include the new pattern types.

This is a non-breaking change — `setup_type` is `Optional[str]` and not an enum, so just update the Gemini schema string.

Find the Gemini schema's `"setup_type"` entry (around line 400):

```python
                    "setup_type":     {"type": "string", "nullable": True},
```

No change needed — it's a free string already. Good.

Find the `OUTPUT FORMAT` section of `SYSTEM_PROMPT` that lists `setup_type` options:

```
  "setup_type":     "trend_follow" | "breakout" | "breakdown" | "reversal_long" | "reversal_short" | "momentum" | null,
```

Replace with:

```
  "setup_type":     "trend_follow" | "breakout" | "breakdown" | "reversal_long" | "reversal_short" | "momentum" | "orb_breakout" | "vwap_bounce" | null,
```

- [ ] **Step 2: Add Step 1.5 to SYSTEM_PROMPT**

In `yukti/agents/arjun.py`, find this text in `SYSTEM_PROMPT`:

```
Step 2 — Stock Analysis
```

Insert **before** that line (after the Step 1 block):

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

- [ ] **Step 3: Add ORB and VWAP rules**

Find the end of the `Step 6 — Holding Period` block in `SYSTEM_PROMPT`. After the line:

```
SWING: 2-5 days. Only LONG in delivery.
```

Insert:

```

━━━ ORB RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- ORB only valid 09:30–11:00 IST. After 11:00, ignore opening range entirely.
- Narrow opening range (< 1× ATR) breakouts are higher probability.
- If ORB fails (reverses back into range), it becomes a TRAP — do not re-enter same direction.
- ORB entry: breakout candle close. Stop: OR_Mid (tight) or opposite end of range (wider).
- Target 1: 1× opening range width from breakout. Target 2: 2× range width.

━━━ VWAP BOUNCE RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- VWAP Bounce only valid 09:45–14:40 IST.
- VWAP is where institutions trade. Bounces off VWAP in a trending stock are high-probability.
- If VWAP breaks and holds on other side for 2+ candles, trend may be reversing — avoid.
- Stop: VWAP minus 0.5× ATR (for long). Target: nearest swing high/low or 2× stop distance.
```

- [ ] **Step 4: Verify no syntax errors**

Run: `cd d:\yukti && uv run python -c "from yukti.agents.arjun import SYSTEM_PROMPT; print(len(SYSTEM_PROMPT))"`
Expected: prints a number (no import errors).

- [ ] **Step 5: Run existing arjun tests**

Run: `cd d:\yukti && uv run pytest tests/unit/test_arjun.py -v`
Expected: PASS (existing tests unaffected).

- [ ] **Step 6: Commit**

```bash
cd d:\yukti
git add yukti/agents/arjun.py
git commit -m "feat(arjun): add Step 1.5 daily timeframe check + ORB/VWAP rules"
```

---

## Task 8: Universe Scanner Service — Scoring Logic

**Files:**
- Create: `yukti/services/universe_scanner_service.py`
- Test: `tests/unit/test_universe_scanner.py` (new)

This is the largest new file. We split it into two tasks: scoring logic first (testable without Redis/API), then integration (wiring).

- [ ] **Step 1: Write tests for scoring and selection**

Create `tests/unit/test_universe_scanner.py`:

```python
"""tests/unit/test_universe_scanner.py — tests for scanner scoring and selection logic."""
from __future__ import annotations

import pytest


class TestScoring:
    def test_score_volume_surge(self):
        from yukti.services.universe_scanner_service import _score_candidate
        candidate = {
            "symbol": "RELIANCE", "security_id": "1333",
            "vol_ratio": 3.0, "change_pct": 1.0,
            "has_catalyst": False, "sector_in_play": False,
            "avg_turnover_cr": 100,
        }
        score = _score_candidate(candidate)
        assert 0 <= score <= 100
        # vol_ratio=3 → min(3/5,1)*25 = 15.0
        assert score >= 15

    def test_score_caps_at_100(self):
        from yukti.services.universe_scanner_service import _score_candidate
        candidate = {
            "symbol": "RELIANCE", "security_id": "1333",
            "vol_ratio": 10.0, "change_pct": 6.0,
            "has_catalyst": True, "sector_in_play": True,
            "avg_turnover_cr": 200,
        }
        score = _score_candidate(candidate)
        assert score == 100

    def test_score_with_catalyst(self):
        from yukti.services.universe_scanner_service import _score_candidate
        candidate = {
            "symbol": "TCS", "security_id": "11536",
            "vol_ratio": 1.0, "change_pct": 0.5,
            "has_catalyst": True, "sector_in_play": False,
            "avg_turnover_cr": 50,
        }
        score = _score_candidate(candidate)
        assert score >= 20  # catalyst alone is 20

    def test_liquidity_floor_rejects(self):
        from yukti.services.universe_scanner_service import _select_universe
        candidates = [
            {
                "symbol": "PENNY", "security_id": "9999",
                "vol_ratio": 5.0, "change_pct": 3.0,
                "has_catalyst": True, "sector_in_play": True,
                "avg_turnover_cr": 5,  # below 10 Cr threshold
            },
        ]
        selected = _select_universe(candidates, pick_count=15, min_turnover_cr=10)
        assert len(selected) == 0

    def test_selection_respects_pick_count(self):
        from yukti.services.universe_scanner_service import _select_universe
        candidates = [
            {
                "symbol": f"STOCK{i}", "security_id": str(i),
                "vol_ratio": 3.0, "change_pct": 2.0,
                "has_catalyst": False, "sector_in_play": False,
                "avg_turnover_cr": 50,
            }
            for i in range(30)
        ]
        selected = _select_universe(candidates, pick_count=10, min_turnover_cr=10)
        assert len(selected) == 10

    def test_selection_sorted_by_score_desc(self):
        from yukti.services.universe_scanner_service import _score_candidate, _select_universe
        candidates = [
            {
                "symbol": "LOW", "security_id": "1",
                "vol_ratio": 1.0, "change_pct": 0.5,
                "has_catalyst": False, "sector_in_play": False,
                "avg_turnover_cr": 50,
            },
            {
                "symbol": "HIGH", "security_id": "2",
                "vol_ratio": 5.0, "change_pct": 4.0,
                "has_catalyst": True, "sector_in_play": True,
                "avg_turnover_cr": 200,
            },
        ]
        selected = _select_universe(candidates, pick_count=15, min_turnover_cr=10)
        assert selected[0]["symbol"] == "HIGH"

    def test_no_duplicate_inflation(self):
        from yukti.services.universe_scanner_service import _deduplicate_candidates
        candidates = [
            {"symbol": "RELIANCE", "security_id": "1333", "vol_ratio": 3.0,
             "change_pct": 1.0, "has_catalyst": True, "sector_in_play": False,
             "avg_turnover_cr": 100},
            {"symbol": "RELIANCE", "security_id": "1333", "vol_ratio": 2.0,
             "change_pct": 2.0, "has_catalyst": False, "sector_in_play": True,
             "avg_turnover_cr": 100},
        ]
        deduped = _deduplicate_candidates(candidates)
        assert len(deduped) == 1
        assert deduped[0]["symbol"] == "RELIANCE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd d:\yukti && uv run pytest tests/unit/test_universe_scanner.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Create the universe scanner service with scoring logic**

Create `yukti/services/universe_scanner_service.py`:

```python
"""
yukti/services/universe_scanner_service.py
Dynamic stock discovery engine.

Discovers tradeable stocks via 4 sources:
  1. Volume explosions (2x+ avg volume)
  2. Volatility breakouts (±2% close-to-close)
  3. News & events (catalysts from macro service headlines)
  4. Sector momentum (sectoral index moves)

Scores candidates 0-100, selects top N, writes to Redis.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from yukti.config import settings

log = logging.getLogger(__name__)

# ── Nifty 100 symbols (Nifty 50 + Next 50) for discovery pool ────────────────
# This is the scan pool — actual picks are filtered down to scanner_pick_count.
# Security IDs from DhanHQ for NSE_EQ.
NIFTY_100_POOL: dict[str, str] = {
    "RELIANCE": "1333", "HDFCBANK": "1232", "INFY": "1594",
    "TCS": "11536", "ICICIBANK": "4963", "SBIN": "3045",
    "BHARTIARTL": "10604", "HINDUNILVR": "1394", "ITC": "1660",
    "KOTAKBANK": "1922", "LT": "11483", "AXISBANK": "5900",
    "BAJFINANCE": "317", "MARUTI": "10999", "TATAMOTORS": "3456",
    "SUNPHARMA": "3351", "NTPC": "11630", "ONGC": "2475",
    "WIPRO": "3787", "HCLTECH": "7229", "TATASTEEL": "3499",
    "ADANIENT": "25", "ADANIPORTS": "15083", "POWERGRID": "14977",
    "M&M": "2031", "ULTRACEMCO": "11532", "NESTLEIND": "17963",
    "TECHM": "13538", "BAJAJ-AUTO": "16669", "BAJAJFINSV": "16573",
    "JSWSTEEL": "11723", "TITAN": "3506", "DRREDDY": "881",
    "CIPLA": "694", "HINDALCO": "1363", "HEROMOTOCO": "1348",
    "BPCL": "526", "VEDL": "3063", "SHREECEM": "3103",
    "GRASIM": "1232", "COALINDIA": "20374", "DIVISLAB": "10940",
    "EICHERMOT": "14091", "ASIANPAINT": "236", "BRITANNIA": "547",
    "APOLLOHOSP": "157", "SBILIFE": "21808", "HDFCLIFE": "467",
    "INDUSINDBK": "5258", "DABUR": "772",
}

# ── Sector index mapping ─────────────────────────────────────────────────────
SECTOR_STOCKS: dict[str, list[str]] = {
    "BANK": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK"],
    "IT": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"],
    "PHARMA": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP"],
    "AUTO": ["TATAMOTORS", "MARUTI", "M&M", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT"],
    "METAL": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "COALINDIA"],
    "ENERGY": ["RELIANCE", "ONGC", "BPCL", "NTPC", "POWERGRID", "ADANIENT"],
}


# ═══════════════════════════════════════════════════════════════
#  SCORING (pure functions — no I/O, fully testable)
# ═══════════════════════════════════════════════════════════════

def _score_candidate(candidate: dict[str, Any]) -> float:
    """
    Score a discovery candidate 0-100.

    Expected keys:
        vol_ratio:       float  — today's volume / 20-day avg
        change_pct:      float  — absolute close-to-close change %
        has_catalyst:    bool   — news/event catalyst present
        sector_in_play:  bool   — parent sector moving ±1.5%
        avg_turnover_cr: float  — average daily turnover in crores
    """
    vol_ratio = candidate.get("vol_ratio", 0)
    change_pct = abs(candidate.get("change_pct", 0))
    has_catalyst = candidate.get("has_catalyst", False)
    sector_in_play = candidate.get("sector_in_play", False)
    avg_turnover_cr = candidate.get("avg_turnover_cr", 0)

    # Volume surge: weight 25, caps at 5x
    vol_score = min(vol_ratio / 5.0, 1.0) * 25

    # Price move: weight 25, caps at 4%
    price_score = min(change_pct / 4.0, 1.0) * 25

    # Catalyst: weight 20, binary
    catalyst_score = 20 if has_catalyst else 0

    # Sector: weight 15, binary
    sector_score = 15 if sector_in_play else 0

    # Liquidity: weight 15, caps at 50 Cr
    liq_score = min(avg_turnover_cr / 50.0, 1.0) * 15

    total = vol_score + price_score + catalyst_score + sector_score + liq_score
    return min(round(total, 1), 100)


def _deduplicate_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Deduplicate by symbol — keep the entry with the highest computed score.
    A stock found by multiple sources gets its highest score, not summed.
    """
    best: dict[str, dict[str, Any]] = {}
    for c in candidates:
        sym = c["symbol"]
        score = _score_candidate(c)
        if sym not in best or score > _score_candidate(best[sym]):
            best[sym] = c
    return list(best.values())


def _select_universe(
    candidates: list[dict[str, Any]],
    pick_count: int = 15,
    min_turnover_cr: float = 10,
    existing_positions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Apply liquidity floor, sort by score, pick top N.
    Always includes stocks with existing open positions.
    """
    # Liquidity filter
    qualified = [c for c in candidates if c.get("avg_turnover_cr", 0) >= min_turnover_cr]

    # Score and sort
    scored = sorted(qualified, key=lambda c: _score_candidate(c), reverse=True)

    # Pick top N
    selected = scored[:pick_count]

    # Ensure existing positions are included
    if existing_positions:
        selected_symbols = {c["symbol"] for c in selected}
        for c in qualified:
            if c["symbol"] in existing_positions and c["symbol"] not in selected_symbols:
                selected.append(c)

    return selected


# ═══════════════════════════════════════════════════════════════
#  DATA FETCHING (async, hits DhanHQ / Redis / news)
# ═══════════════════════════════════════════════════════════════

async def _fetch_volume_and_price_data(symbols: dict[str, str]) -> list[dict[str, Any]]:
    """
    Fetch previous-day candles for all symbols in the pool.
    Computes volume ratio and price change for each.
    """
    from yukti.execution.dhan_client import dhan

    candidates: list[dict[str, Any]] = []
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    for symbol, sec_id in symbols.items():
        try:
            raw = await dhan.get_candles(sec_id, "1", start, today)
            if not raw or len(raw) < 20:
                continue

            df = pd.DataFrame(
                raw, columns=["time", "open", "high", "low", "close", "volume"]
            ).astype({c: float for c in ["open", "high", "low", "close", "volume"]})

            vol_sma20 = df["volume"].rolling(20).mean().iloc[-1]
            vol_ratio = df["volume"].iloc[-1] / vol_sma20 if vol_sma20 > 0 else 0

            change_pct = 0.0
            if len(df) >= 2:
                change_pct = (df["close"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2] * 100

            avg_turnover = (df["close"] * df["volume"]).rolling(20).mean().iloc[-1] / 1e7  # in crores

            candidates.append({
                "symbol": symbol,
                "security_id": sec_id,
                "vol_ratio": float(vol_ratio),
                "change_pct": float(change_pct),
                "has_catalyst": False,
                "sector_in_play": False,
                "avg_turnover_cr": float(avg_turnover),
            })
        except Exception as exc:
            log.warning("Scanner: failed to fetch %s: %s", symbol, exc)

    return candidates


async def _enrich_with_catalysts(
    candidates: list[dict[str, Any]],
    headlines: list[str],
) -> None:
    """Mark candidates that have news catalysts (in-place)."""
    from yukti.services.macro_context_service import filter_headlines_for_symbol

    for c in candidates:
        matches = filter_headlines_for_symbol(c["symbol"], headlines)
        if matches:
            c["has_catalyst"] = True


async def _enrich_with_sector_momentum(
    candidates: list[dict[str, Any]],
) -> None:
    """
    Check sectoral momentum. If a sector moves ±1.5%, mark its stocks.
    Uses the candidates' own change_pct as a proxy for sector movement
    (average of sector members' changes).
    """
    sector_avg: dict[str, float] = {}
    for sector, members in SECTOR_STOCKS.items():
        changes = [
            c["change_pct"] for c in candidates
            if c["symbol"] in members and c.get("change_pct") is not None
        ]
        if changes:
            sector_avg[sector] = sum(changes) / len(changes)

    for c in candidates:
        for sector, members in SECTOR_STOCKS.items():
            if c["symbol"] in members:
                avg = sector_avg.get(sector, 0)
                if abs(avg) >= 1.5:
                    c["sector_in_play"] = True
                break


# ═══════════════════════════════════════════════════════════════
#  MAIN SCANNER SERVICE
# ═══════════════════════════════════════════════════════════════

class UniverseScannerService:
    """
    Discovers stocks to trade. Runs at 08:45 (primary) and intraday refresh at 10:00, 12:00.
    Writes universe to Redis key `yukti:universe`.
    """

    def __init__(self) -> None:
        self._pool = NIFTY_100_POOL

    async def run_scan(self, is_refresh: bool = False) -> list[dict[str, str]]:
        """
        Execute a full discovery scan.

        Args:
            is_refresh: If True, merge new discoveries with existing universe (never remove).

        Returns:
            List of {symbol, security_id} dicts written to Redis.
        """
        log.info("UniverseScanner: starting %s scan", "refresh" if is_refresh else "primary")

        # 1. Fetch volume + price data for the pool
        candidates = await _fetch_volume_and_price_data(self._pool)
        log.info("UniverseScanner: fetched data for %d symbols", len(candidates))

        # 2. Enrich with catalysts
        try:
            from yukti.data.state import get_redis
            r = await get_redis()
            cached_headlines = await r.get("yukti:market:headlines")
            headlines = cached_headlines.split("||") if cached_headlines else []
        except Exception:
            headlines = []
        await _enrich_with_catalysts(candidates, headlines)

        # 3. Enrich with sector momentum
        await _enrich_with_sector_momentum(candidates)

        # 4. Deduplicate
        candidates = _deduplicate_candidates(candidates)

        # 5. Get existing positions (never remove mid-day)
        existing_positions: list[str] = []
        try:
            from yukti.data.state import get_all_positions
            positions = await get_all_positions()
            existing_positions = list(positions.keys())
        except Exception:
            pass

        # 6. Select
        selected = _select_universe(
            candidates,
            pick_count=settings.scanner_pick_count,
            min_turnover_cr=settings.min_turnover_cr,
            existing_positions=existing_positions,
        )

        # 7. If refresh, merge with existing universe
        if is_refresh:
            selected = await self._merge_with_existing(selected)

        # 8. Write to Redis
        universe_list = [{"symbol": c["symbol"], "security_id": c["security_id"]} for c in selected]
        await self._write_to_redis(universe_list)

        # 9. Log scored results
        for c in selected:
            score = _score_candidate(c)
            log.info(
                "UniverseScanner: picked %s (score=%.1f, vol=%.1f×, chg=%.1f%%, catalyst=%s, sector=%s)",
                c["symbol"], score, c.get("vol_ratio", 0), c.get("change_pct", 0),
                c.get("has_catalyst"), c.get("sector_in_play"),
            )

        log.info("UniverseScanner: selected %d symbols", len(universe_list))
        return universe_list

    async def _merge_with_existing(self, new_picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge new discoveries with existing universe — never remove a stock mid-day."""
        try:
            from yukti.data.state import get_redis
            r = await get_redis()
            raw = await r.get("yukti:universe")
            if raw:
                existing = json.loads(raw)
                existing_symbols = {e["symbol"] for e in existing}
                new_symbols = {c["symbol"] for c in new_picks}
                # Add new picks to existing, keep all existing
                merged_list = list(existing)
                for c in new_picks:
                    if c["symbol"] not in existing_symbols:
                        merged_list.append({"symbol": c["symbol"], "security_id": c["security_id"]})
                return [
                    next((p for p in new_picks if p["symbol"] == m["symbol"]), m)
                    for m in merged_list
                ]
        except Exception as exc:
            log.warning("UniverseScanner: merge failed: %s", exc)
        return new_picks

    async def _write_to_redis(self, universe_list: list[dict[str, str]]) -> None:
        """Write universe to Redis."""
        try:
            from yukti.data.state import get_redis
            r = await get_redis()
            await r.set("yukti:universe", json.dumps(universe_list))
            log.info("UniverseScanner: wrote %d symbols to yukti:universe", len(universe_list))
        except Exception as exc:
            log.error("UniverseScanner: Redis write failed: %s", exc)

    async def run_with_fallback(self, is_refresh: bool = False) -> list[dict[str, str]]:
        """
        Run scan with fallback chain:
        1. Full scan
        2. Previous session universe from Redis
        3. Emergency Nifty 50 baseline
        """
        try:
            return await self.run_scan(is_refresh=is_refresh)
        except Exception as exc:
            log.error("UniverseScanner: scan failed: %s — trying fallback", exc)

        # Fallback 1: previous session
        try:
            from yukti.data.state import get_redis
            r = await get_redis()
            raw = await r.get("yukti:universe")
            if raw:
                universe = json.loads(raw)
                log.warning("UniverseScanner: using previous session universe (%d symbols)", len(universe))
                return universe
        except Exception:
            pass

        # Fallback 2: emergency baseline
        log.warning("UniverseScanner: using emergency Nifty 50 baseline")
        baseline = [
            {"symbol": s, "security_id": sid}
            for s, sid in list(NIFTY_100_POOL.items())[:50]
        ]
        try:
            from yukti.data.state import get_redis
            r = await get_redis()
            await r.set("yukti:universe", json.dumps(baseline))
        except Exception:
            pass
        return baseline
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd d:\yukti && uv run pytest tests/unit/test_universe_scanner.py -v`
Expected: PASS — all 7 scoring/selection tests green.

- [ ] **Step 5: Commit**

```bash
cd d:\yukti
git add yukti/services/universe_scanner_service.py tests/unit/test_universe_scanner.py
git commit -m "feat(scanner): add universe scanner service with scoring and selection"
```

---

## Task 9: Scheduler — Add Scanner Jobs

**Files:**
- Modify: `yukti/scheduler/jobs.py`

- [ ] **Step 1: Add the scanner job functions**

At the bottom of `yukti/scheduler/jobs.py`, before `build_scheduler()`, add:

```python
async def job_universe_scan() -> None:
    """Pre-market universe scan at 08:45 IST."""
    log.info("=== universe scan (primary) ===")
    from yukti.services.universe_scanner_service import UniverseScannerService
    scanner = UniverseScannerService()
    await scanner.run_with_fallback(is_refresh=False)


async def job_universe_refresh() -> None:
    """Intraday universe refresh — add new movers, never remove."""
    log.info("=== universe refresh ===")
    from yukti.services.universe_scanner_service import UniverseScannerService
    scanner = UniverseScannerService()
    await scanner.run_with_fallback(is_refresh=True)
```

- [ ] **Step 2: Register jobs in build_scheduler()**

Update `build_scheduler()` to add the new jobs. Replace the existing function with:

```python
def build_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="Asia/Kolkata")
    sched.add_job(job_universe_scan,    "cron", hour=8,  minute=45)
    sched.add_job(job_morning_prep,     "cron", hour=9,  minute=0)
    sched.add_job(job_universe_refresh, "cron", hour=10, minute=0)
    sched.add_job(job_universe_refresh, "cron", hour=12, minute=0)
    sched.add_job(job_eod_squareoff,    "cron", hour=15, minute=10)
    sched.add_job(job_daily_reset,      "cron", hour=16, minute=0)
    sched.add_job(job_daily_report,     "cron", hour=16, minute=30)
    return sched
```

- [ ] **Step 3: Verify import works**

Run: `cd d:\yukti && uv run python -c "from yukti.scheduler.jobs import build_scheduler; s = build_scheduler(); print(f'Jobs: {len(s.get_jobs())}')"`
Expected: `Jobs: 7`

- [ ] **Step 4: Commit**

```bash
cd d:\yukti
git add yukti/scheduler/jobs.py
git commit -m "feat(scheduler): add 08:45 universe scan + 10:00/12:00 refresh jobs"
```

---

## Task 10: Market Scan Service — Fetch Daily Candles + Wire Everything Together

**Files:**
- Modify: `yukti/services/market_scan_service.py`

This is the integration task that wires daily candle fetching, daily indicators, updated pattern detection, and the new context builder parameters through the scan pipeline.

- [ ] **Step 1: Add daily candle fetch + cache method**

Add the following method to the `MarketScanService` class, after `__init__`:

```python
    async def _get_daily_candles(self, symbol: str, security_id: str) -> pd.DataFrame | None:
        """
        Fetch 60-day daily candles, cached in Redis for one trading session.
        Returns DataFrame or None on failure.
        """
        from yukti.data.state import get_redis
        cache_key = f"yukti:daily_candles:{symbol}"
        r = await get_redis()

        # Check cache
        cached = await r.get(cache_key)
        if cached:
            import json
            data = json.loads(cached)
            df = pd.DataFrame(data)
            if len(df) >= 20:
                return df

        # Fetch from DhanHQ
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=settings.daily_candle_history + 10)).strftime("%Y-%m-%d")
            raw = await dhan.get_candles(security_id, "1", start, today)
            if not raw or len(raw) < 20:
                return None

            df = pd.DataFrame(
                raw, columns=["time", "open", "high", "low", "close", "volume"]
            ).astype({c: float for c in ["open", "high", "low", "close", "volume"]})

            # Cache with session TTL
            import json
            await r.set(cache_key, json.dumps(df.to_dict("records")), ex=settings.daily_cache_ttl)
            log.debug("Cached daily candles for %s (%d rows)", symbol, len(df))
            return df
        except Exception as exc:
            log.warning("Failed to fetch daily candles for %s: %s", symbol, exc)
            return None
```

- [ ] **Step 2: Update _scan_symbol() to use daily indicators**

Replace the existing `_scan_symbol()` method with the updated version that fetches daily candles, computes daily indicators, computes ORB levels, and passes everything through:

```python
    async def _scan_symbol(self, symbol: str, security_id: str, macro: MacroContext, perf: dict) -> None:
        """Scan one symbol with daily + 5-min multi-timeframe analysis."""
        async with self.sem:
            signals_scanned.inc()
            log.info("MarketScanService: scanning %s", symbol)
            try:
                # ── 5-min candles (existing) ──────────────────────────
                today = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
                raw = await dhan.get_candles(security_id, 5, start, today)
                if not raw or len(raw) < 60:
                    return

                df = pd.DataFrame(
                    raw, columns=["time", "open", "high", "low", "close", "volume"]
                ).astype({c: float for c in ["open", "high", "low", "close", "volume"]})
                snap = compute(df, timeframe="5m")

                # ── Daily candles (new) ───────────────────────────────
                snap_daily = None
                daily_df = await self._get_daily_candles(symbol, security_id)
                if daily_df is not None and len(daily_df) >= 20:
                    snap_daily = compute(daily_df, timeframe="daily")

                # ── Current time for time-gating ──────────────────────
                current_time = datetime.now().time()

                # ── ORB levels (from first 3 candles of today) ────────
                or_high, or_low = None, None
                if len(df) >= 3:
                    or_candles = df.iloc[:3]
                    or_high = float(or_candles["high"].max())
                    or_low = float(or_candles["low"].min())

                # ── Pattern detection (updated) ───────────────────────
                pattern = best_pattern(snap, candles=df, indicators_daily=snap_daily, current_time=current_time)

                # ── Memory retrieval ──────────────────────────────────
                memory_setup = pattern.pattern_type if pattern else "unknown"
                memory_dir = "LONG" if macro.nifty_trend == "UP" else "SHORT" if macro.nifty_trend == "DOWN" else "LONG"
                past_journal = await retrieve_similar(symbol, memory_setup, memory_dir)
                symbol_headlines = filter_headlines_for_symbol(symbol, macro.headlines)

                # ── Context (updated with daily + ORB/VWAP) ──────────
                context = build_context(
                    symbol, snap, macro, perf, past_journal, symbol_headlines,
                    indicators_daily=snap_daily,
                    or_high=or_high,
                    or_low=or_low,
                )

                decision = await arjun.safe_decide(context)
                log.info("MarketScanService: AI decision for %s: %s (conviction %d)", symbol, decision.action, decision.conviction)

                if decision.action == "SKIP":
                    record_skip(decision.skip_reason or "claude_skip")
                    return

                if not decision.stop_loss or not decision.target_1:
                    levels = calculate_levels(decision.direction or "LONG", decision.entry_price or snap.close, snap.atr, snap.nearest_swing_low, snap.nearest_swing_high)
                    decision.stop_loss = decision.stop_loss or levels.stop_loss
                    decision.target_1 = decision.target_1 or levels.target_1
                    decision.target_2 = decision.target_2 or levels.target_2
                    decision.risk_reward = decision.risk_reward or levels.risk_reward

                portfolio = Portfolio(
                    account_value=settings.account_value,
                    open_positions=await count_open_positions(),
                    daily_pnl_pct=await get_daily_pnl_pct(),
                    total_exposure_pct=0.0,
                )
                gate = await run_gates(decision, portfolio)
                if not gate.passed:
                    record_skip(gate.reason or "gate_blocked")
                    log.info("MarketScanService: risk gate failed for %s: %s", symbol, gate.reason)
                    return

                position = calculate_position(decision.entry_price or snap.close, decision.stop_loss, decision.direction or "LONG", decision.conviction)
                pos = await open_trade(symbol, security_id, decision, position)
                if pos:
                    record_trade_opened(decision.direction or "LONG", decision.setup_type or "unknown")
                    try:
                        from yukti.telegram.bot import alert_trade_opened
                        await alert_trade_opened(pos)
                    except Exception as tg_exc:
                        log.warning("Telegram trade alert failed: %s", tg_exc)

            except Exception as exc:
                log.error("MarketScanService: scan error %s: %s", symbol, exc)
```

- [ ] **Step 3: Add the `compute` import update**

The existing import `from yukti.signals.indicators import compute` is already present. Add the `timeframe` parameter to the `compute()` calls — already done in Step 2.

- [ ] **Step 4: Verify import chain works**

Run: `cd d:\yukti && uv run python -c "from yukti.services.market_scan_service import MarketScanService; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Run all existing tests to verify nothing broke**

Run: `cd d:\yukti && uv run pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
cd d:\yukti
git add yukti/services/market_scan_service.py
git commit -m "feat(scan): wire daily candles, multi-timeframe indicators, ORB/VWAP through pipeline"
```

---

## Task 11: Integration — End-to-End Smoke Test

**Files:**
- Modify: `tests/unit/test_signals.py` (add import check for new patterns)

- [ ] **Step 1: Add a smoke test that the full import chain works**

Append to `tests/unit/test_signals.py`:

```python
class TestNewPatternIntegration:
    """Verify new patterns integrate with the existing scan pipeline."""

    def test_orb_importable(self):
        from yukti.signals.patterns import orb_breakout
        assert callable(orb_breakout)

    def test_vwap_importable(self):
        from yukti.signals.patterns import vwap_bounce
        assert callable(vwap_bounce)

    def test_scan_all_with_new_params(self):
        from yukti.signals.patterns import scan_all
        df = _make_ohlcv()
        snap = compute(df)
        from datetime import time
        results = scan_all(snap, candles=df, current_time=time(10, 0))
        assert isinstance(results, list)

    def test_compute_daily_indicators(self):
        df = _make_ohlcv(n=80)
        snap = compute(df, timeframe="daily")
        assert snap.adx is not None
        assert snap.daily_support is not None
        assert snap.daily_resistance is not None

    def test_context_with_daily(self):
        from unittest.mock import MagicMock
        from yukti.signals.context import build_context

        df = _make_ohlcv()
        snap = compute(df)
        snap_daily = compute(df, timeframe="daily")

        macro = MagicMock()
        macro.nifty_chg_pct = 0.5
        macro.nifty_trend = "UP"
        macro.vix_label = "15.0"
        macro.fii_label = "N/A"
        macro.dii_label = "N/A"
        macro.headlines_text = "  None"
        perf = {
            "consecutive_losses": 0, "daily_pnl_pct": 0.0,
            "win_rate_last_10": 0.5, "trades_today": 0,
        }
        ctx = build_context("TEST", snap, macro, perf, indicators_daily=snap_daily)
        assert "DAILY TIMEFRAME" in ctx
        assert "TEST" in ctx
```

- [ ] **Step 2: Run the full test suite**

Run: `cd d:\yukti && uv run pytest tests/ -v --tb=short`
Expected: ALL PASS.

- [ ] **Step 3: Commit**

```bash
cd d:\yukti
git add tests/unit/test_signals.py
git commit -m "test: add integration smoke tests for signal quality upgrade"
```

---

## Task 12: Update .env.example with New Config Keys

**Files:**
- Modify: `d:\yukti\.env.example`

- [ ] **Step 1: Add new config keys to .env.example**

Append to `.env.example`:

```env
# ── Universe Scanner ──────────────────────────────────────────
# SCANNER_PICK_COUNT=15
# MIN_TURNOVER_CR=10
# VOLUME_SURGE_THRESHOLD=2.0
# PRICE_MOVE_THRESHOLD=1.5
# INTRADAY_REFRESH_TIMES=10:00,12:00

# ── Multi-Timeframe (Daily Candles) ──────────────────────────
# DAILY_CANDLE_HISTORY=60
# DAILY_CACHE_TTL=28800
```

- [ ] **Step 2: Commit**

```bash
cd d:\yukti
git add .env.example
git commit -m "docs: add new scanner and daily candle config keys to .env.example"
```

---

## Summary

| Task | Component | Type | Dependency |
|------|-----------|------|------------|
| 1 | Config — new settings | Modify | None |
| 2 | Indicators — timeframe, ADX, daily S/R | Modify | Task 1 |
| 3 | Patterns — ORB Breakout | Modify | Task 2 |
| 4 | Patterns — VWAP Bounce | Modify | Task 3 |
| 5 | Patterns — update scan_all/best_pattern | Modify | Task 4 |
| 6 | Context — two-layer builder | Modify | Task 2 |
| 7 | Arjun — Step 1.5 + ORB/VWAP rules | Modify | None |
| 8 | Universe Scanner — scoring logic | New | Task 1 |
| 9 | Scheduler — scanner jobs | Modify | Task 8 |
| 10 | Market Scan Service — wire everything | Modify | Tasks 2, 5, 6 |
| 11 | Integration smoke tests | Test | All above |
| 12 | .env.example update | Docs | Task 1 |

**Parallelizable:** Tasks 7 and 8 can run in parallel with Tasks 3-6 (no code dependencies between Arjun prompt changes / scanner service and the pattern/context work).
