"""tests/unit/test_patterns_orb_vwap.py — tests for ORB and VWAP Bounce patterns."""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd
import pytest

from yukti.signals.indicators import IndicatorSnapshot
from yukti.signals.patterns import orb_breakout, vwap_bounce, scan_all, best_pattern, PatternSignal


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
    data = {
        "open":   [1000, 1005, 1003, 1010, 1015, 1020, 1018, 1025],
        "high":   [1008, 1012, 1010, 1018, 1022, 1028, 1025, 1030],
        "low":    [ 995,  998,  997, 1005, 1010, 1015, 1012, 1020],
        "close":  [1005, 1003, 1008, 1015, 1020, 1025, 1022, 1028],
        "volume": [500000]*8,
    }
    times = pd.date_range("2024-01-02 09:15", periods=8, freq="5min")
    return pd.DataFrame(data, index=times)


# ═══════════════════════════════════════════════════════════════
#  ORB TESTS
# ═══════════════════════════════════════════════════════════════

class TestORBBreakout:
    def test_orb_detected_above_range(self):
        candles = _make_orb_candles()
        snap = _make_snap(close=1028.0, rsi=60.0, volume_ratio=1.8)
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
        assert result.strength > 0

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


# ═══════════════════════════════════════════════════════════════
#  VWAP BOUNCE TESTS
# ═══════════════════════════════════════════════════════════════

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

    def test_vwap_returns_valid_signal(self):
        candles = _make_vwap_candles_long()
        snap = _make_snap(close=1005.0, vwap=1005.0, rsi=50.0)
        result = vwap_bounce(snap, candles, current_time=time(10, 30))
        assert isinstance(result, PatternSignal)


# ═══════════════════════════════════════════════════════════════
#  SCAN_ALL / BEST_PATTERN UPDATED SIGNATURES
# ═══════════════════════════════════════════════════════════════

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
        assert result is None or isinstance(result, PatternSignal)
